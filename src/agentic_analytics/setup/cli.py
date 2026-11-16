"""CLI wiring for `agentic-analytics setup clickhouse|snowflake` (N1.7, OWNER D-55).

`add_setup_args` defines the options; `cli_setup` runs one engine and returns
(exit code, JSON payload, text). Progress lines go through `say`, already scrubbed of every
secret. Exit 0 when setup finished, 2 on any configuration or runtime error.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agentic_analytics.setup._common import SecretBox, SetupError
from agentic_analytics.setup.envfile import EnvFileError

USAGE = """\
usage: agentic-analytics setup clickhouse [--name NAME] [--region REGION] [--ip-allow CIDR]...
                                          [--key-ip-allow CIDR[=DESC]]... [--ro-user USER]
                                          [--rotate-mcp-key] [--reset-admin-password]
                                          [--mcp-role-id UUID]... [--env-file PATH]
       agentic-analytics setup snowflake [--key-dir DIR] [--run] [--account ACCOUNT]
                                         [--env-file PATH]"""


def add_setup_args(p: argparse.ArgumentParser) -> None:
    from agentic_analytics.setup.clickhouse import DEFAULT_KEY_IP_ALLOW, DEFAULT_SERVICE
    from agentic_analytics.setup.snowflake import DEFAULT_KEY_DIR

    engines = p.add_subparsers(dest="engine", metavar="{clickhouse,snowflake}")
    ch = engines.add_parser(
        "clickhouse",
        help="create or adopt the ClickHouse Cloud service, MCP key and read-only user",
    )
    ch.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    ch.add_argument("--name", default=DEFAULT_SERVICE, help=f"service name ({DEFAULT_SERVICE})")
    ch.add_argument("--region", default=None, help="AWS region, needed to create the service")
    ch.add_argument(
        "--ip-allow",
        action="append",
        default=[],
        metavar="CIDR[=DESC]",
        help="service IP allow list entry (repeatable; none means open to anywhere)",
    )
    ch.add_argument(
        "--key-ip-allow",
        action="append",
        default=None,
        metavar="CIDR[=DESC]",
        help=f"MCP key IP allow list entry (repeatable; default {DEFAULT_KEY_IP_ALLOW[0]!r})",
    )
    ch.add_argument(
        "--ro-user", default=None, help="read-only user (CLICKHOUSE_RO_USER or aa_ro_user)"
    )
    ch.add_argument("--rotate-mcp-key", action="store_true", help="make a new MCP key even if set")
    ch.add_argument(
        "--reset-admin-password",
        action="store_true",
        help="issue a new default-user password for an adopted service",
    )
    ch.add_argument(
        "--mcp-role-id",
        action="append",
        default=[],
        metavar="UUID",
        help="org role id for the MCP key (repeatable; default: the system role "
        "'Basic Service API Reader', found by name)",
    )
    ch.add_argument("--pack", default="uk_price_paid", help="pack whose databases the role reads")
    ch.add_argument("--env-file", default=".env", help="the .env to update (default: .env)")
    ch.add_argument("--clickhousectl", default=None, help="path to clickhousectl")
    ch.add_argument("--wait", type=float, default=900.0, help="seconds to wait for the service")
    sf = engines.add_parser(
        "snowflake", help="make key pairs and the ACCOUNTADMIN SQL; --run runs it"
    )
    sf.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sf.add_argument(
        "--key-dir", default=str(DEFAULT_KEY_DIR), help=f"key folder (default {DEFAULT_KEY_DIR})"
    )
    sf.add_argument(
        "--run",
        action="store_true",
        help="run the SQL now (asks for password and MFA code, hidden) and fill in .env",
    )
    sf.add_argument("--account", default=None, help="account identifier, e.g. ORG-ACCOUNT")
    sf.add_argument("--pack", default="uk_price_paid", help="pack whose database the role reads")
    sf.add_argument("--env-file", default=".env", help="the .env to update (default: .env)")


def _clickhouse(
    ns: Any,
    environ: Mapping[str, str],
    say: Callable[[str], None],
    deps: Mapping[str, Any],
    box: SecretBox,
) -> tuple[dict[str, Any], str]:
    from agentic_analytics.setup.clickhouse import (
        ADMIN_KEY_NOTE,
        DEFAULT_KEY_IP_ALLOW,
        ClickHouseOptions,
        setup_clickhouse,
    )
    from agentic_analytics.setup.clickhousectl import (
        Clickhousectl,
        clickhousectl_env,
        find_clickhousectl,
        subprocess_runner,
    )

    exe = deps.get("clickhousectl") or find_clickhousectl(ns.clickhousectl)
    ctl = Clickhousectl(exe, deps.get("runner", subprocess_runner), clickhousectl_env(environ), box)
    opts = ClickHouseOptions(
        name=ns.name,
        region=ns.region,
        ip_allow=tuple(ns.ip_allow),
        key_ip_allow=tuple(ns.key_ip_allow) if ns.key_ip_allow else DEFAULT_KEY_IP_ALLOW,
        ro_user=ns.ro_user,
        rotate_mcp_key=ns.rotate_mcp_key,
        reset_admin_password=ns.reset_admin_password,
        mcp_role_ids=tuple(ns.mcp_role_id),
        pack=ns.pack,
        wait_s=ns.wait,
    )
    kwargs: dict[str, Any] = {k: deps[k] for k in ("admin_client", "sleep", "clock") if k in deps}
    result = setup_clickhouse(
        opts, ctl=ctl, env_path=Path(ns.env_file), environ=environ, say=say, **kwargs
    )
    text = (
        f"ClickHouse setup done: service {result.service!r} ({result.service_id}), "
        f"host {result.host}, MCP key {result.mcp_key}, read-only user {result.ro_user}.\n"
        f"{len(set(result.env_written))} variables updated in {ns.env_file} (values not shown).\n\n"
        f"{result.console_steps}\n{ADMIN_KEY_NOTE}"
    )
    return {**result.to_dict(), "admin_key_note": ADMIN_KEY_NOTE}, box.scrub(text)


def _snowflake(
    ns: Any,
    environ: Mapping[str, str],
    say: Callable[[str], None],
    deps: Mapping[str, Any],
    box: SecretBox,
) -> tuple[dict[str, Any], str]:
    from agentic_analytics.setup.snowflake import SNOWSIGHT_STEPS, SnowflakeOptions, setup_snowflake

    opts = SnowflakeOptions(key_dir=Path(ns.key_dir), run=ns.run, account=ns.account, pack=ns.pack)
    kwargs: dict[str, Any] = {
        k: deps[k] for k in ("connect", "prompt", "secret_prompt") if k in deps
    }
    result = setup_snowflake(
        opts, env_path=Path(ns.env_file), environ=environ, say=say, box=box, **kwargs
    )
    if result.ran:
        text = (
            f"Snowflake setup done: account {result.account}; AA_ADMIN_USER and AA_RO_USER log in "
            f"with their key pairs; {len(set(result.env_written))} variables updated in "
            f"{ns.env_file}.\nNext: `agentic-analytics doctor`."
        )
        return result.to_dict(include_sql=False), box.scrub(text)
    env_text = "\n".join(f"{k}={v}" for k, v in result.env_lines.items())
    text = (
        f"{SNOWSIGHT_STEPS} Saved as {result.sql_path}.\n\n{result.sql}\n"
        "Then put the value in the last result row into SNOWFLAKE_ACCOUNT in .env, with:\n"
        f"{env_text}\n"
        "Or run `agentic-analytics setup snowflake --run` to do all of it from here."
    )
    return result.to_dict(), box.scrub(text)


def cli_setup(
    ns: Any,
    environ: Mapping[str, str],
    *,
    say: Callable[[str], None] | None = None,
    deps: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any], str]:
    """Run `setup <engine>`: (exit code, JSON payload, text). `deps` injects fakes in tests."""
    deps = dict(deps or {})
    quiet = bool(getattr(ns, "json", False))
    box = SecretBox.from_environ(environ)

    def progress(text: str) -> None:
        if not quiet:
            (say or print)(box.scrub(text))

    engine = getattr(ns, "engine", None)
    if engine not in ("clickhouse", "snowflake"):
        return 2, {"ok": False, "error": "usage"}, USAGE
    try:
        if engine == "clickhouse":
            payload, text = _clickhouse(ns, environ, progress, deps, box)
        else:
            payload, text = _snowflake(ns, environ, progress, deps, box)
    except (SetupError, EnvFileError) as exc:
        msg = box.scrub(f"setup {engine}: {exc}")
        return 2, {"ok": False, "error": msg}, msg
    except Exception as exc:  # OSError, or anything unexpected: this box knows the run's secrets
        msg = box.scrub(f"setup {engine}: {type(exc).__name__}: {exc}")
        return 2, {"ok": False, "error": msg}, msg
    return 0, payload, text


__all__ = ["USAGE", "add_setup_args", "cli_setup"]
