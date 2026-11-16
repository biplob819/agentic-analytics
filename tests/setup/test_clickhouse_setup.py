"""SET-01, SET-02, SET-03, SET-05: `setup clickhouse` on a fake clickhousectl and admin client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.setup import SecretBox, SetupError, read_env_file
from agentic_analytics.setup import clickhouse as chs
from agentic_analytics.setup.clickhouse import (
    ClickHouseOptions,
    generate_password,
    https_host,
    mcp_role_ids,
    setup_clickhouse,
    sql_statements,
    user_statements,
)
from agentic_analytics.setup.clickhousectl import AUTH_HINT, Clickhousectl, Completed
from agentic_analytics.warehouse.roles import pack_role_sql
from tests.setup.fakes import (
    ADMIN_PW,
    CLOUD_API_SECRET,
    HOST,
    KEY_ID,
    KEY_SECRET,
    LIVE_ROLE_NAMES,
    MCP_ROLE_ID,
    NEW_ADMIN_PW,
    PLANTED,
    SERVICE_ID,
    FakeAdminClient,
    FakeCloud,
    live_role,
    live_roles,
    service_doc,
)

CREATE = ClickHouseOptions(region="eu-west-1", poll_s=0)


class Run:
    def __init__(self, tmp_path: Path, cloud: FakeCloud | None = None, **admin: Any) -> None:
        self.env_path = tmp_path / ".env"
        self.cloud = cloud or FakeCloud()
        self.admin = FakeAdminClient(**admin)
        self.lines: list[str] = []
        self.sleeps: list[float] = []
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += max(s, 1.0)  # poll_s=0 still moves time: a poll that never ends times out

    def __call__(
        self, opts: ClickHouseOptions = CREATE, environ: dict[str, str] | None = None
    ) -> chs.ClickHouseResult:
        environ = {"CLICKHOUSE_CLOUD_API_SECRET": CLOUD_API_SECRET, **(environ or {})}
        ctl = Clickhousectl(
            "/fake/clickhousectl", self.cloud, environ, SecretBox.from_environ(environ)
        )
        self.ctl = ctl
        return setup_clickhouse(
            opts,
            ctl=ctl,
            env_path=self.env_path,
            environ=environ,
            admin_client=self.admin.factory,
            say=self.lines.append,
            sleep=self.sleep,
            clock=self.clock,
        )

    @property
    def env(self) -> dict[str, str]:
        return read_env_file(self.env_path)

    def output(self, result: chs.ClickHouseResult | None = None) -> str:
        extra = json.dumps(result.to_dict()) if result else ""
        return "\n".join(self.lines) + extra


# ---- SET-01 service ----------------------------------------------------------------------------


def test_creates_service_saves_password_first_and_waits_until_running(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeCloud(polls_until_running=3))
    seen_at_first_poll: dict[str, str] = {}

    def on_call(args: tuple[str, ...]) -> None:
        if args[:2] == ("service", "get") and not seen_at_first_poll:
            seen_at_first_poll.update(read_env_file(run.env_path))

    run.cloud.on_call = on_call
    result = run()
    assert result.created and result.service_id == SERVICE_ID and result.host == HOST
    # the one-time password reached .env before anything else could fail
    assert seen_at_first_poll["CLICKHOUSE_ADMIN_PASSWORD"] == ADMIN_PW
    assert run.cloud.polls == 3 and len(run.sleeps) == 2
    env = run.env
    assert env["CLICKHOUSE_SERVICE_ID"] == SERVICE_ID
    assert env["CLICKHOUSE_HOST"] == HOST
    assert env["CLICKHOUSE_ADMIN_USER"] == "default"
    assert run.admin.opened == [(HOST, "default", ADMIN_PW)]
    kinds = [c[:2] for c in run.cloud.calls]
    assert kinds[:2] == [("service", "list"), ("service", "create")]


def test_clickhousectl_argv_uses_json_and_never_carries_a_secret(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run(ClickHouseOptions(region="eu-west-1", poll_s=0, ip_allow=("203.0.113.7/32=office",)))
    create = next(c for c in run.cloud.calls if c[:2] == ("service", "create"))
    assert create == (
        "service",
        "create",
        "--name",
        "agentic-analytics",
        "--provider",
        "aws",
        "--region",
        "eu-west-1",
        "--ip-allow",
        "203.0.113.7/32=office",
    )
    flat = " ".join(" ".join(c) for c in run.cloud.calls)
    assert "--api-key" not in flat and "--api-secret" not in flat
    for secret in PLANTED:
        assert secret not in flat
    # telemetry off for the child process (clickhousectl documents DO_NOT_TRACK)
    assert all(e.get("DO_NOT_TRACK") == "1" for e in run.cloud.envs)


def test_second_run_adopts_the_service_and_creates_nothing(tmp_path: Path) -> None:
    run = Run(tmp_path)
    first = run()
    env1 = run.env
    run.cloud.calls.clear()
    run.admin.commands.clear()
    second = run(ClickHouseOptions(poll_s=0))  # no --region needed to adopt
    assert first.created and not second.created
    assert second.mcp_key == "kept" and second.mcp_key_name is None
    kinds = {c[:2] for c in run.cloud.calls}
    assert ("service", "create") not in kinds and ("key", "create") not in kinds
    assert run.env == env1  # same password, same key, same read-only password
    assert run.admin.commands  # role SQL re-applied: idempotent statements only
    # never OR REPLACE: replacing analytics_ro drops ClickHouse Cloud's grant of it to the Query
    # API endpoint user, and MCP then runs with no role (found live)
    assert all(
        "IF NOT EXISTS" in c and "OR REPLACE" not in c
        for c in run.admin.commands
        if c.startswith("CREATE")
    )
    # (the user statements hold a random password, so only the role statements are searched)
    assert not any("REPLACE" in c.upper() for c in run.admin.commands if "IDENTIFIED" not in c)


@pytest.mark.parametrize("shape", ["result", "flat"])
def test_parser_finds_fields_in_several_json_shapes(tmp_path: Path, shape: str) -> None:
    run = Run(tmp_path, FakeCloud(shape=shape))
    result = run()
    assert (result.service_id, result.host, result.mcp_key) == (SERVICE_ID, HOST, "created")


def test_https_host_prefers_the_https_endpoint() -> None:
    doc = {"result": service_doc("x", "n", "running")}
    assert https_host(doc) == HOST
    assert https_host({"service": {"host": "h.clickhouse.cloud"}}) == "h.clickhouse.cloud"
    assert https_host({"service": {"host": "evil.example.com"}}) is None


def test_missing_field_fails_with_key_names_not_values(tmp_path: Path) -> None:
    cloud = FakeCloud()

    def broken(args: tuple[str, ...]) -> str:
        if args[:2] == ("service", "create"):
            return json.dumps(
                {"result": {"service": {"name": "agentic-analytics"}, "pw": ADMIN_PW}}
            )
        return FakeCloud.handle(cloud, args)

    cloud.handle = broken  # type: ignore[method-assign]
    run = Run(tmp_path, cloud)
    with pytest.raises(SetupError) as err:
        run()
    msg = str(err.value)
    assert "keys seen" in msg and "pw" in msg and ADMIN_PW not in msg


def test_non_json_output_is_a_clear_error(tmp_path: Path) -> None:
    cloud = FakeCloud(fail={"service list": Completed(0, "Services: none\n", "")})
    with pytest.raises(SetupError, match="did not print JSON"):
        Run(tmp_path, cloud)()


def test_auth_required_exit_4_gives_login_hint(tmp_path: Path) -> None:
    cloud = FakeCloud(fail={"service list": Completed(4, "", "auth required")})
    with pytest.raises(SetupError) as err:
        Run(tmp_path, cloud)()
    assert str(err.value) == AUTH_HINT and "--interactive" in AUTH_HINT


def test_clickhousectl_error_text_is_scrubbed(tmp_path: Path) -> None:
    cloud = FakeCloud(
        fail={"service list": Completed(1, "", f"rejected secret {CLOUD_API_SECRET} for key")}
    )
    with pytest.raises(SetupError) as err:
        Run(tmp_path, cloud)()
    assert CLOUD_API_SECRET not in str(err.value) and "[REDACTED]" in str(err.value)


def test_create_requires_region(tmp_path: Path) -> None:
    run = Run(tmp_path)
    with pytest.raises(SetupError, match="--region"):
        run(ClickHouseOptions(poll_s=0))
    assert [c[:2] for c in run.cloud.calls] == [("service", "list")]


@pytest.mark.parametrize(
    "opts",
    [
        ClickHouseOptions(name="bad;name"),
        ClickHouseOptions(region="Europe"),
        ClickHouseOptions(provider="gcp"),
        ClickHouseOptions(ip_allow=("not-an-ip",)),
        ClickHouseOptions(key_ip_allow=()),
        ClickHouseOptions(key_ip_allow=("1.2.3.4/32=bad'desc",)),
        ClickHouseOptions(ro_user="ro; DROP USER x"),
        ClickHouseOptions(mcp_role_ids=("member",)),
    ],
)
def test_invalid_options_refused_before_any_call(tmp_path: Path, opts: ClickHouseOptions) -> None:
    run = Run(tmp_path)
    with pytest.raises(SetupError):
        run(opts)
    assert run.cloud.calls == [] and not run.env_path.exists()


def test_two_services_with_the_same_name_are_refused(tmp_path: Path) -> None:
    cloud = FakeCloud()
    cloud.services = {
        "a": service_doc("a", "agentic-analytics", "running"),
        "b": service_doc("b", "agentic-analytics", "running"),
    }
    with pytest.raises(SetupError, match="unique --name"):
        Run(tmp_path, cloud)()


def test_adopted_service_without_admin_password_needs_reset_flag(tmp_path: Path) -> None:
    cloud = FakeCloud()
    cloud.services = {SERVICE_ID: service_doc(SERVICE_ID, "agentic-analytics", "running")}
    run = Run(tmp_path, cloud)
    with pytest.raises(SetupError, match="--reset-admin-password"):
        run(ClickHouseOptions(poll_s=0))
    assert ("service", "reset-password") not in {c[:2] for c in cloud.calls}


def test_reset_admin_password_saves_new_password(tmp_path: Path) -> None:
    cloud = FakeCloud()
    cloud.services = {SERVICE_ID: service_doc(SERVICE_ID, "agentic-analytics", "idle")}
    run = Run(tmp_path, cloud)
    result = run(ClickHouseOptions(poll_s=0, reset_admin_password=True))
    assert not result.created
    assert run.env["CLICKHOUSE_ADMIN_PASSWORD"] == NEW_ADMIN_PW
    assert run.admin.opened[0] == (HOST, "default", NEW_ADMIN_PW)
    assert NEW_ADMIN_PW not in run.output(result)


def test_stopped_service_is_started_then_waited_for(tmp_path: Path) -> None:
    cloud = FakeCloud(polls_until_running=3)
    cloud.services = {SERVICE_ID: service_doc(SERVICE_ID, "agentic-analytics", "stopped")}
    run = Run(tmp_path, cloud)
    run(ClickHouseOptions(poll_s=0), environ={"CLICKHOUSE_ADMIN_PASSWORD": ADMIN_PW})
    assert [c[:2] for c in cloud.calls].count(("service", "start")) == 1


def test_wait_times_out_with_clear_message(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeCloud(polls_until_running=10_000))
    with pytest.raises(SetupError, match="run setup again later"):
        run(ClickHouseOptions(region="eu-west-1", wait_s=30, poll_s=10))
    assert run.env["CLICKHOUSE_ADMIN_PASSWORD"] == ADMIN_PW  # kept for the rerun


def test_failed_state_stops(tmp_path: Path) -> None:
    cloud = FakeCloud()
    cloud.services = {SERVICE_ID: service_doc(SERVICE_ID, "agentic-analytics", "failed")}
    run = Run(tmp_path, cloud)
    with pytest.raises(SetupError, match="is failed; check it in the console"):
        run(ClickHouseOptions(poll_s=0))
    assert run.cloud.polls == 1 and run.sleeps == []  # stops at once, no waiting


# ---- SET-02 MCP key ------------------------------------------------------------------------------


def test_mcp_key_gets_the_basic_service_api_reader_role_and_ip_allow(tmp_path: Path) -> None:
    run = Run(tmp_path)
    result = run()
    create = next(c for c in run.cloud.calls if c[:2] == ("key", "create"))
    args = list(create)
    role_ids = [args[i + 1] for i, a in enumerate(args) if a == "--role-id"]
    ips = [args[i + 1] for i, a in enumerate(args) if a == "--ip-allow"]
    assert role_ids == [MCP_ROLE_ID]  # one least-privilege org role, found by name (live)
    assert ips == ["0.0.0.0/0=agentic-analytics MCP"]  # never empty: that would deny all
    assert result.mcp_key == "created" and result.mcp_key_name
    assert result.mcp_key_name.startswith("agentic-analytics-mcp-")
    assert run.env["CLICKHOUSE_MCP_KEY_ID"] == KEY_ID
    assert run.env["CLICKHOUSE_MCP_KEY_SECRET"] == KEY_SECRET


def test_role_lookup_fails_clearly_and_accepts_explicit_ids(tmp_path: Path) -> None:
    roles = [r for r in live_roles() if r["name"] != chs.MCP_KEY_ROLE]
    with pytest.raises(SetupError) as err:
        mcp_role_ids({"result": roles})
    msg = str(err.value)
    assert f"exactly one org role named {chs.MCP_KEY_ROLE!r}" in msg and "--mcp-role-id" in msg
    seen = msg.split("(roles seen: ", 1)[1].split(")", 1)[0]
    assert seen == ", ".join(sorted(n for n in LIVE_ROLE_NAMES if n != chs.MCP_KEY_ROLE))
    cloud = FakeCloud(roles=[])
    run = Run(tmp_path, cloud)
    explicit = ("44444444-4444-4444-8444-444444444444",)
    run(ClickHouseOptions(region="eu-west-1", poll_s=0, mcp_role_ids=explicit))
    assert ("org", "role") not in {c[:2] for c in cloud.calls}
    create = next(c for c in cloud.calls if c[:2] == ("key", "create"))
    assert create[create.index("--role-id") + 1] == explicit[0]
    # the progress line names the roles actually used: the given id, not the default role's name
    line = next(x for x in run.lines if "MCP API key" in x)
    assert f"with role id {explicit[0]} (from --mcp-role-id); saved in .env" in line
    assert chs.MCP_KEY_ROLE not in line and "Member" not in line and "Query Endpoints" not in line


# ---- the MCP key's role, on the live `org role list --json` shape (2026-11-17) ------------------


@pytest.mark.parametrize("wrap", ["bare", "result"])
def test_live_role_list_yields_the_basic_service_api_reader_id(wrap: str) -> None:
    roles = live_roles()
    assert [r["name"] for r in roles] == list(LIVE_ROLE_NAMES)
    assert set(roles[0]) == {
        "actors", "createdAt", "id", "name", "ownerId", "policies", "tenantId", "type", "updatedAt"
    }  # fmt: skip
    assert set(roles[0]["policies"][0]) == {"resources", "permissions", "allowDeny", "roleId"}
    doc = roles if wrap == "bare" else {"result": roles}
    assert mcp_role_ids(doc) == [MCP_ROLE_ID]
    assert chs.MCP_KEY_ROLE == "Basic Service API Reader"


def test_default_progress_line_names_the_role_by_name(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run()
    line = next(x for x in run.lines if "MCP API key" in x)
    assert line.startswith("created MCP API key 'agentic-analytics-mcp-")
    assert line.endswith(" with org role 'Basic Service API Reader'; saved in .env")
    assert "Member" not in line and "Query Endpoints" not in line and MCP_ROLE_ID not in line


def test_two_given_role_ids_are_both_named(tmp_path: Path) -> None:
    ids = (MCP_ROLE_ID, "44444444-4444-4444-8444-444444444444")
    run = Run(tmp_path)
    run(ClickHouseOptions(region="eu-west-1", poll_s=0, mcp_role_ids=ids))
    line = next(x for x in run.lines if "MCP API key" in x)
    assert line.endswith(f" with role ids {ids[0]}, {ids[1]} (from --mcp-role-id); saved in .env")
    create = list(next(c for c in run.cloud.calls if c[:2] == ("key", "create")))
    assert [create[i + 1] for i, a in enumerate(create) if a == "--role-id"] == list(ids)


@pytest.mark.parametrize(
    "name",
    ["Basic Service API Reader ", "basic service api reader", "Basic Service API Readers",
     "Service API Reader", "Basic Service Reader"],
)  # fmt: skip
def test_role_name_must_match_exactly(name: str) -> None:
    roles = [r for r in live_roles() if r["name"] != chs.MCP_KEY_ROLE]
    near = live_role("Organization API Reader")
    near["name"], near["id"] = name, MCP_ROLE_ID
    with pytest.raises(SetupError, match="exactly one org role named"):
        mcp_role_ids([*roles, near] if name not in LIVE_ROLE_NAMES else roles)


def test_a_custom_role_with_the_same_name_loses_to_the_system_role() -> None:
    custom = live_role("Basic Service API Reader", type_="custom")
    custom["id"] = "c0570000-0000-4000-8000-000000000001"
    assert mcp_role_ids([custom, *live_roles()]) == [MCP_ROLE_ID]
    twin = live_role("Basic Service API Reader")
    twin["id"] = "c0570000-0000-4000-8000-000000000002"
    with pytest.raises(SetupError, match="exactly one"):  # two system roles: never guess
        mcp_role_ids([*live_roles(), twin])


def test_actors_and_policies_are_never_read_as_roles() -> None:
    """A role's actors may carry an id and a name too; they are neither roles nor listed."""
    person = {"id": "a1a10000-0000-4000-8000-000000000001", "name": "Planted Person Name"}
    imposter = {"id": "a1a10000-0000-4000-8000-000000000002", "name": chs.MCP_KEY_ROLE}
    roles = [live_role(n, actors=[person, imposter]) for n in LIVE_ROLE_NAMES]
    assert mcp_role_ids(roles) == [MCP_ROLE_ID]
    without = [r for r in roles if r["name"] != chs.MCP_KEY_ROLE]
    with pytest.raises(SetupError) as err:
        mcp_role_ids({"result": without})
    assert "Planted Person Name" not in str(err.value)


def test_a_non_uuid_role_id_from_the_listing_is_refused() -> None:
    roles = live_roles()
    roles[1]["id"] = f"{MCP_ROLE_ID}\n--role-id x"
    assert roles[1]["name"] == chs.MCP_KEY_ROLE
    with pytest.raises(SetupError, match="is not a UUID; pass --mcp-role-id"):
        mcp_role_ids(roles)


def test_a_given_role_id_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    run = Run(tmp_path)
    with pytest.raises(SetupError, match="is not a UUID"):
        run(ClickHouseOptions(region="eu-west-1", mcp_role_ids=(MCP_ROLE_ID + "\n",)))
    assert run.cloud.calls == []


def test_existing_mcp_key_is_kept_unless_rotate(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run()
    assert run.cloud.keys_created == 1
    run(ClickHouseOptions(poll_s=0))
    assert run.cloud.keys_created == 1
    rotated = run(ClickHouseOptions(poll_s=0, rotate_mcp_key=True))
    assert run.cloud.keys_created == 2 and rotated.mcp_key == "rotated"
    assert any("previous MCP key still works" in line for line in run.lines)


def test_key_without_both_credentials_fails_without_echo(tmp_path: Path) -> None:
    cloud = FakeCloud()
    original = cloud.handle

    def no_id(args: tuple[str, ...]) -> str:
        if args[:2] == ("key", "create"):
            return json.dumps({"keySecret": KEY_SECRET, "key": {"id": "m", "name": "k"}})
        return original(args)

    cloud.handle = no_id  # type: ignore[method-assign]
    run = Run(tmp_path, cloud)
    with pytest.raises(SetupError) as err:
        run()
    assert KEY_SECRET not in str(err.value) and "delete it in the console" in str(err.value)
    assert "CLICKHOUSE_MCP_KEY_SECRET" not in run.env


@pytest.mark.safety
def test_key_secret_and_id_never_printed(tmp_path: Path) -> None:
    run = Run(tmp_path)
    result = run()
    out = run.output(result) + result.console_steps
    for secret in PLANTED:
        assert secret not in out
    assert run.env["CLICKHOUSE_MCP_KEY_SECRET"] == KEY_SECRET


# ---- SET-03 read-only user and role ---------------------------------------------------------


def test_role_sql_from_roles_py_is_sent_with_the_admin_password(tmp_path: Path) -> None:
    run = Run(tmp_path)
    result = run()
    env = run.env
    ro_user, ro_pw = env["CLICKHOUSE_RO_USER"], env["CLICKHOUSE_RO_PASSWORD"]
    assert ro_user == "aa_ro_user" == result.ro_user
    cmds = run.admin.commands
    assert cmds[0] == f"CREATE USER IF NOT EXISTS aa_ro_user IDENTIFIED BY '{ro_pw}'"
    assert cmds[1] == f"ALTER USER aa_ro_user IDENTIFIED BY '{ro_pw}'"
    assert "GRANT SELECT ON uk.* TO analytics_ro" in cmds
    assert "GRANT SELECT ON uk_probes.* TO analytics_ro" in cmds
    assert "GRANT analytics_ro TO aa_ro_user" in cmds
    assert result.statements == len(cmds)


def test_statements_equal_roles_py_rendering(tmp_path: Path) -> None:
    from agentic_analytics.packs import load_pack

    run = Run(tmp_path)
    run(ClickHouseOptions(region="eu-west-1", poll_s=0, ro_user="custom_ro"))
    expected = sql_statements(
        pack_role_sql(load_pack("uk_price_paid").manifest, "clickhouse", user="custom_ro")
    )
    assert run.admin.commands[2:] == expected
    assert expected[:2] == [
        "CREATE SETTINGS PROFILE IF NOT EXISTS analytics_ro_profile",
        "ALTER SETTINGS PROFILE analytics_ro_profile SETTINGS readonly = 1 MIN 1 MAX 2 "
        "CHANGEABLE_IN_READONLY, max_execution_time = 30 MIN 1 MAX 300 CHANGEABLE_IN_READONLY, "
        "max_result_rows MIN 1 MAX 10001 CHANGEABLE_IN_READONLY, result_overflow_mode "
        "CHANGEABLE_IN_READONLY, send_progress_in_http_headers CHANGEABLE_IN_READONLY, "
        "http_headers_progress_interval_ms CHANGEABLE_IN_READONLY, max_memory_usage CONST, "
        "max_threads CONST, max_bytes_to_read CONST, max_rows_to_read CONST, "
        "max_result_bytes CONST",
    ]
    assert expected[2:4] == [
        "CREATE ROLE IF NOT EXISTS analytics_ro",
        "ALTER ROLE analytics_ro SETTINGS PROFILE 'analytics_ro_profile'",
    ]
    assert all(not s.startswith("--") and ";" not in s for s in expected)


def test_rerun_reuses_the_ro_password_from_env(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run()
    pw = run.env["CLICKHOUSE_RO_PASSWORD"]
    run(ClickHouseOptions(poll_s=0))
    assert run.env["CLICKHOUSE_RO_PASSWORD"] == pw
    # a different user name gets a fresh password, never the other user's
    run(ClickHouseOptions(poll_s=0, ro_user="other_ro"))
    assert run.env["CLICKHOUSE_RO_USER"] == "other_ro"
    assert run.env["CLICKHOUSE_RO_PASSWORD"] != pw


@pytest.mark.safety
def test_non_read_only_role_sql_is_never_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = chs.pack_role_sql

    def widened(*a: Any, **k: Any) -> str:
        return real(*a, **k).replace("GRANT SELECT ON uk.*", "GRANT SELECT, INSERT ON uk.*")

    monkeypatch.setattr(chs, "pack_role_sql", widened)
    run = Run(tmp_path)
    with pytest.raises(SetupError, match="not read-only"):
        run()
    assert run.admin.commands == [] and run.admin.opened == []


@pytest.mark.safety
def test_failed_statement_error_hides_the_password(tmp_path: Path) -> None:
    run = Run(tmp_path, fail_at=1)
    with pytest.raises(SetupError) as err:
        run()
    msg = str(err.value)
    ro_pw = run.env["CLICKHOUSE_RO_PASSWORD"]
    assert (
        ro_pw not in msg and "statement 1/" in msg and "CREATE USER IF NOT EXISTS aa_ro_user" in msg
    )


def test_generated_password_meets_cloud_complexity_and_needs_no_quoting() -> None:
    for _ in range(200):
        pw = generate_password()
        assert len(pw) == 28 and pw[0].isalnum()
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw) and any(c in "-_.+" for c in pw)
        assert all(c.isalnum() or c in "-_.+" for c in pw)


def test_existing_password_with_quotes_is_escaped_in_sql(tmp_path: Path) -> None:
    run = Run(tmp_path)
    tricky = "ab'c-Long-Enough-1"
    run(environ={"CLICKHOUSE_RO_USER": "aa_ro_user", "CLICKHOUSE_RO_PASSWORD": tricky})
    assert run.admin.commands[0] == (
        "CREATE USER IF NOT EXISTS aa_ro_user IDENTIFIED BY 'ab\\'c-Long-Enough-1'"
    )
    assert read_env_file(run.env_path)["CLICKHOUSE_RO_PASSWORD"] == tricky
    assert (
        user_statements("u", "a\\b'c")[0]
        == "CREATE USER IF NOT EXISTS u IDENTIFIED BY 'a\\\\b\\'c'"
    )


# ---- SET-05 console steps ------------------------------------------------------------------------

# Written out independently of the module's template (EK L-10).
EXPECTED_STEPS = (
    "Two steps have no public API. Do them in the ClickHouse Cloud console "
    "(https://console.clickhouse.cloud):\n"
    "  1. Turn on the remote MCP server for service 'agentic-analytics': open the service, find "
    "its MCP setting (in the Connect dialog or the service settings) and enable it. Agents reach "
    "it at https://mcp.clickhouse.cloud/mcp.\n"
    "  2. Bind the generic Query API endpoint to the MCP key and the read-only role: in service "
    "'agentic-analytics', open the Query API endpoint settings, enable the endpoint, add the API "
    "key 'KEYNAME', and set its role to 'analytics_ro' (not sql_console_read_only or "
    "sql_console_admin: those would drop the analytics_ro settings profile).\n"
    "Then run `agentic-analytics doctor`: its clickhouse.mcp_role check passes only when MCP "
    "queries run as analytics_ro.\n"
)


def test_console_steps_are_printed_word_for_word(tmp_path: Path) -> None:
    run = Run(tmp_path)
    result = run()
    assert result.mcp_key_name
    assert result.console_steps == EXPECTED_STEPS.replace("KEYNAME", result.mcp_key_name)


def test_kept_key_steps_name_the_env_variable_not_the_id(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run()
    again = run(ClickHouseOptions(poll_s=0))
    assert again.console_steps == EXPECTED_STEPS.replace(
        "key 'KEYNAME'", "key whose id is CLICKHOUSE_MCP_KEY_ID in .env"
    )
    assert KEY_ID not in again.console_steps


@pytest.mark.safety
def test_console_steps_never_suggest_the_legacy_roles(tmp_path: Path) -> None:
    run = Run(tmp_path)
    result = run()
    steps = result.console_steps
    assert "set its role to 'analytics_ro'" in steps
    assert "query-endpoint" not in " ".join(" ".join(c) for c in run.cloud.calls)
    assert steps.count("sql_console_") == 2 and "(not sql_console_read_only" in steps


def test_sql_statements_drop_comments_and_split() -> None:
    sql = "-- c1\nCREATE ROLE x;\n  -- c2\nGRANT SELECT ON a.* TO x;\n"
    assert sql_statements(sql) == ["CREATE ROLE x", "GRANT SELECT ON a.* TO x"]
