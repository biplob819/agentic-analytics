"""N1.1 verifier round 2: each test isolates one invariant so deleting it alone fails a test."""

import copy
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentic_analytics.schemas import FixUplift, GoldResult, ResultSet, load_run_config
from agentic_analytics.schemas._base import FrozenDict
from tests.schemas.factories import (
    IV,
    SQL,
    T0,
    H,
    ambiguous,
    answer,
    execution,
    pack,
    record,
    scores,
)


@pytest.mark.parametrize("cell", [Decimal("1.10"), date(2024, 1, 1), datetime(2024, 1, 1), b"x"])
def test_result_cells_must_be_json_scalars(cell):
    rs = ResultSet(columns=("v",), rows=((cell,),))
    with pytest.raises(ValidationError):
        execution(result=rs, rows_total=1)
    with pytest.raises(ValidationError):
        GoldResult(
            schema_version="1.0",
            question_id="q-001",
            engine="clickhouse",
            sql_hash=H,
            result=rs,
            elapsed_ms=1,
            executed_at=T0,
        )


def test_sql_answer_needs_an_execution_even_when_unmatched():
    s = scores(result_match=False, correct=False, answer_from_rows=None)
    with pytest.raises(ValidationError, match="execution is present"):
        record(execution=None, scores=s)


def test_clarified_must_match_the_answer_even_when_consistent_alone():
    s = scores(result_match=False, clarified=True, false_clarification=True, correct=False)
    with pytest.raises(ValidationError, match="clarified"):
        record(scores=s)


def test_answer_from_rows_needs_executed_rows():
    clar = answer(sql=None, answer="", clarification="Which year?")
    s = scores(result_match=None, clarified=True, correct=True, answer_from_rows=True)
    with pytest.raises(ValidationError, match="answer_from_rows"):
        record(question_id="q-090", answer=clar, execution=None, scores=s)


@pytest.mark.parametrize("kw", [{"result_match": "true"}, {"correct": 1}, {"clarified": 0}])
def test_score_booleans_are_strict(kw):
    with pytest.raises(ValidationError):
        scores(**kw)


def test_p_value_is_strict():
    with pytest.raises(ValidationError):
        FixUplift(
            backend="claude_mcp",
            fix="examples",
            delta_points=IV,
            p_value=True,  # type: ignore[arg-type]
            separated=False,
        )


def test_interpretations_differing_only_in_spacing_or_case_are_one():
    spaced = {k: "  " + v.replace(" ", "   ") + "\n" for k, v in SQL.items()}
    with pytest.raises(ValidationError):
        ambiguous(interpretations=[SQL, spaced])


def _yaml(tmp_path, text):
    p = tmp_path / "run.yaml"
    p.write_text(text, encoding="utf-8")
    return load_run_config(p)


BASE = "backends: [claude_mcp]\nas_of: 2026-09-30\n"


def test_explicit_keys_override_merged_ones(tmp_path):
    c = _yaml(tmp_path, BASE + "<<: {trials: 4}\ntrials: 5\nmodels: {claude_mcp: x}\n")
    assert c.trials == 5


@pytest.mark.parametrize(
    "text",
    [
        BASE + "models: {claude_mcp: x}\n<<: {trials: 1, trials: 2}\n",
        BASE + "models: {claude_mcp: x}\n<<: {trials: 1}\n<<: {seed: 2}\n",
        BASE + "models:\n  <<: [{claude_mcp: a, claude_mcp: b}]\n",
    ],
)
def test_duplicates_inside_merges_are_errors(tmp_path, text):
    from agentic_analytics.schemas import ConfigError

    with pytest.raises(ConfigError):
        _yaml(tmp_path, text)


def test_case_differences_make_distinct_interpretations():
    leeds = {k: v + " WHERE town = 'LEEDS'" for k, v in SQL.items()}
    lower = {k: v + " WHERE town = 'Leeds'" for k, v in SQL.items()}
    assert len(ambiguous(interpretations=[leeds, lower]).interpretations) == 2


def test_result_match_needs_a_successful_execution_alone():
    failed = execution(result=None, error="sql_error", rows_total=None)
    s = scores(result_match=True, correct=True, answer_from_rows=None)
    with pytest.raises(ValidationError, match="result_match needs"):
        record(execution=failed, scores=s)


def test_answer_from_rows_is_strict():
    with pytest.raises(ValidationError):
        scores(answer_from_rows="yes")


def test_source_url_with_spaces_is_rejected():
    with pytest.raises(ValidationError):
        pack(source_url="https://example.com/a b")


def test_first_merged_mapping_wins(tmp_path):
    c = _yaml(tmp_path, BASE + "models:\n  <<: [{claude_mcp: a}, {claude_mcp: b}]\n")
    assert c.models["claude_mcp"] == "a"


@pytest.mark.parametrize(
    "url", ["https://example.com/data?x=1", "https://example.com/p#frag", "http://a.b:8080/x"]
)
def test_ordinary_source_urls_are_accepted(url):
    assert pack(source_url=url).source_url == url


@pytest.mark.parametrize("url", ["https://.", "https://-", "https://localhost", "https://a..b"])
def test_hostless_source_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        pack(source_url=url)


@pytest.mark.parametrize("kw", [{"name": "UK"}, {"tables": ["a", " a"]}, {"tables": [" a"]}])
def test_pack_names_and_tables(kw):
    with pytest.raises(ValidationError):
        pack(**kw)


def test_deepcopy_keeps_mappings_frozen():
    r = record()
    c = copy.deepcopy(r)
    assert isinstance(c.tokens, FrozenDict)
    with pytest.raises(TypeError):
        c.tokens["in"] = 1  # type: ignore[index]
