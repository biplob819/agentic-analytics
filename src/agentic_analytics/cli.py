"""The `agentic-analytics` command line (alias `aa`; stdlib argparse, D-25, OWNER D-28).

Each subcommand is listed with the build node that makes it real. Until that node is green the
subcommand exits 2 and names the node, so `make` targets fail loudly instead of doing nothing.

Exit codes (Tech Spec §4): 0 ok; 1 parity, gold or probe failure; 2 configuration or runtime
error; 3 stopped at the cost cap, with work cached and a resume hint. Every command takes
`--json`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentic_analytics import __version__

PROG = "agentic-analytics"

# subcommand -> (help, node that builds it)
COMMANDS: dict[str, tuple[str, str]] = {
    "init": ("write run.yaml and .env.example into the current folder", "R3.1"),
    "pack": ("validate a dataset pack: `pack check [--pack PATH]`", "Q2.1"),
    "doctor": ("check env vars, engine reach and read-only roles", "N1.2"),
    "load": ("load the engines and run the parity check", "N1.3"),
    "gold": ("run the gold check", "Q2.2"),
    "smoke": ("12-question smoke run on every configured backend", "R3.1"),
    "run": ("run the benchmark", "R3.1"),
    "report": ("build the dashboard", "R3.1"),
    "reconcile": ("pull metered warehouse credits into a run", "R3.1"),
    "demo-cache": ("save answered questions for demo replay", "D4.1"),
    "setup": ("set up an engine account: `setup clickhouse` or `setup snowflake`", "N1.7"),
}

EXIT_OK, EXIT_FAILED, EXIT_ERROR, EXIT_COST_CAP = 0, 1, 2, 3
SMOKE_MAX_COST_USD = 2.0  # §4: the smoke run spends under $2


def _paths(p: argparse.ArgumentParser) -> None:
    p.add_argument("--results", default="results", help="results folder (default: results)")
    p.add_argument("--cache", default=".cache", help="answer cache folder (default: .cache)")
    p.add_argument(
        "--measurements",
        default="docs/measurements",
        help="snapshot, parity and ledger records (default: docs/measurements)",
    )


def _run_args(p: argparse.ArgumentParser, *, smoke: bool) -> None:
    p.add_argument("--config", default="run.yaml", help="run configuration (default: run.yaml)")
    p.add_argument("--pack", default="uk_price_paid", help="bundled pack name or pack directory")
    p.add_argument("--backend", default=None, help="all, or a comma list (default: run.yaml)")
    p.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="stop before this invocation's model spend passes $N (per invocation: a resumed "
        "run gets the cap afresh; the outcome prints the spend behind all the run's answers)",
    )
    p.add_argument("--dry-run", action="store_true", help="print the estimate; make no calls")
    p.add_argument("--resamples", type=int, default=10_000, help="bootstrap resamples")
    if not smoke:
        p.add_argument("--fix", default=None, help="none, all, or a comma list of fixes")
        p.add_argument("--trials", type=int, default=None, help="trials per configuration")
        p.add_argument("--rescore", action="store_true", help="rescore from the cache, no calls")
        p.add_argument(
            "--force-fix-hash",
            default=None,
            metavar="REASON",
            help="run fixes against baseline answers made under another fix_hash (D-52); the "
            "reason is logged in run.log and run.json",
        )
    _paths(p)


def _demo_cache_args(p: argparse.ArgumentParser) -> None:
    # defaults restate demo/cache.py's constants (a test keeps them equal; no import here)
    p.add_argument("--questions", type=int, default=10, help="sample questions (default: 10)")
    p.add_argument("--config", default="run.yaml", help="run configuration (default: run.yaml)")
    p.add_argument("--pack", default="uk_price_paid", help="bundled pack name or pack directory")
    p.add_argument("--backend", default="claude_mcp,cortex_analyst", help="comma list")
    p.add_argument("--out", default="demo/replay.json", help="cache file (demo/replay.json)")
    p.add_argument("--max-cost", type=float, default=10.0, help="stop before $N (default: 10)")
    p.add_argument("--dry-run", action="store_true", help="print the bound; make no calls")
    p.add_argument("--redo", action="store_true", help="re-ask samples with a failed panel")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__)
    ap.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command")
    for name, (help_text, _node) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        # SUPPRESS keeps a top-level --json from being reset by the subparser default
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "init":
            p.add_argument("--force", action="store_true", help="overwrite existing files")
            p.add_argument("--dir", default=".", help="folder to write into (default: .)")
        elif name == "pack":
            p.add_argument("action", nargs="?", choices=["check"], default=None)
            p.add_argument("--pack", default="uk_price_paid", help="pack name or directory")
        elif name == "gold":
            p.add_argument("--pack", default="uk_price_paid", help="pack name or directory")
            p.add_argument("--only", default=None, help="check one new question; writes nothing")
            p.add_argument(
                "--verify",
                action="store_true",
                help="only validate the existing gold check against the files; run no SQL",
            )
            p.add_argument("--config", default="run.yaml", help="run.yaml holding as_of")
            p.add_argument("--as-of", default=None, help="as-of date YYYY-MM-DD (else run.yaml)")
            p.add_argument("--seed", type=int, default=None, help="selection seed (default fixed)")
            p.add_argument(
                "--measurements",
                default="docs/measurements",
                help="snapshot records (default: docs/measurements)",
            )
        elif name in ("run", "smoke"):
            _run_args(p, smoke=name == "smoke")
        elif name == "report":
            p.add_argument("--run", default="latest", help="latest or a run id")
            p.add_argument("--example", action="store_true", help="the bundled synthetic run")
            p.add_argument("--out", default=None, help="output folder (default: the run folder)")
            p.add_argument("--results", default="results", help="results folder")
        elif name == "reconcile":
            p.add_argument("--run", default="latest", help="latest or a run id")
            p.add_argument(
                "--credits", required=True, help='JSON file of metered credits: {"snowflake": 0.4}'
            )
            p.add_argument("--results", default="results", help="results folder")
        elif name == "demo-cache":
            _demo_cache_args(p)
        elif name == "load":
            from agentic_analytics.load.cli import add_load_args

            add_load_args(p)
        elif name == "setup":
            from agentic_analytics.setup.cli import add_setup_args

            add_setup_args(p)
    return ap


def _redact(value: Any) -> Any:
    """Every CLI message passes the trace redactor: no secret from an exception text leaks."""
    from agentic_analytics.tracing import Redactor

    r = Redactor.from_environ()
    return (
        r.text(value) if isinstance(value, str) else r(json.loads(json.dumps(value, default=str)))
    )


def _emit(as_json: bool, payload: dict[str, Any], text: str, *, err: bool = False) -> None:
    if as_json:
        print(json.dumps(_redact(payload), sort_keys=True, default=str))
    else:
        print(_redact(text), file=sys.stderr if err else sys.stdout)


def _error(as_json: bool, message: str, code: int = EXIT_ERROR) -> int:
    _emit(as_json, {"ok": False, "error": message}, message, err=True)
    return code


def cmd_doctor(as_json: bool) -> int:
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.warehouse.doctor import (
        executors_from_env,
        mcp_query_from_env,
        run_doctor,
    )

    load_dotenv()
    ch, sf = executors_from_env()
    report = run_doctor(clickhouse=ch, snowflake=sf, mcp=mcp_query_from_env(), check_mcp=True)
    print(report.to_json() if as_json else report.render(), end="" if not as_json else "\n")
    return EXIT_OK if report.ok else EXIT_FAILED


def cmd_load(ns: argparse.Namespace) -> int:
    """N1.3: load the engines, plant the probes, run parity, set the as-of date. Progress lines
    and the outcome both pass the redactor."""
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.load.cli import cli_load

    load_dotenv()

    def say(text: str) -> None:
        print(_redact(text), flush=True)

    code, payload, text = cli_load(ns, os.environ, say=say)
    _emit(ns.json, payload, text, err=code != EXIT_OK)
    return code


def cmd_init(ns: argparse.Namespace) -> int:
    from agentic_analytics.run.config import RunConfigError, write_init

    try:
        written = write_init(Path(ns.dir), force=ns.force)
    except RunConfigError as exc:
        return _error(ns.json, str(exc))
    names = [str(p) for p in written]
    _emit(ns.json, {"ok": True, "written": names}, "wrote " + ", ".join(names))
    return EXIT_OK


def cmd_pack(ns: argparse.Namespace) -> int:
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.questions import check_pack

    if ns.action != "check":
        return _error(ns.json, "usage: agentic-analytics pack check [--pack PATH]")
    try:  # a pack that cannot be found or read is a configuration error (§4: exit 2)
        pack = load_pack(ns.pack)
    except PackError as exc:
        return _error(ns.json, f"cannot load pack {ns.pack!r}: {exc}")
    report = check_pack(pack)
    text = f"{report.pack}: {report.questions} questions, {report.probes} probes, " + (
        "ok" if report.ok else f"{len(report.issues)} issues\n  " + "\n  ".join(report.issues)
    )
    _emit(ns.json, report.to_dict(), text)
    return EXIT_OK if report.ok else EXIT_FAILED


def _verify_gold(ns: argparse.Namespace) -> int:
    """`gold --verify`: validate the pack's inputs and its existing gold check; run no SQL."""
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.questions import BankError, load_questions
    from agentic_analytics.run.gold import PreconditionError, load_gold_check, require_gold

    try:
        pack = load_pack(ns.pack)
        bank = load_questions(pack)
        check = load_gold_check(pack)
    except (PackError, BankError, PreconditionError) as exc:
        return _error(ns.json, str(exc))
    if check is None:
        return _error(ns.json, f"{pack.ref} has no gold check: run `{PROG} gold`", EXIT_FAILED)
    try:
        require_gold(pack, bank, check.engines, check.snapshot_sha256)
    except PreconditionError as exc:
        return _error(ns.json, f"gold check not clean: {exc}", EXIT_FAILED)
    msg = f"{pack.ref}: gold check {check.gold_check_id[:12]} is clean ({check.mode})"
    payload = {"ok": True, "gold_check_id": check.gold_check_id, "mode": check.mode}
    _emit(ns.json, payload, msg)
    return EXIT_OK


def _gold_as_of(ns: argparse.Namespace) -> Any:
    from datetime import date

    from agentic_analytics.run.config import RunConfigError
    from agentic_analytics.schemas import load_run_config

    if ns.as_of:
        try:
            return date.fromisoformat(ns.as_of)
        except ValueError as exc:
            raise RunConfigError(f"--as-of takes YYYY-MM-DD, got {ns.as_of!r}") from exc
    if not Path(ns.config).is_file():
        raise RunConfigError(
            f"no {ns.config} for the as-of date: pass --as-of or run `{PROG} init`"
        )
    return load_run_config(ns.config).as_of


def cmd_gold(ns: argparse.Namespace) -> int:
    """Q2.2: run the gold check (GLD-01..06), or check one question with `--only`."""
    if ns.verify:
        return _verify_gold(ns)
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.gold import check as gold
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.questions import BankError, load_questions
    from agentic_analytics.run.config import RunConfigError
    from agentic_analytics.run.gold import PreconditionError, current_snapshot
    from agentic_analytics.schemas import ConfigError
    from agentic_analytics.warehouse import WarehouseConfigError

    load_dotenv()
    try:
        pack = load_pack(ns.pack)
        bank = load_questions(pack)
        executors = gold.configured_executors(os.environ)
        if not executors:
            raise RunConfigError("no engine is configured: set the variables in .env.example")
        as_of = _gold_as_of(ns)
    except (PackError, BankError, RunConfigError, ConfigError) as exc:
        return _error(ns.json, str(exc))
    try:
        snapshot = current_snapshot(Path(ns.measurements))
    except PreconditionError as exc:
        return _error(ns.json, f"refusing to start: {exc}", EXIT_FAILED)

    def say(text: str) -> None:
        if not ns.json:
            print(text)

    seed = gold.SELECTION_SEED if ns.seed is None else ns.seed
    try:
        outcome = gold.run_gold_check(
            pack,
            bank,
            executors,
            as_of=as_of,
            snapshot_sha256=snapshot,
            seed=seed,
            only=ns.only,
            say=say,
        )
    except (gold.GoldConfigError, WarehouseConfigError) as exc:
        return _error(ns.json, str(exc))
    except Exception as exc:  # a runtime failure: exit 2 without a traceback
        return _error(ns.json, f"runtime error: {_redact(f'{type(exc).__name__}: {exc}')[:300]}")
    failures = list(outcome.failures)
    payload: dict[str, Any] = {"ok": not failures, "failures": failures, "only": ns.only}
    if outcome.check is not None:
        c = outcome.check
        payload |= {
            "gold_check_id": c.gold_check_id,
            "mode": c.mode,
            "status": c.status,
            "questions": len(c.questions),
            "written": [str(p) for p in outcome.written],
        }
        msg = (
            f"{pack.ref}: gold check {c.status} ({c.mode}), {len(c.questions)} questions "
            f"selected, gold_check_id {c.gold_check_id[:12]}"
        )
    else:
        msg = f"{ns.only}: {'ok' if not failures else 'FAILED'}"
    if failures:
        msg += f"; {len(failures)} failures:\n  " + "\n  ".join(failures[:50])
    _emit(ns.json, payload, msg, err=bool(failures))
    return outcome.exit_code


def _run_or_smoke(ns: argparse.Namespace, *, smoke: bool) -> int:
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.questions import BankError, load_questions
    from agentic_analytics.run import runner
    from agentic_analytics.run.config import (
        RunConfigError,
        parse_backends,
        parse_fixes,
        with_overrides,
    )
    from agentic_analytics.run.gold import PreconditionError, load_gold_check
    from agentic_analytics.schemas import ConfigError, load_run_config

    load_dotenv()
    rescore = bool(getattr(ns, "rescore", False))
    try:
        if not Path(ns.config).is_file():
            raise RunConfigError(f"no {ns.config}: run `{PROG} init` first")
        cfg = load_run_config(ns.config)
        pack = load_pack(ns.pack)
        configured = None if rescore else runner.configured_engines(os.environ)
        if configured is not None and not configured:
            if not ns.dry_run:
                raise RunConfigError("no engine is configured: set the variables in .env.example")
            configured = None  # OSS-03: a dry run estimates from the config and pack alone
        default = "all" if smoke else ",".join(cfg.backends)
        backends = parse_backends(ns.backend or default, configured)
        if smoke:
            max_cost = ns.max_cost if ns.max_cost is not None else SMOKE_MAX_COST_USD
            cfg = with_overrides(cfg, backends=backends, fixes=(), trials=1, max_cost_usd=max_cost)
        else:
            cfg = with_overrides(
                cfg,
                backends=backends,
                fixes=None if ns.fix is None else parse_fixes(ns.fix),
                trials=ns.trials,
                max_cost_usd=ns.max_cost,
            )
    except (RunConfigError, ConfigError, PackError) as exc:
        return _error(ns.json, str(exc))

    question_ids = None
    if smoke:
        try:
            check = load_gold_check(pack)
            bank = [q for q in load_questions(pack) if check is None or q.id in check.questions]
        except (BankError, PackError) as exc:
            return _error(ns.json, str(exc))
        except PreconditionError as exc:
            return _error(ns.json, f"refusing to start: {exc}", EXIT_FAILED)
        question_ids = [q.id for q in runner.smoke_questions(bank)]

    def say(text: str) -> None:
        if not ns.json:
            print(text)

    measurements = Path(ns.measurements)
    options = runner.RunOptions(
        results_dir=Path(ns.results),
        cache_dir=Path(ns.cache),
        measurements_dir=measurements,
        ledger_path=measurements / "spend-ledger.jsonl",
        engines=None if configured is None else tuple(configured),
        kind="smoke" if smoke else ("rescore" if rescore else "run"),
        dry_run=ns.dry_run,
        rescore=rescore,
        question_ids=question_ids,
        include_probes=not smoke,
        n_resamples=ns.resamples,
        fix_hash_reason=getattr(ns, "force_fix_hash", None),
        say=say,
    )

    def services() -> runner.Services:
        return runner.services_from_env(cfg, pack, os.environ)

    outcome = runner.run_benchmark(cfg, pack, services=services, options=options)
    _emit(ns.json, outcome.to_dict(), outcome.message, err=outcome.exit_code != EXIT_OK)
    return outcome.exit_code


def cmd_run(ns: argparse.Namespace) -> int:
    return _run_or_smoke(ns, smoke=False)


def cmd_smoke(ns: argparse.Namespace) -> int:
    return _run_or_smoke(ns, smoke=True)


def cmd_report(ns: argparse.Namespace) -> int:
    from agentic_analytics.packs import PackError, load_pack
    from agentic_analytics.report import EXAMPLE_RUN_ID, render_example, render_report
    from agentic_analytics.run.config import RunConfigError
    from agentic_analytics.run.runner import load_run, resolve_run, stamped_region
    from agentic_analytics.schemas import RunSummary

    results = Path(ns.results)
    if ns.example:
        out = Path(ns.out) if ns.out else results / EXAMPLE_RUN_ID
        path = render_example(out)
        _emit(ns.json, {"ok": True, "path": str(path)}, f"wrote {path} (synthetic example)")
        return EXIT_OK
    try:
        run_dir = resolve_run(results, ns.run)
        manifest, records, questions = load_run(run_dir)
        summary = RunSummary.model_validate_json(
            (run_dir / "summary.json").read_text(encoding="utf-8")
        )
    except (RunConfigError, OSError, ValueError) as exc:
        return _error(ns.json, f"cannot report: {exc}")
    try:
        pack_manifest = load_pack(manifest.get("pack_source") or "").manifest
    except (PackError, OSError):
        pack_manifest = None
    out = Path(ns.out) if ns.out else run_dir
    path = render_report(
        summary,
        records,
        out,
        questions=questions,
        pack=pack_manifest,
        snowflake_region=stamped_region(manifest),
    )
    _emit(ns.json, {"ok": True, "path": str(path), "run_id": summary.run_id}, f"wrote {path}")
    return EXIT_OK


def cmd_reconcile(ns: argparse.Namespace) -> int:
    from agentic_analytics.run.config import RunConfigError
    from agentic_analytics.run.runner import reconcile_run, resolve_run

    try:
        run_dir = resolve_run(Path(ns.results), ns.run)
        data = json.loads(Path(ns.credits).read_text(encoding="utf-8"))
        summary = reconcile_run(run_dir, data)
    except (RunConfigError, OSError, ValueError) as exc:
        return _error(ns.json, f"cannot reconcile: {exc}")
    msg = (
        f"{summary.run_id}: cost_status {summary.cost_status}, total ${summary.total_cost_usd:.4f}"
    )
    payload = {
        "ok": True,
        "run_id": summary.run_id,
        "cost_status": summary.cost_status,
        "total_cost_usd": summary.total_cost_usd,
        "total_credits": dict(summary.total_credits),
    }
    _emit(ns.json, payload, msg)
    return EXIT_OK


def cmd_demo_cache(ns: argparse.Namespace) -> int:
    from agentic_analytics.demo.cache import cli_demo_cache
    from agentic_analytics.env import load_dotenv

    load_dotenv()
    code, payload, text = cli_demo_cache(ns, os.environ)
    _emit(ns.json, payload, text, err=code != EXIT_OK)
    return code


def cmd_setup(ns: argparse.Namespace) -> int:
    """N1.7 (OWNER D-55). Output goes through the setup command's own secret box, which knows
    every secret it has seen; the printed Snowflake SQL holds public keys only."""
    from agentic_analytics.env import load_dotenv
    from agentic_analytics.setup.cli import cli_setup

    env_file = getattr(ns, "env_file", ".env")
    load_dotenv(env_file)
    code, payload, text = cli_setup(ns, os.environ)
    if ns.json:
        print(json.dumps(payload, sort_keys=True, default=str))
    else:
        print(text, file=sys.stderr if code != EXIT_OK else sys.stdout)
    return code


BUILT: dict[str, Callable[[argparse.Namespace], int]] = {
    "doctor": lambda ns: cmd_doctor(ns.json),
    "init": cmd_init,
    "load": cmd_load,
    "pack": cmd_pack,
    "gold": cmd_gold,
    "run": cmd_run,
    "smoke": cmd_smoke,
    "report": cmd_report,
    "reconcile": cmd_reconcile,
    "demo-cache": cmd_demo_cache,
    "setup": cmd_setup,
}


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    # Subcommand options are defined by the node that builds the subcommand. Until then extra
    # arguments after a subcommand are accepted so `make` targets reach the "not built" message.
    ns, rest = ap.parse_known_args(argv)
    if ns.command is None:
        if rest:
            ap.error(f"unrecognized arguments: {' '.join(rest)}")
        ap.print_help()
        return EXIT_OK
    if ns.command in BUILT:
        if rest:
            ap.error(f"unrecognized arguments: {' '.join(rest)}")
        try:
            return BUILT[ns.command](ns)
        except Exception as exc:  # never a traceback: one redacted line, exit 2 (§4)
            # redact the whole text first: a cut key no longer matches its environment value
            text = " ".join(_redact(f"{type(exc).__name__}: {exc}").split())[:300]
            return _error(ns.json, f"{PROG} {ns.command}: runtime error: {text}")
    node = COMMANDS[ns.command][1]
    msg = f"`{PROG} {ns.command}` is not built yet; it arrives with build node {node}"
    if ns.json:
        print(json.dumps({"ok": False, "error": "not_built", "node": node, "message": msg}))
    else:
        print(msg, file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
