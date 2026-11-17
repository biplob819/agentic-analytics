"""The golden run (Tech Spec §11): a 12-question end-to-end run against recorded MCP, model,
Cortex and warehouse responses, with no network, must reproduce the stored `summary.json`
byte for byte. The recordings come from the fakes until A2.1/A2.2 record live responses
(`tests/golden/record.py`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.packs import load_pack
from agentic_analytics.questions import load_questions
from agentic_analytics.schemas import RunSummary
from tests.golden.record import GOLDEN, golden_run, scores_digest

pytestmark = pytest.mark.golden


def rd(out: Any) -> Path:
    assert out.run_dir is not None, out.message
    return out.run_dir


def sm(out: Any) -> RunSummary:
    assert out.summary is not None, out.message
    return out.summary


def test_golden_run_reproduces_the_stored_summary(tmp_path):
    out = golden_run(tmp_path)
    assert out.exit_code == 0, out.message
    got = (rd(out) / "summary.json").read_bytes()
    assert got == (GOLDEN / "summary.json").read_bytes()
    assert scores_digest(rd(out)) == (GOLDEN / "scores.json").read_text(encoding="utf-8")
    # OWNER D-81: no equivalence fires on the golden answers, so strict equals the headline
    for line in (rd(out) / "records.jsonl").read_text(encoding="utf-8").splitlines():
        sc = json.loads(line)["scores"]
        assert sc["correct_strict"] == sc["correct"] and "equivalences" not in sc


def test_golden_run_exercises_trials_fixes_and_scorer_edges():
    summary = RunSummary.model_validate_json((GOLDEN / "summary.json").read_text("utf-8"))
    assert {b.trials for b in summary.backends} == {3}
    assert {(u.backend, u.fix) for u in summary.uplifts} == {
        ("claude_mcp", "examples"),
        ("gpt_mcp", "examples"),
    }
    assert any(0 < b.pass_all_trials.point < b.accuracy.point for b in summary.backends)
    assert any(b.trial_agreement < 1 for b in summary.backends)
    reasons = (GOLDEN / "scores.json").read_text(encoding="utf-8")
    assert "row_count" in reasons  # extra and missing rows
    pack = load_pack(GOLDEN / "pack")
    ordered = [q for q in load_questions(pack) if q.ordered]
    assert ordered and any(q.tolerance.rel == 0.005 for q in load_questions(pack))


def test_golden_inputs_are_what_the_spec_asks_for():
    pack = load_pack(GOLDEN / "pack")
    questions = load_questions(pack)
    assert len(questions) == 12
    cats = sorted(q.category for q in questions)
    assert all(cats.count(c) == 2 for c in set(cats)) and len(set(cats)) == 6
    rec = json.loads((GOLDEN / "recording.json").read_text(encoding="utf-8"))
    assert set(rec) >= {"agents", "cortex", "warehouse", "note"}
    assert "A2.1" in rec["note"] and "A2.2" in rec["note"]
    summary = RunSummary.model_validate_json((GOLDEN / "summary.json").read_text("utf-8"))
    assert {b.backend for b in summary.backends} == {"claude_mcp", "gpt_mcp", "cortex_analyst"}
    assert summary.run_id == "golden" and summary.probes


def test_summary_is_deterministic_across_reruns_and_from_cache(tmp_path):
    first = golden_run(tmp_path / "a")
    second = golden_run(tmp_path / "b")
    a = (rd(first) / "summary.json").read_bytes()
    assert a == (rd(second) / "summary.json").read_bytes()

    def no_calls():
        raise AssertionError("a fully cached rerun must not build services")

    import shutil

    shutil.rmtree(tmp_path / "a" / "results")
    cached = golden_run(tmp_path / "a", services=no_calls)
    assert cached.exit_code == 0 and cached.called == 0
    assert (rd(cached) / "summary.json").read_bytes() == a
