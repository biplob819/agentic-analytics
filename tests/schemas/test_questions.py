import pytest
from pydantic import ValidationError

from agentic_analytics.schemas import GoldResult, Tolerance
from tests.schemas.factories import SQL, T0, H, ambiguous, probe, question, result


def test_valid_question_and_ambiguous_parse():
    assert question().expected == "answer"
    assert ambiguous().gold_sql is None


@pytest.mark.parametrize("qid", ["q-\u0661\u0662\u0663", "q-\uff11\uff12\uff13"])
def test_ids_take_ascii_digits_only(qid):
    with pytest.raises(ValidationError):
        question(id=qid)
    with pytest.raises(ValidationError):
        probe(id="p-iq\u0660\u0661")


@pytest.mark.parametrize(
    "kw",
    [
        {"id": "q-1"},
        {"expected": "clarify"},  # answer category with clarify
        {"category": "ambiguous"},  # ambiguous with answer
        {"gold_sql": None},
        {"gold_sql": {"clickhouse": "SELECT 1"}},  # one engine
        {"gold_sql": {**SQL, "duckdb": "SELECT 1"}},  # extra engine
        {"interpretations": [SQL, SQL]},  # only ambiguous questions carry them
        {"top_n": 5},  # top_n on a non-top_n question
        {"text": "   "},
        {"surprise": 1},
    ],
)
def test_answer_question_invariants(kw):
    with pytest.raises(ValidationError):
        question(**kw)


@pytest.mark.parametrize(
    "kw",
    [
        {"gold_sql": SQL},
        {"interpretations": [SQL]},
        {"interpretations": [SQL, {"clickhouse": "SELECT 1"}]},
        {"interpretations": [SQL, SQL]},  # two identical readings are one reading
    ],
)
def test_ambiguous_question_invariants(kw):
    with pytest.raises(ValidationError):
        ambiguous(**kw)


def test_top_n_required_on_top_n_questions():
    with pytest.raises(ValidationError):
        question(category="top_n")
    assert question(category="top_n", top_n=10).top_n == 10


@pytest.mark.parametrize("kw", [{"rel": 1.0}, {"rel": -0.1}, {"abs": -1.0}, {"rel": float("nan")}])
def test_tolerance_bounds(kw):
    with pytest.raises(ValidationError):
        Tolerance(**kw)


@pytest.mark.parametrize(
    "kw",
    [
        {"id": "p-w01"},  # id does not match kind
        {"id": "p-x01"},
        {"injection_marker": None},
        {"expected": "refuse"},
        {"backends": []},
        {"backends": ["claude_mcp", "claude_mcp"]},
        {"kind": "injection_data", "id": "p-id01"},  # cortex listed on a data probe
    ],
)
def test_probe_invariants(kw):
    with pytest.raises(ValidationError):
        probe(**kw)


def test_write_probe_rules():
    ok = probe(
        id="p-w01", kind="write", expected="refuse", injection_marker=None, text="Delete 2024."
    )
    assert ok.kind == "write"
    with pytest.raises(ValidationError):
        probe(id="p-w02", kind="write", expected="refuse", text="Delete 2024.")  # marker set


def test_data_probe_without_cortex_is_valid():
    p = probe(kind="injection_data", id="p-id01", backends=["claude_mcp", "gpt_mcp"])
    assert "cortex_analyst" not in p.backends


def test_gold_result_holds_a_result_set():
    g = GoldResult(
        schema_version="1.0",
        question_id="q-001",
        engine="snowflake",
        sql_hash=H,
        result=result(),
        elapsed_ms=10,
        executed_at=T0,
    )
    assert g.result.columns == ("median_price", "sales")
    with pytest.raises(ValidationError):
        GoldResult.model_validate({**g.model_dump(), "engine": "duckdb"})
