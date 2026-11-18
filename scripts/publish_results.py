#!/usr/bin/env python3
"""Publish a finished run as the project's headline (B3.2: REL-01, REL-02, REL-04).

    uv run python scripts/publish_results.py results/<run id>

Renders the run's dashboard into site/ (index.html, summary.vl.json, summary.json and the
exported traces), copies the run's summary.json and run.json into docs/results/ (the committed
record every README number is regenerated from), draws the write-up figures into docs/assets/
(headline.svg, categories.svg and, when fixes ran, uplift.svg), writes the run-history trend
page site/trend.html from the committed run ledger docs/measurements/runs.jsonl (V5.1 MON-07,
linked from the dashboard), and regenerates the README's
generated blocks (headline, results) between their markers. Every number comes from the run's
summary.json; nothing is typed by hand. The Pages workflow (.github/workflows/pages.yml, run by
hand) deploys site/.

    uv run python scripts/publish_results.py --from-record

renders site/ from the committed record alone (docs/results/summary.json and run.json, OWNER
D-111): the dashboard with the bundled pack's credit and the stamped region, but no per-answer
records or traces, so its failure explorer is empty. The Pages workflow runs this, so nothing
large is committed; it writes nothing outside site/.

It refuses an unfinished run and the bundled SYNTHETIC example (also when renamed: its
fingerprint, `example-` model snapshots and placeholder hashes give it away), and removes site/
again if the secret scan flags anything in it, exported traces included. `--example --root DIR`
renders the example into a scratch copy of the repository, for previews and tests only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = "site"
ASSETS = "docs/assets"
RECORD = "docs/results"  # the published run's summary.json and run.json, committed
HISTORY = "docs/measurements/runs.jsonl"  # EvalKit's run ledger (V5.1 MON-07), committed
TREND = "trend.html"  # the run-history trend page written beside the dashboard
GATES = "monitoring/gates.yaml"


def _trend(root: Path, site: Path) -> str | None:
    """V5.1 MON-07: EvalKit's trend page from the committed run ledger into site/trend.html
    (accuracy with intervals, cost per correct, p95 latency; alarm bands and flags); the link
    the dashboard shows, or None when there is no ledger."""
    from agentic_analytics.monitoring.history import trend_page

    ledger = root / HISTORY
    if not ledger.is_file():
        return None
    gates = root / GATES
    trend_page(ledger, site / TREND, gates=gates if gates.is_file() else None)
    return TREND


def _example_run(scratch: Path) -> Path:
    """Write the bundled example as a run folder (summary, records, questions, traces)."""
    from agentic_analytics.report.example import example_root

    src = example_root()
    run = scratch / "example-synthetic"
    (run / "traces").mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "records.jsonl", "questions.jsonl"):
        (run / name).write_text(
            src.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
    (run / "run.json").write_text(
        '{"pack_source": "uk_price_paid"}\n', encoding="utf-8", newline="\n"
    )
    for t in sorted(src.joinpath("traces").iterdir(), key=lambda t: t.name):
        if t.name.endswith(".json"):
            (run / "traces" / t.name).write_bytes(t.read_bytes())
    return run


def scan_site(site: Path) -> list[str]:
    """Secret-scan every file in the rendered site, traces included (paths relative to it)."""
    from secret_scan import expand, scan

    return [hit.replace(str(site) + os.sep, "", 1) for hit in scan(expand([site]))]


def _credit_pack_region(summary, manifest: dict):
    """The licence credit, the bundled pack (only at the summary's exact version) and the
    stamped Snowflake region that words the OWNER D-62 Cortex latency caveat."""
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.report import resolve_credit
    from agentic_analytics.run.runner import stamped_region

    try:
        pack = load_pack(manifest.get("pack_source") or summary.pack.partition("@")[0]).manifest
    except (PackError, OSError):
        pack = None
    if pack is not None and pack.ref != summary.pack:
        pack = None  # never credit a different version of the data
    credit, pack = resolve_credit(summary, pack, None)
    return credit, pack, stamped_region(manifest)


def _write_scanned_site(site: Path, summary_text: str) -> None:
    """Add summary.json to the rendered site and secret-scan it; remove it on any hit."""
    (site / "summary.json").write_text(summary_text, encoding="utf-8", newline="\n")
    leaks = scan_site(site)
    if leaks:
        shutil.rmtree(site)  # never leave a deployable site holding a key
        raise SystemExit(
            "refusing to publish: the secret scan flagged the site (removed):\n  "
            + "\n  ".join(leaks)
        )


def publish_from_record(root: Path) -> str:
    """OWNER D-111: render `root/site/` from the committed record `root/docs/results/` only."""
    from agentic_analytics.report import render_report
    from agentic_analytics.report.render import looks_synthetic
    from agentic_analytics.schemas import RunSummary

    record = root / RECORD
    summary_text = (record / "summary.json").read_text(encoding="utf-8")
    summary = RunSummary.model_validate_json(summary_text)
    manifest = json.loads((record / "run.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit(f"refusing to publish: {RECORD}/run.json is not an object")
    if summary.finished_at is None:
        raise SystemExit(f"refusing to publish {summary.run_id}: the run has not finished")
    if looks_synthetic(summary):
        raise SystemExit("refusing to publish the SYNTHETIC example as results")
    _credit, pack, region = _credit_pack_region(summary, manifest)
    site = root / SITE
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    trend = _trend(root, site)
    render_report(summary, None, site, pack=pack, snowflake_region=region, trend_href=trend)
    _write_scanned_site(site, summary_text)
    return (
        f"rendered {SITE}/index.html from {RECORD}/ ({summary.run_id}); "
        "no per-answer records or traces"
    )


def publish(run_dir: Path, root: Path, *, allow_example: bool = False) -> str:
    """Publish `run_dir` into the repository at `root`; return a one-line summary."""
    from agentic_analytics.report import render_report
    from agentic_analytics.report.figures import writeup_figures
    from agentic_analytics.report.readme import update_readme
    from agentic_analytics.report.render import looks_synthetic
    from agentic_analytics.run.runner import load_run
    from agentic_analytics.schemas import RunSummary

    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    if summary.finished_at is None:
        raise SystemExit(f"refusing to publish {summary.run_id}: the run has not finished")
    manifest, records, questions = load_run(run_dir)
    # a renamed copy of the example is still the example: fingerprint, snapshots, hashes
    if looks_synthetic(summary, records) and not allow_example:
        raise SystemExit("refusing to publish the SYNTHETIC example as results")
    credit, pack, region = _credit_pack_region(summary, manifest)
    config = manifest.get("config")
    models = config.get("models") if isinstance(config, dict) else None

    site = root / SITE
    if site.exists():
        shutil.rmtree(site)  # generated output only: stale traces must not linger
    site.mkdir(parents=True)
    trend = None if allow_example else _trend(root, site)
    render_report(
        summary,
        records,
        site,
        questions=questions,
        pack=pack,
        example=allow_example,
        snowflake_region=region,
        trend_href=trend,
    )
    traces = run_dir / "traces"
    if traces.is_dir():
        (site / "traces").mkdir(exist_ok=True)
        for t in sorted(traces.glob("*.json")):
            shutil.copyfile(t, site / "traces" / t.name)
    _write_scanned_site(site, (run_dir / "summary.json").read_text(encoding="utf-8"))

    assets = root / ASSETS
    assets.mkdir(parents=True, exist_ok=True)
    figures = writeup_figures(summary, credit)
    for stale in ("headline.svg", "categories.svg", "uplift.svg"):
        if stale not in figures:
            (assets / stale).unlink(missing_ok=True)
    for name, svg in figures.items():
        (assets / name).write_text(svg, encoding="utf-8", newline="\n")
    record = root / RECORD
    record.mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "run.json"):
        src = run_dir / name
        if src.is_file():
            (record / name).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
            )
    changed = update_readme(
        root / "README.md",
        summary,
        models=models if isinstance(models, dict) else None,
        snowflake_region=region,
    )
    return (
        f"published {summary.run_id}: {SITE}/index.html, {RECORD}/summary.json, "
        f"{len(figures)} figures in {ASSETS}/, "
        f"README headline {'updated' if changed else 'unchanged'}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("run_dir", type=Path, nargs="?", help="a finished run folder")
    ap.add_argument("--root", type=Path, default=None, help="repository root (default: this one)")
    ap.add_argument(
        "--example",
        action="store_true",
        help="publish the bundled SYNTHETIC example into --root (never this repository)",
    )
    ap.add_argument(
        "--from-record",
        action="store_true",
        help="render site/ from the committed docs/results/ record only (OWNER D-111)",
    )
    args = ap.parse_args(argv)
    root = (args.root or ROOT).resolve()
    if sum([args.example, args.from_record, args.run_dir is not None]) != 1:
        ap.error("give exactly one of a run folder, --example or --from-record")
    try:
        if args.from_record:
            print(publish_from_record(root))
        elif args.example:
            if args.root is None or root == ROOT.resolve():
                ap.error("--example needs --root pointing at a scratch copy, not this repository")
            print(publish(_example_run(root / ".example-run"), root, allow_example=True))
        else:
            print(publish(args.run_dir, root))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise
    except Exception as exc:  # a broken run folder: say what, exit 2
        print(f"cannot publish: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
