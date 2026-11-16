"""CTX-02: the response parser reads every documented field and survives everything else."""

from __future__ import annotations

import json

import pytest

from agentic_analytics.backends.cortex import CortexSchemaError, parse_response
from tests.backends.cortex_fakes import (
    MODELS,
    SQL,
    body,
    run,
    sql_block,
    suggestions_block,
    text_block,
)

# The documented example shape (Snowflake docs, Cortex Analyst REST API, checked 2026-11-15).
DOCS_EXAMPLE = {
    "request_id": "75d343ee-699c-483f-83a1-e314609fb563",
    "message": {
        "role": "analyst",
        "content": [
            {"type": "text", "text": "This is our interpretation of your question: ..."},
            {
                "type": "sql",
                "statement": SQL,
                "confidence": {
                    "verified_query_used": {
                        "name": "median_price_by_town",
                        "question": "Median price in a town",
                        "sql": "SELECT 1",
                        "verified_at": 1714497970,
                        "verified_by": "Jane Doe",
                    }
                },
            },
        ],
    },
    "warnings": [{"message": "Table '...' has (30) dimensions, max (10)"}],
    "response_metadata": {
        "model_names": ["claude-sonnet-5-5"],
        "question_category": "UNAMBIGUOUS_SQL",
        "cortex_search_retrieval": [],
    },
}


def test_parses_the_documented_example():
    r = parse_response(json.dumps(DOCS_EXAMPLE).encode())
    assert r.request_id == "75d343ee-699c-483f-83a1-e314609fb563"
    assert r.text == "This is our interpretation of your question: ..."
    assert r.sql == SQL
    assert r.verified_query_used is not None
    assert r.verified_query_used["name"] == "median_price_by_town"
    assert r.suggestions == ()
    assert r.warnings == ("Table '...' has (30) dimensions, max (10)",)
    assert r.model_names == ("claude-sonnet-5-5",)
    assert r.model_snapshot == "claude-sonnet-5-5"
    assert r.question_category == "UNAMBIGUOUS_SQL"
    assert r.ignored == ()


def test_accepts_str_bytes_and_mapping():
    a = parse_response(DOCS_EXAMPLE)
    b = parse_response(json.dumps(DOCS_EXAMPLE))
    c = parse_response(json.dumps(DOCS_EXAMPLE).encode())
    assert a == b == c


def test_verified_query_absent_or_null_is_none():
    assert parse_response(body(sql_block(verified=None))).verified_query_used is None
    bare = {"type": "sql", "statement": "SELECT 1"}
    assert parse_response(body(bare)).verified_query_used is None
    odd = {"type": "sql", "statement": "SELECT 1", "confidence": "high"}
    assert parse_response(body(odd)).verified_query_used is None


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        ("suggestions", "suggestions"),
        ("suggestion", "suggestions"),
        ("suggestion", "suggestion"),
        ("suggestions", "suggestion"),
    ],
)
def test_both_suggestion_spellings_accepted(kind, key):
    r = parse_response(body(text_block(), suggestions_block("A?", "B?", kind=kind, key=key)))
    assert r.suggestions == ("A?", "B?")
    assert r.sql is None
    assert r.ignored == ()


def test_suggestions_cleaned_and_deduplicated():
    block = {"type": "suggestions", "suggestions": ["  A? ", "", 3, None, "A?", "B?"]}
    assert parse_response(body(block)).suggestions == ("A?", "B?")


def test_unknown_and_malformed_content_ignored_and_recorded():
    r = parse_response(
        body(
            text_block("first"),
            {"type": "chart", "spec": {}},
            "not an object",
            {"no_type": True},
            {"type": "sql", "statement": "  "},
            {"type": "text", "text": ""},
            {"type": "suggestions", "suggestions": []},
            sql_block("SELECT 1"),
            sql_block("SELECT 2"),
            text_block("second"),
        )
    )
    assert r.sql == "SELECT 1"
    assert r.text == "first\n\nsecond"
    assert r.ignored == (
        "chart",
        "<untyped>",
        "<untyped>",
        "sql (no statement)",
        "text (empty)",
        "suggestions (empty)",
        "sql (more than one)",
    )


def test_sql_and_suggestions_together_keep_the_sql():
    r = parse_response(body(sql_block("SELECT 1"), suggestions_block("A?")))
    assert r.sql == "SELECT 1"
    assert r.suggestions == ()
    assert r.ignored == ("suggestions (alongside sql)",)


def test_metadata_read_defensively():
    data = body(text_block(), request_id=None)
    data["request_id"] = 42
    data["warnings"] = ["plain string warning", {"message": ""}, {"code": 1}, 7]
    data["response_metadata"] = {"model_names": "solo-model", "question_category": 5}
    r = parse_response(data)
    assert r.request_id is None
    assert r.warnings == ("plain string warning",)
    assert r.model_names == ("solo-model",)
    assert r.question_category is None
    data["response_metadata"] = "junk"
    data["warnings"] = {"message": "not a list"}
    r = parse_response(data)
    assert r.model_names == ()
    assert r.model_snapshot is None
    assert r.warnings == ()


def test_model_names_joined_for_the_snapshot():
    assert parse_response(body(text_block())).model_snapshot == ",".join(MODELS)
    names = ["a", " ", 3, "b"]
    assert parse_response(body(text_block(), model_names=names)).model_snapshot == "a,b"


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff\xfe",
        b"[1, 2]",
        b'"text"',
        b"{}",
        b'{"message": null}',
        b'{"message": {"content": "text"}}',
        b'{"message": {"role": "analyst"}}',
    ],
)
def test_unreadable_bodies_are_schema_errors(raw):
    with pytest.raises(CortexSchemaError):
        parse_response(raw)


def test_unreadable_200_is_a_schema_answer_and_billed():
    out, api, ex = run(b'{"message": {"role": "analyst"}}')
    assert len(api.requests) == 1
    assert out.answer.error == "schema"
    assert out.answer.sql is None
    assert out.answer.answer == ""
    assert out.cortex_messages == 1
    assert ex.calls == []


def test_parsed_fields_reach_the_run():
    out, _, _ = run(
        body(text_block("reading"), sql_block(verified={"name": "vq"}), warnings=["w1", "w2"])
    )
    assert out.warnings == ("w1", "w2")
    assert out.response is not None
    assert out.response.verified_query_used == {"name": "vq"}
    assert out.answer.request_id == "req-0001"
    assert out.answer.model_snapshot == ",".join(MODELS)
    assert out.answer.interpretation == "reading"


def test_schema_error_keeps_the_request_id():
    with pytest.raises(CortexSchemaError) as info:
        parse_response(b'{"request_id": " rid-9 ", "message": {"role": "analyst"}}')
    assert info.value.request_id == "rid-9"
    out, _, _ = run(b'{"request_id": "rid-9", "message": {}}')
    assert out.answer.error == "schema"
    assert out.answer.request_id == "rid-9"


@pytest.mark.parametrize("rid", ['"  "', '""', "42", "null"])
def test_schema_error_with_a_blank_or_odd_request_id_has_none(rid):
    out, _, _ = run(f'{{"request_id": {rid}, "message": {{}}}}'.encode())
    assert out.answer.error == "schema"
    assert out.answer.request_id is None


def test_blank_request_id_on_a_good_body_is_none():
    assert parse_response(body(text_block(), request_id="   ")).request_id is None


# --- A2.2 verifier: extreme but real-shaped bodies ------------------------------------------------


def test_latency_int_too_large_for_a_float_is_none_not_a_crash():
    meta = '"response_metadata": {'
    raw = json.dumps(body(text_block(), sql_block()))
    raw = raw.replace(meta, meta + '"analyst_latency_ms": ' + "9" * 400 + ", ", 1)
    assert "9" * 400 in raw
    assert parse_response(raw).analyst_latency_ms is None
    out, _, ex = run(raw.encode())  # reaches the executor; the run is not lost to a crash
    assert out.answer.error is None and len(ex.calls) == 1 and out.cortex_messages == 1


def test_deeply_nested_body_is_a_schema_error_not_a_crash():
    raw = b"[" * 100_000 + b"]" * 100_000
    with pytest.raises(CortexSchemaError, match="RecursionError"):
        parse_response(raw)
    out, _, ex = run(raw)
    assert out.answer.error == "schema" and out.cortex_messages == 1 and ex.calls == []


def test_many_suggestions_are_deduplicated_in_order_and_quickly():
    many = [f"s{i % 50_000}" for i in range(100_000)]
    resp = parse_response(json.dumps(body(text_block(), suggestions_block(*many))))
    assert resp.suggestions == tuple(f"s{i}" for i in range(50_000))
