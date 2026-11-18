#!/usr/bin/env python3
"""Publish a finished run as the project's headline (B3.2: REL-01, REL-02, REL-04).

    uv run python scripts/publish_results.py results/<run id>

Renders the run's dashboard into site/ (index.html, summary.vl.json, summary.json and the
exported traces), copies the run's summary.json and run.json into docs/results/ (the committed
record every README number is regenerated from), draws the write-up figures into docs/assets/
(headline.svg, categories.svg and, when fixes ran, uplift.svg) and regenerates the README's
generated blocks (headline, results) between their markers. Every number comes from the run's
summary.json; nothing is typed by hand. The Pages workflow (.github/workflows/pages.yml, run by
hand) deploys site/.

It refuses an unfinished run and the bundled SYNTHETIC example (also when renamed: its
fingerprint, `example-` model snapshots and placeholder hashes give it away), and removes site/
again if the secret scan flags anything in it, exported traces included. `--example --root DIR`
renders the example into a scratch copy of the repository, for previews and tests only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = "site"
ASSETS = "docs/assets"
RECORD = "docs/results"  # the published run's summary.json and run.json, committed


def _example_run(scratch: Path) -> Path:
    """Write the bundled example as a run folder (summary, records, questions, traces)."""
    from agentic_analytics.report.example import example_root

    src = example_root()
    run = scratch / "example-synthetic"
    (run / "traces").mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "records.jsonl", "questions.jsonl"):
        (run / name).write_text(src.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8")
    (run / "run.json").write_text('{"pack_source": "uk_price_paid"}\n', encoding="utf-8")
    for t in sorted(src.joinpath("traces").iterdir(), key=lambda t: t.name):
        if t.name.endswith(".json"):
            (run / "traces" / t.name).write_bytes(t.read_bytes())
    return run


def scan_site(site: Path) -> list[str]:
    """Secret-scan every file in the rendered site, traces included (paths relative to it)."""
    from secret_scan import expand, scan

    return [hit.replace(str(site) + os.sep, "", 1) for hit in scan(expand([site]))]


def publish(run_dir: Path, root: Path, *, allow_example: bool = False) -> str:
    """Publish `run_dir` into the repository at `root`; return a one-line summary."""
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.report import render_report, resolve_credit
    from agentic_analytics.report.figures import writeup_figures
    from agentic_analytics.report.readme import update_readme
    from agentic_analytics.report.render import looks_synthetic
    from agentic_analytics.run.runner import load_run, stamped_region
    from agentic_analytics.schemas import RunSummary

    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    if summary.finished_at is None:
        raise SystemExit(f"refusing to publish {summary.run_id}: the run has not finished")
    manifest, records, questions = load_run(run_dir)
    # a renamed copy of the example is still the example: fingerprint, snapshots, hashes
    if looks_synthetic(summary, records) and not allow_example:
        raise SystemExit("refusing to publish the SYNTHETIC example as results")
    try:
        pack = load_pack(manifest.get("pack_source") or summary.pack.partition("@")[0]).manifest
    except (PackError, OSError):
        pack = None
    if pack is not None and pack.ref != summary.pack:
        pack = None  # never credit a different version of the data
    credit, pack = resolve_credit(summary, pack, None)
    region = stamped_region(manifest)  # words the OWNER D-62 Cortex latency caveat
    config = manifest.get("config")
    models = config.get("models") if isinstance(config, dict) else None

    site = root / SITE
    if site.exists():
        shutil.rmtree(site)  # generated output only: stale traces must not linger
    render_report(
        summary,
        records,
        site,
        questions=questions,
        pack=pack,
        example=allow_example,
        snowflake_region=region,
    )
    (site / "summary.json").write_text(
        (run_dir / "summary.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    traces = run_dir / "traces"
    if traces.is_dir():
        (site / "traces").mkdir(exist_ok=True)
        for t in sorted(traces.glob("*.json")):
            shutil.copyfile(t, site / "traces" / t.name)
    leaks = scan_site(site)
    if leaks:
        shutil.rmtree(site)  # never leave a deployable site holding a key
        raise SystemExit(
            "refusing to publish: the secret scan flagged the site (removed):\n  "
            + "\n  ".join(leaks)
        )

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
            (record / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
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
    args = ap.parse_args(argv)
    root = (args.root or ROOT).resolve()
    if args.example == (args.run_dir is not None):
        ap.error("give a run folder or --example, not both")
    try:
        if args.example:
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
