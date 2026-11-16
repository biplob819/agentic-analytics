"""`agentic-analytics load` (N1.3): load the engines, plant the probes, run parity, set as-of.

Order for `--engine all` (the default; `both` is the same): the ClickHouse `url()` load, then the
Snowflake load (download, sha256, `PUT`, `COPY`, typed insert), checked against the ClickHouse
load time when ClickHouse loaded in this run (D-11: the download starts within the hour), then
the probes table on every chosen engine, then the parity fingerprints on every chosen engine.
A passing parity sets the as-of date (D-50) and writes it into `--config` when that file exists.

Records: `snapshot-<date>.json` (after a fresh Snowflake download) and `parity-<date>.json`, both
in `--measurements`. Exit codes (Tech Spec §4): 0 ok; 1 parity failed; 2 configuration or load
error. Loads run on the ADMIN credentials only. Every message goes through the CLI redactor in
`cli._emit`; this module never formats a credential into a message.

`--dry-run` connects to nothing: it renders and checks every statement the load would send and
names the admin variables that are missing.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agentic_analytics.load import clickhouse as ch_load
from agentic_analytics.load import probes as probes_load
from agentic_analytics.load import snowflake as sf_load
from agentic_analytics.load._common import Clock, LoaderError, LoadResult, utc_now
from agentic_analytics.load.asof import as_of_for, max_date_from_parity, write_as_of
from agentic_analytics.load.parity import (
    ParityError,
    QueryRunner,
    clickhouse_runner,
    load_parity_spec,
    run_parity,
    snowflake_runner,
)
from agentic_analytics.load.snapshot import (
    Downloader,
    content_length,
    http_download,
    source_csv_url,
    write_snapshot_record,
)
from agentic_analytics.packs import Pack, PackError, load_pack
from agentic_analytics.schemas import ENGINES, Engine

EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2
ENGINE_CHOICES = ("all", "both", "clickhouse", "snowflake")
ADMIN_ENV: dict[Engine, tuple[str, ...]] = {
    "clickhouse": ("CLICKHOUSE_HOST", "CLICKHOUSE_ADMIN_USER", "CLICKHOUSE_ADMIN_PASSWORD"),
    "snowflake": (
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_ADMIN_USER",
        "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH",
        "SNOWFLAKE_WAREHOUSE",
    ),
}
DISK_FACTOR = 1.5  # the CSV plus its gzip copy (about a fifth of the CSV), with room to spare
DISK_MARGIN_BYTES = 1 << 30
SETUP_STAGE_FILE = "pp-complete.csv.gz"

Factory = Callable[[Mapping[str, str]], Any]
SizeProbe = Callable[[str], int | None]
FreeSpace = Callable[[Path], int]


def add_load_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pack", default="uk_price_paid", help="bundled pack name or pack directory")
    p.add_argument(
        "--engine",
        choices=ENGINE_CHOICES,
        default="all",
        help="which engines to load (default: all; both is the same)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="reload tables that already hold rows, and replace a table whose columns differ",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="check every statement the load would send; connect to nothing",
    )
    p.add_argument(
        "--workdir",
        default="data/snapshots",
        help="where the Snowflake snapshot is downloaded (default: data/snapshots, gitignored)",
    )
    p.add_argument(
        "--measurements",
        default="docs/measurements",
        help="snapshot and parity records (default: docs/measurements)",
    )
    p.add_argument(
        "--config",
        default="run.yaml",
        help="run configuration whose as_of is set after a passing parity, if it exists",
    )


def chosen_engines(choice: str, pack: Pack) -> tuple[Engine, ...]:
    if choice in ("all", "both"):
        return tuple(e for e in ENGINES if e in pack.manifest.engines)
    if choice not in pack.manifest.engines:
        raise LoaderError(f"pack {pack.ref} has no {choice} database")
    return (choice,)  # type: ignore[return-value]


def missing_env(engines: tuple[Engine, ...], environ: Mapping[str, str]) -> list[str]:
    """Names (never values) of the admin variables that are not set."""
    return [n for e in engines for n in ADMIN_ENV[e] if not (environ.get(n) or "").strip()]


# ---- the download guard ------------------------------------------------------------------------


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def needed_bytes(size: int) -> int:
    return int(size * DISK_FACTOR) + DISK_MARGIN_BYTES


def guarded_download(
    download: Downloader,
    *,
    size_of: SizeProbe,
    free: FreeSpace,
    say: Callable[[str], None],
) -> Downloader:
    """Wrap a downloader so it first checks the free disk space for the CSV and its gzip copy.
    It runs only when the Snowflake loader really downloads (never on a skipped load)."""

    def run(url: str, dest: Path) -> None:
        size = size_of(url)
        available = free(dest.parent)
        if size is None:
            say(f"snowflake: the source does not state its size; {available / 1e9:.1f} GB free")
        else:
            need = needed_bytes(size)
            if available < need:
                raise LoaderError(
                    f"not enough disk space in {dest.parent}: the snapshot needs about "
                    f"{need / 1e9:.1f} GB (CSV {size / 1e9:.1f} GB plus its gzip copy), "
                    f"{available / 1e9:.1f} GB free"
                )
            say(
                f"snowflake: downloading {size / 1e9:.2f} GB to {dest} "
                f"({available / 1e9:.0f} GB free); this can take a while"
            )
        download(url, dest)
        say("snowflake: download done; hashing, gzipping and staging")

    return run


# ---- dry run -----------------------------------------------------------------------------------


def dry_run(
    pack: Pack, engines: tuple[Engine, ...], environ: Mapping[str, str], workdir: Path
) -> tuple[int, dict[str, Any], str]:
    """Render and check every statement the load would send (the loaders' allow-lists), and the
    parity spec; connect to nothing, download nothing."""
    steps: dict[str, list[str]] = {}
    if "clickhouse" in engines:
        steps["clickhouse"] = [s.kind for s in ch_load.load_steps(pack)]
        steps["clickhouse_probes"] = [s.kind for s in probes_load.clickhouse_steps(pack)]
    if "snowflake" in engines:
        local = (workdir / SETUP_STAGE_FILE).resolve().as_posix()
        steps["snowflake"] = [
            s.kind for s in sf_load.load_steps(pack, local_file=local, stage_file=SETUP_STAGE_FILE)
        ]
        steps["snowflake_probes"] = [s.kind for s in probes_load.snowflake_steps(pack)]
    fingerprints = len(load_parity_spec(pack).fingerprints)
    missing = missing_env(engines, environ)
    url = source_csv_url(pack)
    payload: dict[str, Any] = {
        "ok": True,
        "dry_run": True,
        "pack": pack.ref,
        "engines": list(engines),
        "source_url": url,
        "workdir": str(workdir),
        "steps": steps,
        "parity_fingerprints": fingerprints,
        "missing_env": missing,
    }
    lines = [f"load --dry-run: {pack.ref} on {', '.join(engines)}; nothing was sent"]
    lines += [
        f"  {name}: {len(kinds)} statements ({', '.join(kinds)})" for name, kinds in steps.items()
    ]
    lines.append(f"  source: {url}")
    if "snowflake" in engines:
        lines.append(f"  snapshot download to: {workdir}")
    lines.append(f"  parity: {fingerprints} fingerprints")
    if missing:
        lines.append(f"  missing admin variables: {', '.join(missing)}")
    return EXIT_OK, payload, "\n".join(lines)


# ---- the live load -----------------------------------------------------------------------------


def _latest_snapshot(measurements: Path) -> dict[str, Any] | None:
    """The newest snapshot record (by `downloaded_at`), for a run where Snowflake skipped."""
    best: tuple[str, dict[str, Any]] | None = None
    for p in sorted(measurements.glob("snapshot-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        at, sha = data.get("downloaded_at"), data.get("sha256")
        valid = isinstance(at, str) and isinstance(sha, str) and len(sha) == 64
        if valid and (best is None or at > best[0]):
            best = (at, data)
    return best[1] if best else None


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):  # closing must never hide the load's outcome
            close()


def _load_line(res: LoadResult) -> str:
    rows = ", ".join(f"{t} {n:,}" for t, n in res.rows_after.items())
    secs = (res.finished_at - res.started_at).total_seconds()
    return f"{res.engine}: {res.action} ({rows}) in {secs:.0f}s"


def cli_load(
    ns: Any,
    environ: Mapping[str, str],
    *,
    say: Callable[[str], None] | None = None,
    deps: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any], str]:
    """Run `load`: (exit code, JSON payload, text). `deps` injects fakes in tests:
    `clickhouse` and `snowflake` (factories taking the environment), `download`, `size_of`,
    `free` and `now`."""
    deps = dict(deps or {})
    quiet = bool(getattr(ns, "json", False))

    def progress(text: str) -> None:
        if not quiet:
            (say or print)(text)

    now: Clock = deps.get("now", utc_now)
    try:
        pack = load_pack(ns.pack)
        engines = chosen_engines(ns.engine, pack)
    except (PackError, LoaderError) as exc:
        return EXIT_ERROR, {"ok": False, "error": str(exc)}, str(exc)
    workdir = Path(ns.workdir)
    measurements = Path(ns.measurements)
    if ns.dry_run:
        try:
            return dry_run(pack, engines, environ, workdir)
        except (LoaderError, ParityError, ValueError) as exc:
            return EXIT_ERROR, {"ok": False, "error": str(exc)}, str(exc)
    missing = missing_env(engines, environ)
    if missing:
        msg = f"missing environment variables for the load: {', '.join(missing)} (see .env.example)"
        return EXIT_ERROR, {"ok": False, "error": msg, "missing_env": missing}, msg

    connections: dict[Engine, Any] = {}
    loads: dict[str, LoadResult] = {}
    warnings: list[str] = []
    try:
        if "clickhouse" in engines:
            factory: Factory = deps.get("clickhouse", ch_load.admin_client_from_env)
            connections["clickhouse"] = factory(environ)
            db = ch_load.database(pack)
            progress(f"clickhouse: {db}: a url() load takes minutes; a loaded table is skipped")
            loads["clickhouse"] = ch_load.load_clickhouse(
                pack, connections["clickhouse"], force=ns.force, now=now
            )
            progress(_load_line(loads["clickhouse"]))
        if "snowflake" in engines:
            factory = deps.get("snowflake", sf_load.admin_connection_from_env)
            connections["snowflake"] = factory(environ)
            ch_res = loads.get("clickhouse")
            ch_at = ch_res.finished_at if ch_res and ch_res.action != "skipped" else None
            download = guarded_download(
                deps.get("download", http_download),
                size_of=deps.get("size_of", content_length),
                free=deps.get("free", free_bytes),
                say=progress,
            )
            progress(f"snowflake: setting up {sf_load.database(pack)[1]}")
            loads["snowflake"] = sf_load.load_snowflake(
                pack,
                connections["snowflake"],
                workdir=workdir,
                force=ns.force,
                download=download,
                clickhouse_loaded_at=ch_at,
                now=now,
            )
            progress(_load_line(loads["snowflake"]))
            if loads["snowflake"].action != "skipped" and "clickhouse" in engines and ch_at is None:
                warnings.append(
                    "ClickHouse was already loaded, so the one-hour window between the two loads "
                    "(D-11) could not be checked; parity decides whether they hold the same data. "
                    "Rerun with --force to reload both together."
                )
        snapshot: dict[str, Any] | None = None
        snapshot_path: Path | None = None
        sf_res = loads.get("snowflake")
        if sf_res is not None and sf_res.snapshot:
            snapshot = dict(sf_res.snapshot)
            snapshot_path = write_snapshot_record(snapshot, measurements)
            progress(f"snapshot: sha256 {snapshot['sha256']} recorded in {snapshot_path}")
        else:
            snapshot = _latest_snapshot(measurements)

        probes: dict[str, dict[str, int]] = {}
        for engine in engines:
            loader = (
                probes_load.load_probes_clickhouse
                if engine == "clickhouse"
                else probes_load.load_probes_snowflake
            )
            res = loader(pack, connections[engine], now=now)
            probes[engine] = dict(res.rows_after)
            progress(f"{engine}: probes {', '.join(f'{t} {n}' for t, n in res.rows_after.items())}")

        runners: dict[Engine, QueryRunner] = {}
        if "clickhouse" in connections:
            runners["clickhouse"] = clickhouse_runner(connections["clickhouse"])
        if "snowflake" in connections:
            runners["snowflake"] = snowflake_runner(connections["snowflake"])
        progress(f"parity: running the fingerprints on {', '.join(runners)}")
        report = run_parity(
            pack,
            runners,
            snapshot_sha256=snapshot["sha256"] if snapshot else None,
            out_dir=measurements,
            now=now,
        )
    except (LoaderError, ParityError, ValueError) as exc:
        msg = f"load stopped: {exc}"
        payload = {
            "ok": False,
            "error": msg,
            "loads": {e: r.to_dict() for e, r in loads.items()},
        }
        return EXIT_ERROR, payload, msg
    finally:
        for conn in connections.values():
            _close(conn)

    failed = [
        {"name": f.name, "reason": f.reason, "errors": dict(f.errors)}
        for f in report.fingerprints
        if f.match is False
    ]
    payload: dict[str, Any] = {
        "ok": report.passed,
        "pack": pack.ref,
        "engines": list(engines),
        "loads": {e: r.to_dict() for e, r in loads.items()},
        "probes": probes,
        "snapshot": snapshot,
        "snapshot_record": str(snapshot_path) if snapshot_path else None,
        "parity": {
            "status": report.status,
            "path": str(report.path) if report.path else None,
            "fingerprints": len(report.fingerprints),
            "failed": failed,
        },
        "max_date": None,
        "as_of": None,
        "config_updated": None,
        "warnings": warnings,
    }
    lines: list[str] = []  # each load's line was already printed as progress
    lines.append(
        f"parity: {report.status} ({len(report.fingerprints)} fingerprints) -> {report.path}"
    )
    if failed:
        lines += [f"  FAILED {f['name']}: {f['reason']}" for f in failed]
        lines.append("parity failed: nothing downstream (gold, runs) will start")
        lines += [f"warning: {w}" for w in warnings]
        return EXIT_FAILED, payload, "\n".join(lines)

    max_date = max_date_from_parity(report)
    downloaded_on = (
        dt.datetime.fromisoformat(snapshot["downloaded_at"]).date() if snapshot else now().date()
    )
    as_of = as_of_for(pack, max_date, downloaded_on=downloaded_on)
    payload["max_date"] = max_date.isoformat()
    payload["as_of"] = as_of.isoformat()
    config = Path(ns.config)
    if config.is_file():
        write_as_of(config, as_of)
        payload["config_updated"] = str(config)
        lines.append(f"as-of: {as_of} (latest sale {max_date}); written to {config}")
    else:
        lines.append(
            f"as-of: {as_of} (latest sale {max_date}); no {config} to write it to "
            "(`agentic-analytics init` creates one)"
        )
    if snapshot:
        lines.append(f"snapshot: sha256 {snapshot['sha256']}")
    lines += [f"warning: {w}" for w in warnings]
    return EXIT_OK, payload, "\n".join(lines)


__all__ = [
    "ADMIN_ENV",
    "ENGINE_CHOICES",
    "add_load_args",
    "chosen_engines",
    "cli_load",
    "dry_run",
    "guarded_download",
    "missing_env",
    "needed_bytes",
]
