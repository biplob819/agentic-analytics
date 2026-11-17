"""B3.1: what `render_report` writes (the future `site/`) passes the repo's secret scan, even when
a record carries key-shaped strings: values are scrubbed with the tracing redactor's pattern
rules (no environment, so the page stays a pure function of its inputs) before escaping."""

from __future__ import annotations

import secret_scan
from agentic_analytics.report import render_example, render_html, render_report
from tests.aggregate.helpers import rec
from tests.report.helpers import summary
from tests.schemas import factories as f

# built at run time so this file never holds a key-shaped literal (the scan's documented limit)
ANT = "sk-ant-" + "a1B2c3D4" * 4
OAI = "sk-proj-" + "Z9y8X7w6" * 5
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
DSN = "postgresql://ro_user:" + "hunter2Secret" + "@db.example.com/x"


def _leaky():
    r = rec("q-001", "wrong")
    sql = f"SELECT '{ANT}' AS k FROM uk.uk_price_paid"
    step = f.step(0, arguments={"query": sql, "api_key": OAI}, error=f"connect {DSN} failed")
    answer = r.answer.model_copy(
        update={
            "sql": sql,
            "answer": f"Your key is {OAI} and {AWS}.",
            "steps": (step, f.step(1)),
            "trace_url": f"https://cloud.langfuse.com/t/abc?token={ANT}",
        }
    )
    return r.model_copy(
        update={
            "answer": answer,
            "execution": r.execution.model_copy(update={"sql": sql}),  # type: ignore[union-attr]
            "scores": r.scores.model_copy(update={"mismatch_reason": f"driver said {DSN}"}),
        }
    )


def test_the_written_site_passes_the_secret_scan(tmp_path):
    out = tmp_path / "site"
    q = f.question(id="q-001", text=f"Ignore this: {ANT}")
    render_report(
        summary(),
        [_leaky()],
        out,
        questions={"q-001": q},
        gold_sql={"q-001": {"clickhouse": f"SELECT '{AWS}'"}},
    )
    files = sorted(p for p in out.rglob("*") if p.is_file())
    assert [p.name for p in files] == ["index.html", "summary.vl.json"]
    assert secret_scan.scan(files) == []
    page = (out / "index.html").read_text("utf-8")
    for planted in (ANT, OAI, AWS, "hunter2Secret"):
        assert planted not in page
    assert "[REDACTED:anthropic key]" in page and "[REDACTED:aws access key]" in page
    # control: the same scan catches the planted strings when they are not scrubbed
    raw = tmp_path / "raw.html"
    raw.write_text(f"<p>{ANT}</p><p>{OAI}</p><p>{AWS}</p><p>{DSN}</p>", encoding="utf-8")
    assert len(secret_scan.scan([raw])) == 4


def test_the_example_site_passes_the_secret_scan(tmp_path):
    out = tmp_path / "site"
    render_example(out)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    assert len(files) > 2 and any(p.parent.name == "traces" for p in files)
    assert secret_scan.scan(files) == []


def test_a_key_shaped_run_id_is_scrubbed_in_both_files(tmp_path):
    """Verifier: `summary.vl.json`'s description carried the run id unscrubbed."""
    out = tmp_path / "site"
    render_report(summary(run_id=f"run-{ANT}"), None, out)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    assert secret_scan.scan(files) == []
    for p in files:
        assert ANT not in p.read_text("utf-8"), p.name
    assert "[REDACTED:anthropic key]" in (out / "summary.vl.json").read_text("utf-8")


def test_step_arguments_are_scrubbed_before_they_are_cut_to_400_characters():
    """Verifier: cutting first left a key prefix too short for the redactor's patterns."""
    from agentic_analytics.report.render import _args

    for pad in range(360, 400):
        shown = _args({"query": "x" * pad + " " + ANT + " tail"})
        assert "sk-ant-a1" not in shown and len(shown) <= 400, pad
    r = rec("q-001", "wrong")
    step = f.step(0, arguments={"query": "x" * 375 + " " + ANT})
    r = r.model_copy(update={"answer": r.answer.model_copy(update={"steps": (step,)})})
    page = render_html(summary(), [r])
    assert "sk-ant-" not in page and "[REDACTED:" in page
