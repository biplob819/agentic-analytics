"""The `agentic-analytics` command line (alias `aa`; stdlib argparse, D-25, OWNER D-28).

Each subcommand is listed with the build node that makes it real. Until that node is green the
subcommand exits 2 and names the node, so `make` targets fail loudly instead of doing nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from agentic_analytics import __version__

PROG = "agentic-analytics"

# subcommand -> (help, node that builds it)
COMMANDS: dict[str, tuple[str, str]] = {
    "init": ("write run.yaml and .env.example into the current folder", "B3.3"),
    "pack": ("validate a dataset pack: `pack check [--pack PATH]`", "Q2.1"),
    "doctor": ("check env vars, engine reach and read-only roles", "N1.2"),
    "load": ("load the engines and run the parity check", "N1.3"),
    "gold": ("run the gold check", "Q2.2"),
    "smoke": ("12-question smoke run on every configured backend", "R3.1"),
    "run": ("run the benchmark", "R3.1"),
    "report": ("build the dashboard", "R3.1"),
    "reconcile": ("pull metered warehouse credits into a run", "R3.1"),
    "demo-cache": ("save answered questions for demo replay", "D4.1"),
}

EXIT_OK, EXIT_FAILED, EXIT_ERROR, EXIT_COST_CAP = 0, 1, 2, 3


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__)
    ap.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="command")
    for name, (help_text, _node) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text)
        # SUPPRESS keeps a top-level --json from being reset by the subparser default
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "load":
            from agentic_analytics.load.cli import add_load_args

            add_load_args(p)
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
    from agentic_analytics.warehouse.doctor import executors_from_env, run_doctor

    load_dotenv()
    ch, sf = executors_from_env()
    report = run_doctor(clickhouse=ch, snowflake=sf)
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


BUILT: dict[str, Callable[[argparse.Namespace], int]] = {
    "doctor": lambda ns: cmd_doctor(ns.json),
    "load": cmd_load,
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
            text = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
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
