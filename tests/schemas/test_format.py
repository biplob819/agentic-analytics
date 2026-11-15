"""SCH-06: results format v1."""

import copy
import json
import pickle
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import agentic_analytics.schemas as schemas
from agentic_analytics.schemas import DatasetPack, Probe, Question, Record, RunConfig, RunSummary
from harness_lib import ROOT
from tests.schemas.factories import question, record

FIXTURES = ROOT / "tests" / "fixtures"
TYPES = {
    "question.json": Question,
    "probe.json": Probe,
    "record.json": Record,
    "run_summary.json": RunSummary,
    "run_config.json": RunConfig,
    "pack.json": DatasetPack,
}
# Independent of __all__ (EK L-10): every class in Tech Spec \u00a73.
SPEC_SCHEMAS = {
    "Tolerance", "Question", "Probe", "GoldResult", "AgentStep", "Execution",
    "AnalyticsAnswer", "Scores", "Record", "CategoryAccuracy", "BackendSummary",
    "FixUplift", "RunSummary", "RunConfig", "DatasetPack",
}  # fmt: skip


@pytest.mark.contract
@pytest.mark.parametrize("name", sorted(TYPES))
def test_fixtures_round_trip_losslessly_and_stably(name):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    obj = TYPES[name].model_validate_json(text)
    again = obj.model_dump_json(indent=2) + "\n"
    assert again == text
    assert TYPES[name].model_validate_json(again) == obj


@pytest.mark.contract
def test_every_tech_spec_schema_is_exported():
    spec = (ROOT / "Agentic Analytics Tech Spec.md").read_text(encoding="utf-8")
    section = spec[spec.index("## 3. Schemas") : spec.index("## 4. Interfaces")]
    in_spec = set(re.findall(r"^class (\w+)\(", section, re.M))
    assert in_spec == SPEC_SCHEMAS
    missing = [n for n in SPEC_SCHEMAS if n not in schemas.__all__ or not hasattr(schemas, n)]
    assert not missing


@pytest.mark.parametrize("version", ["2.0", "1", "v1.0", ""])
def test_schema_version_major_must_be_one(version):
    with pytest.raises(ValidationError):
        question(schema_version=version)
    assert question(schema_version="1.7").schema_version == "1.7"


def test_objects_are_deeply_immutable():
    r = record()
    with pytest.raises(ValidationError):
        r.trial = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        r.tokens["in"] = 5  # type: ignore[index]
    with pytest.raises(TypeError):
        r.answer.steps[0].arguments["query"] = "DROP"  # type: ignore[index]
    assert hash(r.tokens)


def test_nested_json_is_frozen_too():
    from tests.schemas.factories import step

    s = step(arguments={"a": [1, {"b": [2]}]})
    with pytest.raises(AttributeError):
        s.arguments["a"].append(3)  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        s.arguments["a"][1]["b"] = 0  # type: ignore[index]


@pytest.mark.parametrize(
    "value", [datetime(2026, 1, 1, tzinfo=UTC), b"x", {1: 2}, object(), {"a": float("nan")}]
)
def test_json_values_must_survive_a_round_trip(value):
    from tests.schemas.factories import step

    with pytest.raises(ValidationError):
        step(arguments={"v": value})


def test_records_pickle_and_deepcopy():
    r = record()
    assert pickle.loads(pickle.dumps(r)) == r
    assert copy.deepcopy(r) == r
    assert r.model_copy(deep=True) == r
    assert hash(pickle.loads(pickle.dumps(r.tokens))) == hash(r.tokens)


@pytest.mark.parametrize("kw", [{"cost_usd": True}, {"seed": "7"}, {"seed": True}])
def test_numbers_are_strict(kw):
    with pytest.raises(ValidationError):
        record(**kw)


def test_non_finite_floats_are_rejected():
    with pytest.raises(ValidationError):
        record(cost_usd=float("nan"))
    with pytest.raises(ValidationError):
        record(warehouse_credits_alloc=float("inf"))


def test_sets_are_rejected_in_free_form_json():
    from tests.schemas.factories import step

    with pytest.raises(ValidationError):
        step(arguments={"x": {1, 2}})


def test_fixtures_are_utf8_json_objects():
    for name in TYPES:
        assert isinstance(json.loads((FIXTURES / name).read_text(encoding="utf-8")), dict)
    assert Path(FIXTURES).is_dir()
