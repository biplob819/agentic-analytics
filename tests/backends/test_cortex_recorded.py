"""Cortex Analyst replies recorded from the live API (A2.2 live step, 2026-11-16, EK L-17).

The bodies in `fixtures/cortex/` are real replies to bank questions, a write probe, an
unanswerable question and three error requests, with request ids redacted. They showed two
things the documented shape did not: Cortex writes semantic SQL (`SELECT * FROM SEMANTIC_VIEW(
... METRICS ... DIMENSIONS ... WHERE ...)`, `is_semantic_sql`), which the Snowflake guard refused
until this step, and it declines (writes, missing data) with text plus suggestions rather than
text alone. Error bodies carry the code as `error_code` (400, 404) or `code` (401).
"""

from __future__ import annotations

import asyncio
import json
import math
import re

import httpx2
import pytest
import sqlglot
from sqlglot import expressions as exp

from agentic_analytics.backends.cortex import CortexHTTPError, ask_cortex, parse_response
from agentic_analytics.models.errors import classify_error
from agentic_analytics.warehouse.guard import _semantic_view_problem, _token_problem, check_sql
from tests.backends.cortex_fakes import (
    Api,
    FakeExecutor,
    body,
    execution,
    make_client,
    recorded,
    run,
    sql_block,
    text_block,
)

SQL_REPLIES = {
    "sql_aggregation_q001": 2651.0,
    "sql_time_q021": 2606.0,
    "sql_geography_q042": 2553.0,
    "sql_top_n_q062": 2622.0,
    "sql_yoy_cte_q082": 3658.0,
    "sql_ambiguous_q101": 4956.0,
    "sql_ambiguous_q103": 3048.0,
}
SUGGESTION_REPLIES = ("suggestions_write_probe_pw01", "suggestions_unanswerable_bedrooms")
REDACTED_ID = re.compile(r"00000000-0000-4000-8000-\d{12}")
MODEL = "claude-sonnet-4-6"  # the US model reached through cross-region inference (OWNER D-62)


def _sql(name: str) -> str:
    sql = parse_response(recorded(name)).sql
    assert sql is not None
    return sql


# --- parser over the real shapes (CTX-02) --------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SQL_REPLIES))
def test_recorded_sql_replies_parse(name):
    resp = parse_response(recorded(name))
    assert resp.sql is not None and "SEMANTIC_VIEW(" in resp.sql
    assert resp.sql.lstrip().upper().startswith(("SELECT", "WITH"))
    assert resp.text is not None
    assert resp.text.startswith("This is our interpretation of your question:")
    assert resp.model_names == (MODEL,)
    assert resp.model_snapshot == MODEL
    assert resp.question_category == "CLEAR_SQL"
    assert resp.suggestions == ()
    assert resp.verified_query_used is None
    assert resp.warnings == ()
    assert resp.ignored == ()  # every real content block is understood
    assert resp.request_id is not None and REDACTED_ID.fullmatch(resp.request_id)
    assert resp.analyst_latency_ms == SQL_REPLIES[name]


@pytest.mark.parametrize("name", SUGGESTION_REPLIES)
def test_recorded_declines_are_text_plus_suggestions(name):
    resp = parse_response(recorded(name))
    assert resp.sql is None
    assert resp.text is not None and resp.text.startswith("I'm sorry")
    assert len(resp.suggestions) == 3
    assert resp.question_category == "UNCLEAR"
    assert resp.ignored == ()


@pytest.mark.parametrize(
    ("took", "expected"),
    [
        (2651.0, 2651.0),
        (0, 0.0),
        (812, 812.0),
        (-1.0, None),
        (math.nan, None),
        (math.inf, None),
        (True, None),
        ("2651", None),
        (None, None),
    ],
)
def test_analyst_latency_is_kept_only_when_finite_and_non_negative(took, expected):
    b = body(text_block(), sql_block())
    b["response_metadata"]["analyst_latency_ms"] = took
    got = parse_response(json.dumps(b, allow_nan=True)).analyst_latency_ms
    assert got == expected and (got is None or isinstance(got, float))


# --- the Snowflake guard accepts Cortex's semantic SQL (fix found live) -------------------------


@pytest.mark.parametrize("name", sorted(SQL_REPLIES))
def test_recorded_cortex_sql_passes_the_snowflake_guard(name):
    assert check_sql(_sql(name), "snowflake").ok


def test_semantic_view_as_a_join_source_passes():
    assert check_sql(
        "SELECT 1 FROM t JOIN SEMANTIC_VIEW(a.b.c METRICS m) s ON TRUE", "snowflake"
    ).ok


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (
            "SELECT * FROM SEMANTIC_VIEW('@st/f.csv' METRICS a)",
            "SEMANTIC_VIEW must name a semantic view (DATABASE.SCHEMA.VIEW)",
        ),
        (
            "SELECT * FROM SEMANTIC_VIEW(@stg METRICS m)",
            "SEMANTIC_VIEW must name a semantic view (DATABASE.SCHEMA.VIEW)",
        ),
        (
            "SELECT * FROM SEMANTIC_VIEW(IDENTIFIER('a.b.c') METRICS m)",
            "SEMANTIC_VIEW must name a semantic view (DATABASE.SCHEMA.VIEW)",
        ),
        (
            "SELECT SEMANTIC_VIEW(a.b.c METRICS m)",
            "SEMANTIC_VIEW is allowed only as a FROM/JOIN source",
        ),
        (
            "SELECT * FROM SEMANTIC_VIEW(a.b.c METRICS AI_COMPLETE('m', 'x'))",
            "function ai_complete is not allowed",
        ),
        (
            "SELECT * FROM SEMANTIC_VIEW(a.b.c METRICS m WHERE b IN "
            "(SELECT * FROM TABLE(RESULT_SCAN('01b0'))))",
            "table function result_scan is not allowed",
        ),
    ],
)
def test_semantic_view_rules_each_refuse_alone(sql, reason):
    verdict = check_sql(sql, "snowflake")
    assert not verdict.ok and verdict.reason == reason


VIEW_NAME = "SEMANTIC_VIEW must name a semantic view (DATABASE.SCHEMA.VIEW)"


@pytest.mark.parametrize(
    ("sql", "reason"),
    [  # A2.2 verifier: each passed the builder's guard
        ("SELECT * FROM SEMANTIC_VIEW(IDENTIFIER('a.b.c').x METRICS m)", VIEW_NAME),
        ("SELECT * FROM SEMANTIC_VIEW(a.IDENTIFIER('b').c METRICS m)", VIEW_NAME),
        ("SELECT * FROM SEMANTIC_VIEW(a.b.c AT(OFFSET => -60) METRICS m)", VIEW_NAME),
        ("SELECT * FROM SEMANTIC_VIEW(c METRICS m)", VIEW_NAME),
        ("SELECT * FROM SEMANTIC_VIEW(b.c METRICS m)", VIEW_NAME),
        ("SELECT * FROM SEMANTIC_VIEW(a..c METRICS m)", VIEW_NAME),
        ('SELECT * FROM SEMANTIC_VIEW("".b.c METRICS m)', VIEW_NAME),
        (
            "SELECT * FROM db.s.SEMANTIC_VIEW(a.b.c METRICS m)",
            "schema-qualified (user-defined or external) functions are not allowed",
        ),
        (
            "SELECT * FROM t JOIN s.SEMANTIC_VIEW(a.b.c METRICS m) ON TRUE",
            "schema-qualified (user-defined or external) functions are not allowed",
        ),
        (
            'SELECT * FROM "semantic_view"(a.b.c METRICS m)',
            "SEMANTIC_VIEW must be the unquoted keyword",
        ),
        (
            'SELECT * FROM t, "SEMANTIC_VIEW"(a.b.c METRICS m)',
            "SEMANTIC_VIEW must be the unquoted keyword",
        ),
    ],
)
def test_semantic_view_name_and_keyword_must_be_plain(sql, reason):
    verdict = check_sql(sql, "snowflake")
    assert not verdict.ok and verdict.reason == reason


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM SEMANTIC_VIEW("ANALYTICS"."UK"."UK_PRICE_PAID_SEMANTIC" METRICS m)',
        "select * from semantic_view(analytics.uk.uk_price_paid_semantic metrics m) s",
        'SELECT "SEMANTIC_VIEW" FROM t',  # a quoted column of that name is not a call
    ],
)
def test_plain_semantic_view_forms_still_pass(sql):
    assert check_sql(sql, "snowflake").ok


def test_a_denied_function_inside_recorded_semantic_sql_is_still_refused():
    sql = _sql("sql_aggregation_q001").replace("= 2023", "= SYSTEM$WHITELIST()")
    verdict = check_sql(sql, "snowflake")
    assert not verdict.ok and verdict.reason == "function system$whitelist is not allowed"


def test_token_backstop_exempts_semantic_view_on_snowflake_only():
    sql = "SELECT * FROM semantic_view(a.b.c METRICS m)"
    tokens = sqlglot.tokenize(sql, read="clickhouse")
    problem = _token_problem(tokens, "clickhouse", sql)
    assert problem == "table function semantic_view is not allowed"
    assert _token_problem(sqlglot.tokenize(sql, read="snowflake"), "snowflake", sql) is None


def test_semantic_view_is_refused_on_clickhouse():
    tree = sqlglot.parse_one("SELECT * FROM SEMANTIC_VIEW(a.b.c METRICS m)", read="snowflake")
    node = tree.find(exp.SemanticView)
    assert node is not None
    assert (
        _semantic_view_problem(node, "clickhouse") == "SEMANTIC_VIEW is not allowed on this engine"
    )
    assert _semantic_view_problem(node, "snowflake") is None
    assert not check_sql("SELECT * FROM SEMANTIC_VIEW(a.b.c METRICS m)", "clickhouse").ok


# --- ask_cortex over the real replies (CTX-03..06) ----------------------------------------------


def test_recorded_single_cell_reply_runs_once_and_templates():
    sql = _sql("sql_aggregation_q001")
    ex = FakeExecutor(execution(((861782,),), ("SALES_COUNT",), sql=sql))
    out, api, _ = run(recorded("sql_aggregation_q001"), executor=ex)
    assert ex.calls == [(sql, 10_000)]
    assert len(api.requests) == 1
    assert out.answer.answer == "The result is 861782."
    assert out.answer.sql == sql
    assert out.answer.model_snapshot == MODEL
    assert out.answer.interpretation is not None
    assert out.answer.interpretation.startswith("This is our interpretation")
    assert out.answer.request_id is not None and REDACTED_ID.fullmatch(out.answer.request_id)
    assert out.answer.asked_for_clarification is False
    assert out.answer.error is None
    assert out.cortex_messages == 1


def test_recorded_multi_row_reply_templates_the_live_rows():
    rows = [
        (59855, "LONDON"),
        (14473, "MANCHESTER"),
        (13357, "BRISTOL"),
        (12081, "BIRMINGHAM"),
        (11665, "NOTTINGHAM"),
    ]
    sql = _sql("sql_top_n_q062")
    ex = FakeExecutor(execution(rows, ("SALES_COUNT", "TOWN"), sql=sql))
    out, _, _ = run(recorded("sql_top_n_q062"), executor=ex)
    assert out.answer.answer == "5 rows returned; first: SALES_COUNT: 59855, TOWN: LONDON"


@pytest.mark.parametrize("name", SUGGESTION_REPLIES)
def test_recorded_declines_become_clarifications_and_run_nothing(name):
    out, _, ex = run(recorded(name))
    assert ex.calls == []
    assert out.answer.sql is None
    assert out.answer.answer == ""
    assert out.answer.error is None
    assert out.answer.asked_for_clarification is True
    assert len(out.answer.suggestions) == 3
    assert out.answer.interpretation is not None
    assert out.answer.interpretation.startswith("I'm sorry")
    assert out.execution is None and out.warehouse_elapsed_ms == 0
    assert out.cortex_messages == 1


@pytest.mark.parametrize(
    ("name", "status", "code", "message_part", "has_id"),
    [
        ("http400_malformed_body", 400, "390400", "Last message must be a User message.", True),
        ("http404_no_such_view", 404, "390404", "does not exist or not authorized", True),
        ("http401_unregistered_key", 401, "390144", "JWT token is invalid", False),
    ],
)
def test_recorded_error_bodies_stop_the_run_after_one_request(
    name, status, code, message_part, has_id
):
    api = Api([httpx2.Response(status, content=recorded(name))])
    ex = FakeExecutor()
    with pytest.raises(CortexHTTPError) as info:
        asyncio.run(ask_cortex("How many sales in 2024?", make_client(api), ex))
    assert len(api.requests) == 1 and ex.calls == []
    err = info.value
    assert err.status_code == status
    assert err.code == code
    assert err.message is not None and message_part in err.message
    assert (err.request_id is not None and bool(REDACTED_ID.fullmatch(err.request_id))) is has_id
    assert classify_error(err) == "config"
