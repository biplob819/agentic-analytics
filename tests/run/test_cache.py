"""RUN-02: the content-addressed answer cache (D-1)."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentic_analytics.run.cache import (
    CODE_VERSION,
    AnswerCache,
    AskResult,
    KeyParts,
    cache_key,
)
from tests.schemas import factories as f

PARTS = KeyParts(
    backend="claude_mcp",
    model_snapshot="claude-sonnet-5-5",
    question_hash="a" * 64,
    fixes=(),
    fix_hash="b" * 64,
    prompt_hash="c" * 64,
    semantic_hash="d" * 64,
    snapshot_sha256="e" * 64,
    trial=1,
    as_of="2026-09-30",
    limits={"max_tool_calls": 6, "max_turns": 10, "row_cap_model": 50, "row_cap_exec": 10_000},
    params={"effort": "medium"},
)


def result(**kw) -> AskResult:
    base = dict(
        answer=f.answer(),
        execution=f.execution(),
        cost_usd=0.0123,
        tokens={"in": 100, "out": 20, "cached": 50, "reasoning": 0},
        cortex_messages=0,
        warehouse_elapsed_ms=40,
        turns=2,
    )
    base.update(kw)
    return AskResult(**base)  # type: ignore[arg-type]


def test_every_d1_input_changes_the_key():
    base = cache_key(PARTS)
    changes = {
        "backend": "gpt_mcp",
        "model_snapshot": "claude-sonnet-5-6",
        "question_hash": "1" * 64,
        "fixes": ("examples",),
        "fix_hash": "2" * 64,
        "prompt_hash": "3" * 64,
        "semantic_hash": None,
        "snapshot_sha256": "5" * 64,
        "trial": 2,
        "as_of": "2026-10-31",
        "limits": {**PARTS.limits, "max_tool_calls": 7},
        "params": {"effort": "high"},
    }
    keys = {name: cache_key(replace(PARTS, **{name: v})) for name, v in changes.items()}
    assert base not in keys.values()
    assert len(set(keys.values())) == len(changes)


def test_code_version_is_in_the_key():
    data = PARTS.as_dict()
    assert data["code_version"] == CODE_VERSION and data["node"] == "ask"
    assert cache_key({**data, "code_version": "ask-v0"}) != cache_key(PARTS)


@given(st.permutations(sorted(PARTS.as_dict())))
def test_key_is_stable_under_dict_ordering(order):
    data = PARTS.as_dict()
    shuffled = {k: data[k] for k in order}
    limits = dict(reversed(list(data["limits"].items())))
    assert cache_key({**shuffled, "limits": limits}) == cache_key(PARTS)


def test_round_trip_and_layout(tmp_path):
    cache = AnswerCache(tmp_path)
    key = cache_key(PARTS)
    assert cache.get(key) is None and key not in cache
    path = cache.put(key, result(), PARTS)
    assert path == tmp_path / key[:2] / f"{key}.json"
    got = cache.get(key)
    assert got == result()
    assert key in cache
    assert json.loads(path.read_text(encoding="utf-8"))["parts"]["trial"] == 1


def test_clarification_without_execution_round_trips(tmp_path):
    cache = AnswerCache(tmp_path)
    ans = f.answer(sql=None, answer="", clarification="Which year?", steps=[])
    cache.put("f" * 64, result(answer=ans, execution=None, warehouse_elapsed_ms=0))
    got = cache.get("f" * 64)
    assert got is not None and got.execution is None and got.answer.clarification == "Which year?"


@pytest.mark.parametrize(
    "corrupt",
    [
        "not json",
        json.dumps({"key": "0" * 64, "value": {}}),  # someone else's key
        json.dumps({"key": "KEY", "value": {"answer": {}}}),  # invalid value
        json.dumps({"key": "KEY", "value": {**result().to_json(), "cost_usd": -1}}),
        json.dumps({"key": "KEY", "value": {**result().to_json(), "extra": 1}}),
    ],
)
def test_corrupt_or_foreign_entries_are_misses(tmp_path, corrupt):
    cache = AnswerCache(tmp_path)
    key = cache_key(PARTS)
    p = cache.path(key)
    p.parent.mkdir(parents=True)
    p.write_text(corrupt.replace("KEY", key), encoding="utf-8")
    assert cache.get(key) is None
    cache.put(key, result())  # a miss is overwritten by the next write
    assert cache.get(key) == result()


def test_bad_keys_are_refused(tmp_path):
    with pytest.raises(ValueError):
        AnswerCache(tmp_path).path("../../etc/passwd")


def test_failed_write_keeps_the_old_entry_and_leaves_no_temp(tmp_path, monkeypatch):
    cache = AnswerCache(tmp_path)
    key = cache_key(PARTS)
    cache.put(key, result())

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cache.put(key, result(cost_usd=9.0))
    monkeypatch.undo()
    assert cache.get(key) == result()
    leftovers = [p for p in Path(tmp_path).rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []


def test_transport_and_timeout_answers_are_never_cached(tmp_path):
    cache = AnswerCache(tmp_path)
    for error in ("transport", "timeout"):
        failed = result(
            answer=f.answer(sql=None, answer="", error=error, steps=[]),
            execution=None,
            warehouse_elapsed_ms=0,
        )
        assert failed.transient
        with pytest.raises(ValueError, match="never cached"):
            cache.put("f" * 64, failed)
    slow = result(
        execution=f.execution(
            result=None, rows_total=None, truncated=False, error="timeout", query_id=None
        )
    )
    assert slow.transient
    assert not result().transient
    assert list(tmp_path.rglob("*.json")) == []


def test_cached_value_carries_the_fix_hash_stamp(tmp_path):
    cache = AnswerCache(tmp_path)
    cache.put("e" * 64, result(fix_hash="9" * 64))
    got = cache.get("e" * 64)
    assert got is not None and got.fix_hash == "9" * 64


def test_ask_wall_time_and_cortex_latency_round_trip(tmp_path):
    """R3.1: the cached value carries the ask's wall time and Cortex's own latency, so a
    resume or `--rescore` reports the same latency as the run that made the answer."""
    cache = AnswerCache(tmp_path)
    key = cache_key(PARTS)
    cache.put(key, result(ask_wall_ms=2_400, analyst_latency_ms=812.5), PARTS)
    got = cache.get(key)
    assert got is not None and (got.ask_wall_ms, got.analyst_latency_ms) == (2_400, 812.5)
    data = json.loads(cache.path(key).read_text(encoding="utf-8"))["value"]
    assert data["ask_wall_ms"] == 2_400 and data["analyst_latency_ms"] == 812.5
    assert CODE_VERSION == "ask-v3"


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("ask_wall_ms", -1),
        ("ask_wall_ms", 1.5),
        ("ask_wall_ms", True),
        ("analyst_latency_ms", -0.1),
        ("analyst_latency_ms", "12"),
        ("analyst_latency_ms", True),
    ],
)
def test_bad_timings_in_an_entry_read_as_a_miss(tmp_path, field, bad):
    cache = AnswerCache(tmp_path)
    key = cache_key(PARTS)
    cache.put(key, result(ask_wall_ms=10, analyst_latency_ms=1.0), PARTS)
    p = cache.path(key)
    entry = json.loads(p.read_text(encoding="utf-8"))
    entry["value"][field] = bad
    p.write_text(json.dumps(entry), encoding="utf-8")
    assert cache.get(key) is None


def test_an_entry_from_before_the_timings_reads_as_a_miss(tmp_path):
    """An ask-v2 value (no timings) is never read as an answer with an unknown latency."""
    cache = AnswerCache(tmp_path)
    key = cache_key(PARTS)
    cache.put(key, result(), PARTS)
    p = cache.path(key)
    entry = json.loads(p.read_text(encoding="utf-8"))
    del entry["value"]["ask_wall_ms"], entry["value"]["analyst_latency_ms"]
    p.write_text(json.dumps(entry), encoding="utf-8")
    assert cache.get(key) is None
