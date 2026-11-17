"""B3.1 synthetic labelling: the bundled example, or any copy of it, is stamped SYNTHETIC on
every surface, whatever its run id says and whichever entry point renders it; a real-looking run
is not. `render.looks_synthetic` is checked one giveaway at a time (L-2)."""

from __future__ import annotations

import json
import re

import pytest

from agentic_analytics.report import load_example, render_html, render_report, vega_lite_specs
from agentic_analytics.report.render import looks_synthetic
from agentic_analytics.schemas import Record, RunSummary
from tests.report.helpers import section, summary

REAL = {  # distinct, real-looking digests (not the example's, not one repeated character)
    "config_hash": "1f2e3d4c5b6a79880716253443526170ffeeddccbbaa99887766554433221100",
    "snapshot_sha256": "96f93c64aa0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7c56a",
    "gold_check_id": "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9",
}


def _snap(records: list[Record], snapshot: str) -> list[Record]:
    return [
        r.model_copy(update={"answer": r.answer.model_copy(update={"model_snapshot": snapshot})})
        for r in records
    ]


def _real_looking() -> tuple[RunSummary, list[Record]]:
    """The example renamed, re-hashed and with real model names: no giveaway left."""
    s, records, _ = load_example()
    s = s.model_copy(update={"run_id": "r-20261201-real", **REAL})
    records = [
        r.model_copy(update={"run_id": "r-20261201-real", "question_hash": REAL["config_hash"]})
        for r in records
    ]
    names = {"claude_mcp": "claude-sonnet-5-5", "gpt_mcp": "gpt-6.1-sol"}
    records = [
        r.model_copy(
            update={
                "answer": r.answer.model_copy(
                    update={"model_snapshot": names.get(r.backend, "claude-sonnet-4-6")}
                ),
                **{k: REAL["config_hash"] for k in ("prompt_hash", "fix_hash", "semantic_hash")},
                "gold_check_id": REAL["gold_check_id"],
                "snapshot_sha256": REAL["snapshot_sha256"],
            }
        )
        for r in records
    ]
    return s, records


def test_a_real_looking_run_is_not_synthetic():
    s, records = _real_looking()
    assert not looks_synthetic(s, records)
    assert not looks_synthetic(s, records, placeholder_hashes=False)
    page = render_html(s, records)
    assert "SYNTHETIC" not in page
    assert vega_lite_specs(s)["synthetic"] is False


@pytest.mark.parametrize(
    "giveaway",
    ["run_id", "fingerprint", "model_snapshot", "summary_placeholder", "record_placeholder"],
)
def test_looks_synthetic_catches_each_giveaway_alone(giveaway):
    s, records = _real_looking()
    stamped = giveaway in ("run_id", "fingerprint", "model_snapshot")
    if giveaway == "run_id":
        s = s.model_copy(update={"run_id": "example-synthetic"})
    elif giveaway == "fingerprint":
        ex, _, _ = load_example()
        s = s.model_copy(
            update={k: getattr(ex, k) for k in ("config_hash", "snapshot_sha256", "gold_check_id")}
        )
    elif giveaway == "model_snapshot":
        records = records[:1] + _snap(records[1:2], "example-agent-b") + records[2:]
    elif giveaway == "summary_placeholder":
        s = s.model_copy(update={"gold_check_id": "a" * 64})
    else:
        records = [*records[:-1], records[-1].model_copy(update={"prompt_hash": "0" * 64})]
    assert looks_synthetic(s, records)
    # the page stamps on the first three only: test fixtures use placeholder hashes too
    assert looks_synthetic(s, records, placeholder_hashes=False) is stamped


def test_one_fingerprint_hash_changed_is_no_longer_the_example():
    ex, _, _ = load_example()
    s, records = _real_looking()
    s = s.model_copy(update={"config_hash": ex.config_hash, "snapshot_sha256": ex.snapshot_sha256})
    assert not looks_synthetic(s, records)  # all three hashes must match the example's


def _labelled_everywhere(page: str, specs: dict) -> None:
    assert page.count('<p class="synthetic" role="note">SYNTHETIC EXAMPLE:') == 1
    assert re.search(r"<title>SYNTHETIC EXAMPLE · ", page)
    titles = re.findall(r'<title id="[^"]+-t">([^<]+)</title>', page)
    assert titles and all(t.startswith("SYNTHETIC EXAMPLE · ") for t in titles)
    assert specs["synthetic"] is True and specs["description"].startswith("SYNTHETIC EXAMPLE")
    for name in ("accuracy_by_category", "uplift", "cost_per_correct"):
        assert specs[name]["description"].startswith("SYNTHETIC EXAMPLE · ")


def test_a_renamed_example_cannot_drop_the_label(tmp_path):
    """Planted defect: `example=False` on a copy of the example (renamed, as a real run would
    be) still renders SYNTHETIC on the page, the charts and summary.vl.json."""
    s, records, questions = load_example()
    renamed = s.model_copy(update={"run_id": "r-20261201-baseline"})
    path = render_report(renamed, records, tmp_path / "a", questions=questions, example=False)
    specs = json.loads((tmp_path / "a" / "summary.vl.json").read_text("utf-8"))
    _labelled_everywhere(path.read_text("utf-8"), specs)
    # the pure entry points stamp it too, not only render_report
    _labelled_everywhere(render_html(renamed, records), vega_lite_specs(renamed))
    # re-hashed, but its records still name the example's fictional models
    rehashed = renamed.model_copy(update=REAL)
    path = render_report(rehashed, records, tmp_path / "b")
    specs = json.loads((tmp_path / "b" / "summary.vl.json").read_text("utf-8"))
    _labelled_everywhere(path.read_text("utf-8"), specs)


def test_test_fixtures_with_placeholder_hashes_are_not_stamped():
    page = render_html(summary(), None)
    assert "SYNTHETIC" not in section(page, "categories") and 'class="synthetic"' not in page
