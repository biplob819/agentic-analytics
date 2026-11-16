"""Offline fakes for `setup`: a stateful fake ClickHouse Cloud behind a fake clickhousectl runner,
an admin client that records statements, and a fake Snowflake connector. Every secret they hand
out is a planted string, so tests can assert it never reaches any output."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentic_analytics.setup.clickhousectl import Completed

ADMIN_PW = "PlantedAdminPw-" + "Qx7" * 6
NEW_ADMIN_PW = "PlantedResetPw-" + "Lm4" * 6
KEY_ID = "PlantedKeyId" + "Vb2" * 6
KEY_SECRET = "4b1dPlantedKeySecret" + "Hn8" * 8
CLOUD_API_SECRET = "PlantedCloudApiSecret" + "Rt5" * 6
SF_PASSWORD = "PlantedSnowflakePw-" + "Gk3" * 5
SF_PASSCODE = "736251"
SERVICE_ID = "6f1c2e7a-0000-4000-8000-00000000abcd"
HOST = "abc123xyz.eu-west-1.aws.clickhouse.cloud"
TENANT_ID = "7e4a0000-0000-4000-8000-00000000e4a7"
MCP_ROLE_ID = "5a0e0000-0000-4000-8000-000000000012"  # "Basic Service API Reader" (fake id)
PLANTED = (ADMIN_PW, NEW_ADMIN_PW, KEY_ID, KEY_SECRET, CLOUD_API_SECRET, SF_PASSWORD)
# the system roles `clickhousectl cloud org role list --json` showed live (2026-11-17), in order
LIVE_ROLE_NAMES = (
    "Admin",
    "Basic Service API Reader",
    "Basic Service Reader",
    "Billing",
    "Member",
    "Organization API Reader",
    "Service API Admin",
    "Service API Reader",
    "Service Admin",
    "Service Reader",
)


def role_id(name: str) -> str:
    """A fake UUID per role name; the MCP role keeps the fixed MCP_ROLE_ID."""
    if name == "Basic Service API Reader":
        return MCP_ROLE_ID
    return f"5a0e0000-0000-4000-8000-{LIVE_ROLE_NAMES.index(name) + 1:012d}"


def live_role(name: str, *, type_: str = "system", actors: list[Any] | None = None) -> Any:
    """One role as the live `org role list --json` prints it (same keys, fake values)."""
    rid = role_id(name) if name in LIVE_ROLE_NAMES else "5a0e0000-0000-4000-8000-0000000c0570"
    policies = [
        {
            "allowDeny": "ALLOW",
            "permissions": [
                "control-plane:organization:view",
                "control-plane:organization:view-udfs",
            ],
            "resources": [f"organization/{TENANT_ID}"],
            "roleId": rid,
        },
        {
            "allowDeny": "ALLOW",
            "permissions": [
                "control-plane:service:view",
                "control-plane:service:view-schema-advisor",
                "control-plane:service:view-monitoring",
            ],
            "resources": ["instance/*"],
            "roleId": rid,
        },
    ]
    return {
        "actors": actors if actors is not None else [],
        "createdAt": "2025-01-01T00:00:00Z",
        "id": rid,
        "name": name,
        "ownerId": TENANT_ID,
        "policies": policies,
        "tenantId": TENANT_ID,
        "type": type_,
        "updatedAt": "2025-01-01T00:00:00Z",
    }


def live_roles() -> list[Any]:
    return [live_role(n) for n in LIVE_ROLE_NAMES]


def service_doc(sid: str, name: str, state: str) -> dict[str, Any]:
    return {
        "id": sid,
        "name": name,
        "provider": "aws",
        "region": "eu-west-1",
        "state": state,
        "endpoints": [
            {"protocol": "nativesecure", "host": HOST, "port": 9440},
            {"protocol": "https", "host": HOST, "port": 8443},
        ],
    }


@dataclass
class FakeCloud:
    """A stateful fake of the ClickHouse Cloud API as clickhousectl --json prints it.

    `shape` picks how documents are wrapped ("result" like the Cloud API, or "flat"), so the
    tolerant parser is exercised on both."""

    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    polls_until_running: int = 2
    shape: str = "result"
    roles: list[dict[str, Any]] = field(default_factory=live_roles)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    envs: list[Mapping[str, str]] = field(default_factory=list)
    fail: dict[str, Completed] = field(default_factory=dict)
    polls: int = 0
    keys_created: int = 0
    on_call: Callable[[tuple[str, ...]], None] | None = None

    def wrap(self, doc: Any) -> str:
        return json.dumps({"result": doc} if self.shape == "result" else doc)

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> Completed:
        assert argv[1] == "cloud" and argv[-1] == "--json", argv
        args = tuple(argv[2:-1])
        self.calls.append(args)
        self.envs.append(env)
        if self.on_call:
            self.on_call(args)
        for prefix, done in self.fail.items():
            if " ".join(args).startswith(prefix):
                return done
        return Completed(0, self.handle(args), "")

    def handle(self, args: tuple[str, ...]) -> str:
        match args[:2]:
            case ("service", "list"):
                return self.wrap(list(self.services.values()))
            case ("service", "create"):
                name = args[args.index("--name") + 1]
                doc = service_doc(SERVICE_ID, name, "provisioning")
                self.services[SERVICE_ID] = doc
                return self.wrap({"service": doc, "password": ADMIN_PW})
            case ("service", "get"):
                doc = self.services[args[2]]
                self.polls += 1
                if doc["state"] in ("provisioning", "starting") and (
                    self.polls >= self.polls_until_running
                ):
                    doc["state"] = "running"
                return self.wrap(doc)
            case ("service", "start"):
                self.services[args[2]]["state"] = "starting"
                return self.wrap({"ok": True})
            case ("service", "reset-password"):
                return self.wrap({"password": NEW_ADMIN_PW})
            case ("org", "role"):
                return self.wrap(self.roles)
            case ("key", "create"):
                self.keys_created += 1
                name = args[args.index("--name") + 1]
                return self.wrap(
                    {
                        "key": {"id": f"mgmt-{self.keys_created}", "name": name},
                        "keyId": KEY_ID,
                        "keySecret": KEY_SECRET,
                    }
                )
        raise AssertionError(f"unexpected clickhousectl call {args}")


class FakeAdminClient:
    """Records every statement; `fail_at` makes the n-th statement raise (1-based)."""

    def __init__(self, fail_at: int | None = None, error: str = "Code: 497. ACCESS_DENIED") -> None:
        self.commands: list[str] = []
        self.fail_at, self.error = fail_at, error
        self.opened: list[tuple[str, str, str]] = []

    def factory(self, host: str, user: str, password: str) -> FakeAdminClient:
        self.opened.append((host, user, password))
        return self

    def command(self, sql: str) -> None:
        self.commands.append(sql)
        if self.fail_at is not None and len(self.commands) == self.fail_at:
            raise RuntimeError(f"{self.error} while running {sql}")


class FakeCursor:
    def __init__(self, description: Any = None, rows: list[tuple[Any, ...]] | None = None):
        self.description = description
        self._rows = list(rows or [])

    def execute(self, sql: str) -> FakeCursor:
        self.sql = sql
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None


class FakeSnowflake:
    """`connect(**params)`: ACCOUNTADMIN password logins run execute_string; key-pair logins
    answer SELECT CURRENT_USER() with the user they logged in as."""

    def __init__(
        self,
        account_row: str = "MYORG-AB12345",
        fail_login: Exception | None = None,
        fail_key_login: Exception | None = None,
    ) -> None:
        self.account_row = account_row
        self.fail_login, self.fail_key_login = fail_login, fail_key_login
        self.connects: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self.closed = 0

    def __call__(self, **params: Any) -> Any:
        self.connects.append(dict(params))
        fake = self
        if params.get("authenticator") == "SNOWFLAKE_JWT":
            if self.fail_key_login:
                raise self.fail_key_login

            class KeyConn:
                def cursor(self) -> FakeCursor:
                    return FakeCursor(rows=[(params["user"],)])

                def close(self) -> None:
                    fake.closed += 1

            return KeyConn()
        if self.fail_login:
            raise self.fail_login

        class AdminConn:
            def execute_string(self, sql: str, remove_comments: bool = False) -> list[FakeCursor]:
                fake.scripts.append(sql)
                return [
                    FakeCursor(),
                    FakeCursor(description=[("SNOWFLAKE_ACCOUNT",)], rows=[(fake.account_row,)]),
                ]

            def close(self) -> None:
                fake.closed += 1

        return AdminConn()


__all__ = [
    "ADMIN_PW",
    "HOST",
    "KEY_ID",
    "KEY_SECRET",
    "LIVE_ROLE_NAMES",
    "MCP_ROLE_ID",
    "PLANTED",
    "SERVICE_ID",
    "FakeAdminClient",
    "FakeCloud",
    "FakeSnowflake",
    "live_role",
    "live_roles",
    "service_doc",
]
