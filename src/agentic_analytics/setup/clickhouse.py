"""`agentic-analytics setup clickhouse` (N1.7: SET-01, SET-02, SET-03, SET-05; OWNER D-55).

Steps, each idempotent, so a failed run is simply run again:

1. Service (SET-01). `clickhousectl cloud service list` finds the service by its exact name and
   adopts it; otherwise `service create --name N --provider aws --region R [--ip-allow ...]`
   creates it. The default user's password is printed by ClickHouse once: it goes into `.env`
   the moment it arrives, before anything else can fail. `service get` is polled until the state
   is `running` (or `idle`, which wakes on the first query); a `stopped` service is started.
   An adopted service needs CLICKHOUSE_ADMIN_PASSWORD in `.env`, or `--reset-admin-password`.
2. MCP API key (SET-02). Unless CLICKHOUSE_MCP_KEY_ID and CLICKHOUSE_MCP_KEY_SECRET are already
   set (and `--rotate-mcp-key` is not given), `key create` makes a key with the system org role
   "Basic Service API Reader" (its id found by exact name in `org role list`; `--mcp-role-id`
   overrides it) and an explicit IP allow list (a key without one denies everything). Query
   access comes from the Query API endpoint binding (step 4), not from an org role. The secret
   is printed once and goes straight into `.env`.
3. Read-only user and role (SET-03). With the admin password, over clickhouse-connect: create the
   read-only user (or reset its password to the one in `.env`), then the role SQL from
   `warehouse.roles.pack_role_sql`, checked by `role_sql_problems` before anything is sent.
4. Console steps (SET-05). Turning on remote MCP and binding the generic Query API endpoint to
   the MCP key and `analytics_ro` have no public API (the legacy endpoint API accepts only
   `sql_console_read_only` and `sql_console_admin`), so they are printed, word for word.

Nothing printed ever holds a password, key id or key secret (`SecretBox`).
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import string
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agentic_analytics.setup._common import (
    SecretBox,
    SetupError,
    find_records,
    find_value,
    key_names,
    require_value,
)
from agentic_analytics.setup._common import field as record_field
from agentic_analytics.setup.clickhousectl import Clickhousectl
from agentic_analytics.setup.envfile import read_env_file, update_env_file
from agentic_analytics.warehouse.roles import CH_ROLE, pack_role_sql, role_sql_problems

DEFAULT_SERVICE = "agentic-analytics"
DEFAULT_RO_USER = "aa_ro_user"
ADMIN_USER = "default"
MCP_URL = "https://mcp.clickhouse.cloud/mcp"
# The MCP key's org role (live 2026-11-17): the least-privilege system role that lets the remote
# MCP server work. Query access comes from the Query API endpoint binding, not from an org role.
MCP_KEY_ROLE = "Basic Service API Reader"
DEFAULT_KEY_IP_ALLOW = ("0.0.0.0/0=agentic-analytics MCP",)
READY_STATES = frozenset({"running", "idle"})
START_STATES = frozenset({"stopped"})
FAILED_STATES = frozenset({"failed", "terminated", "terminating", "deleted"})
RO_PASSWORD_CHARS = 28
_PW_SPECIAL = "-_.+"
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,49}$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
_IP_DESC = re.compile(r"^[A-Za-z0-9 _.,:/()-]{0,100}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# values from clickhousectl that go into .env, a hostname and SQL client: plain shapes only
_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})+$")
_SERVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")

CONSOLE_STEPS = """\
Two steps have no public API. Do them in the ClickHouse Cloud console \
(https://console.clickhouse.cloud):
  1. Turn on the remote MCP server for service '{service}': open the service, find its MCP \
setting (in the Connect dialog or the service settings) and enable it. Agents reach it at \
{mcp_url}.
  2. Bind the generic Query API endpoint to the MCP key and the read-only role: in service \
'{service}', open the Query API endpoint settings, enable the endpoint, add the API key {key}, \
and set its role to '{role}' (not sql_console_read_only or sql_console_admin: those would drop \
the {role} settings profile).
Then run `agentic-analytics doctor`: its clickhouse.mcp_role check passes only when MCP queries \
run as {role}.
"""
ADMIN_KEY_NOTE = (
    "The admin Cloud API key is no longer needed: delete it in the console (Organization > API "
    "keys), then remove CLICKHOUSE_CLOUD_API_KEY and CLICKHOUSE_CLOUD_API_SECRET from .env or run "
    "`clickhousectl cloud auth logout`."
)

AdminClientFactory = Callable[[str, str, str], Any]


@dataclass(frozen=True)
class ClickHouseOptions:
    name: str = DEFAULT_SERVICE
    region: str | None = None
    provider: str = "aws"
    ip_allow: tuple[str, ...] = ()
    key_ip_allow: tuple[str, ...] = DEFAULT_KEY_IP_ALLOW
    ro_user: str | None = None
    rotate_mcp_key: bool = False
    reset_admin_password: bool = False
    mcp_role_ids: tuple[str, ...] = ()
    pack: str = "uk_price_paid"
    wait_s: float = 900.0
    poll_s: float = 10.0


@dataclass
class ClickHouseResult:
    service: str
    service_id: str
    host: str
    created: bool
    mcp_key: Literal["created", "kept", "rotated"]
    mcp_key_name: str | None
    ro_user: str
    statements: int
    env_written: list[str] = field(default_factory=list)
    console_steps: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "engine": "clickhouse",
            "service": self.service,
            "service_id": self.service_id,
            "host": self.host,
            "created": self.created,
            "mcp_key": self.mcp_key,
            "mcp_key_name": self.mcp_key_name,
            "ro_user": self.ro_user,
            "role": CH_ROLE,
            "statements": self.statements,
            "env_written": sorted(set(self.env_written)),
            "console_steps": self.console_steps,
        }


def console_steps(service: str, key_name: str | None) -> str:
    key = f"'{key_name}'" if key_name else "whose id is CLICKHOUSE_MCP_KEY_ID in .env"
    return CONSOLE_STEPS.format(service=service, key=key, role=CH_ROLE, mcp_url=MCP_URL)


def generate_password(length: int = RO_PASSWORD_CHARS) -> str:
    """A random password with a lower, upper, digit and special character (ClickHouse Cloud's
    complexity rule), using only characters that need no quoting in SQL, `.env` or a shell."""
    alphabet = string.ascii_letters + string.digits + _PW_SPECIAL
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in _PW_SPECIAL for c in pw)
            and pw[0].isalnum()
        ):
            return pw


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_statements(sql: str) -> list[str]:
    """Statements of rendered role SQL: `--` comment lines dropped, split on `;`."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [" ".join(s.split()) for s in body.split(";") if s.strip()]


def user_statements(user: str, password: str) -> list[str]:
    """Create the read-only user, or set its password to `password` if it exists."""
    if not _IDENT.match(user):
        raise SetupError(f"read-only user {user!r} is not a plain identifier")
    pw = _sql_string(password)
    return [
        f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY {pw}",
        f"ALTER USER {user} IDENTIFIED BY {pw}",
    ]


def check_ip_allow(entries: Sequence[str], flag: str) -> tuple[str, ...]:
    for e in entries:
        cidr, _, desc = e.partition("=")
        try:
            ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError as exc:
            raise SetupError(f"{flag} {e!r}: {cidr!r} is not an IP address or CIDR") from exc
        if not _IP_DESC.match(desc):
            raise SetupError(f"{flag} {e!r}: the description has unexpected characters")
    return tuple(entries)


def _check_options(opts: ClickHouseOptions) -> None:
    if not _SERVICE_NAME.match(opts.name):
        raise SetupError(f"--name {opts.name!r}: use letters, digits, spaces, '-' or '_'")
    if opts.provider != "aws":
        raise SetupError("only --provider aws is supported (OWNER D-55)")
    if opts.region is not None and not _REGION.match(opts.region):
        raise SetupError(f"--region {opts.region!r} is not an AWS region such as eu-west-1")
    check_ip_allow(opts.ip_allow, "--ip-allow")
    check_ip_allow(opts.key_ip_allow, "--key-ip-allow")
    if not opts.key_ip_allow:
        raise SetupError("--key-ip-allow needs at least one entry: a key without one denies all")
    if opts.ro_user is not None and not _IDENT.match(opts.ro_user):
        raise SetupError(f"--ro-user {opts.ro_user!r} is not a plain identifier")
    for rid in opts.mcp_role_ids:
        if not _UUID.fullmatch(rid):  # fullmatch: `$` alone lets a trailing newline through
            raise SetupError(f"--mcp-role-id {rid!r} is not a UUID")


# ---- parsing clickhousectl output ------------------------------------------------------------


def _checked(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.fullmatch(value):  # fullmatch: no trailing newline either
        raise SetupError(f"clickhousectl returned an unexpected {what}; not using it")
    return value


def _services(listing: Any) -> list[dict[str, Any]]:
    return [r for r in find_records(listing, ["id", "name"]) if isinstance(r.get("name"), str)]


def _state(service: Mapping[str, Any]) -> str:
    value = record_field(service, "state", "status")
    return str(value).lower() if value is not None else ""


def https_host(obj: Any) -> str | None:
    """The service's HTTPS host from a `service get` / `create` document."""
    for ep in find_records(obj, ["protocol", "host"]):
        if str(ep.get("protocol", "")).lower() == "https" and isinstance(ep.get("host"), str):
            return str(ep["host"])
    host = find_value(obj, ["host", "hostname"])
    if isinstance(host, str) and host.endswith(".clickhouse.cloud"):
        return host
    return None


def mcp_role_ids(roles_doc: Any) -> list[str]:
    """The id of the org role the MCP key gets (`MCP_KEY_ROLE`), from `org role list`.

    Found by exact name; if a custom role has the same name, the `type: system` one is taken.
    Only top-level role records count: a role's `actors` and `policies` are never read as roles.
    """
    roles = [
        r
        for r in find_records(roles_doc, ["id", "name"], nested=False)
        if isinstance(r.get("name"), str)
    ]
    names = sorted({str(r["name"]) for r in roles})
    found = [r for r in roles if r["name"] == MCP_KEY_ROLE]
    if len(found) > 1:
        found = [r for r in found if str(record_field(r, "type") or "").lower() == "system"]
    if len(found) != 1:
        raise SetupError(
            f"`clickhousectl cloud org role list` did not show exactly one org role named "
            f"{MCP_KEY_ROLE!r} (roles seen: {', '.join(names) or 'none'}); pass the id of the "
            "role the MCP key should have with --mcp-role-id"
        )
    rid = str(found[0]["id"])
    if not _UUID.fullmatch(rid):
        raise SetupError(
            f"the id of org role {MCP_KEY_ROLE!r} from `org role list` is not a UUID; pass "
            "--mcp-role-id"
        )
    return [rid]


def mcp_roles_text(role_ids: Sequence[str], given: bool) -> str:
    """How the progress line names the key's roles: by name, or by id when given."""
    if not given:
        return f"org role {MCP_KEY_ROLE!r}"
    plural = "s" if len(role_ids) > 1 else ""
    return f"role id{plural} {', '.join(role_ids)} (from --mcp-role-id)"


# ---- the command -----------------------------------------------------------------------------


@dataclass
class _Ctx:
    opts: ClickHouseOptions
    ctl: Clickhousectl
    env_path: Path
    env: dict[str, str]
    box: SecretBox
    say: Callable[[str], None]
    written: list[str]

    def save(self, updates: Mapping[str, str]) -> None:
        self.written += update_env_file(self.env_path, updates)
        self.env.update(updates)

    def tell(self, text: str) -> None:
        self.say(self.box.scrub(text))


def _find_or_create_service(c: _Ctx) -> tuple[str, bool, str | None]:
    """(service id, created, admin password if it was just issued)."""
    listing = c.ctl.json("service", "list")
    matches = [s for s in _services(listing) if s["name"] == c.opts.name]
    if len(matches) > 1:
        raise SetupError(f"{len(matches)} services are named {c.opts.name!r}; pass a unique --name")
    if matches:
        sid = _checked(str(matches[0]["id"]), _SERVICE_ID, "service id")
        c.tell(f"adopting existing service {c.opts.name!r} ({sid})")
        return sid, False, None
    if c.opts.region is None:
        raise SetupError(
            f"no service named {c.opts.name!r} exists; pass --region (an AWS region such as "
            "eu-west-1) to create it"
        )
    args = ["service", "create", "--name", c.opts.name, "--provider", c.opts.provider]
    args += ["--region", c.opts.region]
    for entry in c.opts.ip_allow:
        args += ["--ip-allow", entry]
    created = c.ctl.json(*args)
    password = find_value(created, ["password", "defaultPassword", "default_password"])
    if isinstance(password, str):  # shown once: into .env before anything else can fail
        c.box.add(password)
        c.save({"CLICKHOUSE_ADMIN_USER": ADMIN_USER, "CLICKHOUSE_ADMIN_PASSWORD": password})
    services = [s for s in _services(created) if s["name"] == c.opts.name]
    sid = str(services[0]["id"]) if services else find_value(created, ["serviceId", "id"])
    if not isinstance(sid, str) or not sid:
        raise SetupError(
            "could not find the new service's id in the output of `clickhousectl cloud service "
            f"create` (keys seen: {key_names(created)}); run setup again to adopt it by name"
        )
    if not isinstance(password, str):
        raise SetupError(
            f"service {c.opts.name!r} ({sid}) was created but its one-time password was not in "
            f"the output (keys seen: {key_names(created)}); run setup again with "
            "--reset-admin-password"
        )
    updates = {"CLICKHOUSE_SERVICE_ID": _checked(sid, _SERVICE_ID, "service id")}
    host = https_host(created)
    if host:
        updates["CLICKHOUSE_HOST"] = _checked(host, _HOST, "service host")
    c.save(updates)
    c.tell(f"created service {c.opts.name!r} ({sid}); its admin password is saved in .env")
    if not c.opts.ip_allow:
        c.tell(
            "note: no --ip-allow given, so the service accepts connections from anywhere "
            "(0.0.0.0/0); restrict it in the console or pass --ip-allow next time"
        )
    return sid, True, password


def _wait_until_ready(
    c: _Ctx, sid: str, sleep: Callable[[float], None], clock: Callable[[], float]
) -> Any:
    deadline = clock() + c.opts.wait_s
    started = False
    last = ""
    while True:
        doc = c.ctl.json("service", "get", sid)
        records = [r for r in find_records(doc, ["id"]) if str(r.get("id")) == sid]
        state = _state(records[0]) if records else str(find_value(doc, ["state"]) or "").lower()
        if state in READY_STATES:
            return doc
        if state in FAILED_STATES:
            raise SetupError(f"service {sid} is {state}; check it in the console")
        if state in START_STATES and not started:
            c.tell(f"service {sid} is {state}; starting it")
            c.ctl.json("service", "start", sid)
            started = True
        if state != last:
            c.tell(f"waiting for service {sid}: {state or 'unknown state'}")
            last = state
        if clock() >= deadline:
            raise SetupError(
                f"service {sid} is still {state or 'not ready'} after {c.opts.wait_s:.0f} s; "
                "run setup again later (it adopts the service by name)"
            )
        sleep(c.opts.poll_s)


def _admin_password(c: _Ctx, sid: str, issued: str | None) -> str:
    if issued:
        return issued
    current = (c.env.get("CLICKHOUSE_ADMIN_PASSWORD") or "").strip()
    if current and not c.opts.reset_admin_password:
        return current
    if not c.opts.reset_admin_password:
        raise SetupError(
            "the service exists but CLICKHOUSE_ADMIN_PASSWORD is not in .env: its password was "
            "shown once when it was created. Add it to .env, or pass --reset-admin-password to "
            "issue a new one (this changes the default user's password)"
        )
    doc = c.ctl.json("service", "reset-password", sid)
    password = require_value(
        doc, ["password", "newPassword"], "the new password", "clickhousectl cloud service reset"
    )
    c.box.add(password)
    c.save({"CLICKHOUSE_ADMIN_USER": ADMIN_USER, "CLICKHOUSE_ADMIN_PASSWORD": password})
    c.tell("issued a new admin password; it is saved in .env")
    return password


def _mcp_key(c: _Ctx) -> tuple[Literal["created", "kept", "rotated"], str | None]:
    have = all(
        (c.env.get(n) or "").strip() for n in ("CLICKHOUSE_MCP_KEY_ID", "CLICKHOUSE_MCP_KEY_SECRET")
    )
    if have and not c.opts.rotate_mcp_key:
        c.tell("keeping the MCP API key already in .env (pass --rotate-mcp-key for a new one)")
        return "kept", None
    given = bool(c.opts.mcp_role_ids)
    role_ids = list(c.opts.mcp_role_ids) or mcp_role_ids(c.ctl.json("org", "role", "list"))
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    name = f"agentic-analytics-mcp-{stamp}"
    args = ["key", "create", "--name", name]
    for rid in role_ids:
        args += ["--role-id", rid]
    for entry in c.opts.key_ip_allow:
        args += ["--ip-allow", entry]
    doc = c.ctl.json(*args)
    secret = find_value(doc, ["keySecret", "key_secret", "secret"])
    key_id = find_value(doc, ["keyId", "key_id"])
    for value in (secret, key_id):
        c.box.add(value)
    if not isinstance(secret, str) or not isinstance(key_id, str):
        raise SetupError(
            f"API key {name!r} was created but its key id and secret were not both in the output "
            f"(keys seen: {key_names(doc)}); delete it in the console and run setup again"
        )
    c.save({"CLICKHOUSE_MCP_KEY_ID": key_id, "CLICKHOUSE_MCP_KEY_SECRET": secret})
    kind: Literal["created", "rotated"] = "rotated" if have else "created"
    c.tell(f"{kind} MCP API key {name!r} with {mcp_roles_text(role_ids, given)}; saved in .env")
    if have:
        c.tell("the previous MCP key still works: delete it in the console once nothing uses it")
    return kind, name


def _apply_role_sql(
    c: _Ctx, host: str, admin_password: str, factory: AdminClientFactory
) -> tuple[str, int]:
    from agentic_analytics.packs import PackError, load_pack

    ro_user = c.opts.ro_user or (c.env.get("CLICKHOUSE_RO_USER") or "").strip() or DEFAULT_RO_USER
    if not _IDENT.match(ro_user):
        raise SetupError(f"CLICKHOUSE_RO_USER {ro_user!r} is not a plain identifier")
    same_user = (c.env.get("CLICKHOUSE_RO_USER") or "").strip() == ro_user
    ro_password = (c.env.get("CLICKHOUSE_RO_PASSWORD") or "").strip() if same_user else ""
    if not ro_password:
        ro_password = generate_password()
    c.box.add(ro_password)
    try:
        pack = load_pack(c.opts.pack).manifest
    except PackError as exc:
        raise SetupError(str(exc)) from exc
    role_sql = pack_role_sql(pack, "clickhouse", user=ro_user)
    problems = role_sql_problems(role_sql, "clickhouse")
    if problems:  # never send role SQL that is not read-only
        raise SetupError("the role SQL is not read-only: " + "; ".join(problems))
    role_statements = sql_statements(role_sql)
    # check what is sent too: the splitter and the checker read comments differently
    problems = role_sql_problems("".join(f"{s};\n" for s in role_statements), "clickhouse")
    if problems:
        raise SetupError("the role SQL is not read-only: " + "; ".join(problems))
    statements = user_statements(ro_user, ro_password) + role_statements
    # .env first: if a statement fails, the next run reuses this password
    c.save({"CLICKHOUSE_RO_USER": ro_user, "CLICKHOUSE_RO_PASSWORD": ro_password})
    admin_user = (c.env.get("CLICKHOUSE_ADMIN_USER") or "").strip() or ADMIN_USER
    try:
        client = factory(host, admin_user, admin_password)
    except Exception as exc:
        raise SetupError(c.box.scrub(f"cannot connect to {host} as {admin_user}: {exc}")) from None
    for i, stmt in enumerate(statements, start=1):
        try:
            client.command(stmt)
        except Exception as exc:
            what = stmt.split(" IDENTIFIED", 1)[0]  # never the password literal
            # scrub first: a re-spaced or cut secret would no longer match
            msg = c.box.scrub(f"statement {i}/{len(statements)} ({what[:80]}) failed: {exc}")
            raise SetupError(" ".join(msg.split())[:500]) from None
    c.tell(
        f"read-only user {ro_user} and role {CH_ROLE} (settings profile, SELECT on the pack's "
        f"databases) applied in {len(statements)} statements; credentials saved in .env"
    )
    return ro_user, len(statements)


def clickhouse_admin_client(host: str, user: str, password: str) -> Any:  # pragma: no cover
    """A clickhouse-connect client as the admin (role setup only)."""
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=host,
        username=user,
        password=password,
        port=8443,
        secure=True,
        connect_timeout=60,
        send_receive_timeout=120,
    )


def setup_clickhouse(
    opts: ClickHouseOptions,
    *,
    ctl: Clickhousectl,
    env_path: Path,
    environ: Mapping[str, str],
    admin_client: AdminClientFactory = clickhouse_admin_client,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ClickHouseResult:
    """Provision or adopt the ClickHouse side; see the module docstring for the steps."""
    _check_options(opts)
    env = read_env_file(env_path)
    env.update({k: v for k, v in environ.items() if v and v.strip()})
    box = ctl.secrets
    for name, value in env.items():
        if name in ("CLICKHOUSE_MCP_KEY_ID", "CLICKHOUSE_MCP_KEY_SECRET") or re.search(
            r"(?i)KEY|SECRET|PASSWORD|PASSCODE|TOKEN", name
        ):
            box.add(value)
    c = _Ctx(opts, ctl, env_path, env, box, say, [])
    sid, created, issued = _find_or_create_service(c)
    doc = _wait_until_ready(c, sid, sleep, clock)
    host = https_host(doc)
    if not host:
        raise SetupError(
            f"could not find the HTTPS host of service {sid} in `clickhousectl cloud service get` "
            f"(keys seen: {key_names(doc)})"
        )
    _checked(host, _HOST, "service host")
    c.save(
        {"CLICKHOUSE_SERVICE_ID": sid, "CLICKHOUSE_HOST": host, "CLICKHOUSE_ADMIN_USER": ADMIN_USER}
    )
    admin_password = _admin_password(c, sid, issued)
    kind, key_name = _mcp_key(c)
    ro_user, n = _apply_role_sql(c, host, admin_password, admin_client)
    steps = console_steps(opts.name, key_name)
    return ClickHouseResult(
        service=opts.name,
        service_id=sid,
        host=host,
        created=created,
        mcp_key=kind,
        mcp_key_name=key_name,
        ro_user=ro_user,
        statements=n,
        env_written=c.written,
        console_steps=steps,
    )


__all__ = [
    "ADMIN_KEY_NOTE",
    "CONSOLE_STEPS",
    "DEFAULT_RO_USER",
    "MCP_KEY_ROLE",
    "ClickHouseOptions",
    "ClickHouseResult",
    "console_steps",
    "generate_password",
    "https_host",
    "mcp_role_ids",
    "mcp_roles_text",
    "setup_clickhouse",
    "sql_statements",
    "user_statements",
]
