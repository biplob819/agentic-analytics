"""Suite-wide guards.

N1.3 lesson L-6: once `load` was wired, an old test that called `cli.main(["load", ...])` as an
"unbuilt command" ran a real load from a background converge, because the CLI reads the repo's
`.env`. Outside `AA_LIVE=1` no offline test may reach the owner's accounts or download the
snapshot, whatever it calls, so this fixture refuses at every layer:

- the loaders' admin factories (`load.clickhouse.admin_client_from_env`,
  `load.snowflake.admin_connection_from_env`), with the message tests look for;
- the drivers themselves (`clickhouse_connect`'s `HttpClient` and Snowflake's
  `SnowflakeConnection` refuse to be built), so a factory imported by name before this fixture
  ran (`from agentic_analytics.load.clickhouse import admin_client_from_env`), N1.7's setup
  factories and the read-only executors cannot connect either;
- the snapshot download (`load.snapshot._urlopen`, the only network call behind
  `http_download` and `content_length`), so a fake Snowflake connection without an injected
  downloader cannot start the 5.5 GB download.

Tests that fake a driver patch it themselves after this fixture, which still works. Live tests
are gated by `AA_LIVE` themselves.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

OFFLINE_ADMIN_REFUSAL = "admin connection refused in an offline test (set AA_LIVE=1 for live runs)"
OFFLINE_NETWORK_REFUSAL = (
    "warehouse connection or snapshot download refused in an offline test "
    "(set AA_LIVE=1 for live runs)"
)


@pytest.fixture(autouse=True)
def _no_admin_connections_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("AA_LIVE") == "1":
        return
    from clickhouse_connect.driver.httpclient import HttpClient
    from snowflake.connector.connection import SnowflakeConnection

    from agentic_analytics.load import clickhouse, snapshot, snowflake
    from agentic_analytics.load._common import LoaderError

    def refuse_admin(*args: Any, **kwargs: Any) -> Any:
        raise LoaderError(OFFLINE_ADMIN_REFUSAL)

    def refuse_network(*args: Any, **kwargs: Any) -> Any:
        raise LoaderError(OFFLINE_NETWORK_REFUSAL)

    monkeypatch.setattr(clickhouse, "admin_client_from_env", refuse_admin)
    monkeypatch.setattr(snowflake, "admin_connection_from_env", refuse_admin)
    monkeypatch.setattr(HttpClient, "__init__", refuse_network)
    monkeypatch.setattr(SnowflakeConnection, "__init__", refuse_network)
    monkeypatch.setattr(snapshot, "_urlopen", refuse_network)
