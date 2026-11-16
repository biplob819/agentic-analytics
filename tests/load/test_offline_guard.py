"""L-6: outside AA_LIVE=1 no offline test reaches an account or downloads the snapshot, whatever
route it takes (tests/conftest.py). Every target here is unreachable on purpose (`.invalid`
hosts, a closed local port), so a broken guard fails these tests instead of going live."""

from __future__ import annotations

import os
import re
import shutil
from importlib import resources
from pathlib import Path

import pytest

# Imported by name at collection time, before the guard fixture runs (as tests/load/test_live.py
# and tests/semantic/test_live.py do): patching the module attribute cannot reach these names.
from agentic_analytics.load.clickhouse import admin_client_from_env as ch_factory_by_name
from agentic_analytics.load.snapshot import content_length, http_download
from agentic_analytics.load.snowflake import admin_connection_from_env as sf_factory_by_name
from agentic_analytics.load.snowflake import load_snowflake
from agentic_analytics.packs import load_pack
from tests.conftest import OFFLINE_NETWORK_REFUSAL
from tests.load.fakes import FakeSnowflake

pytestmark = [
    pytest.mark.safety,
    pytest.mark.skipif(os.environ.get("AA_LIVE") == "1", reason="offline guard only"),
]
CLOSED = "http://127.0.0.1:9/pp-complete.csv"  # the discard port: nothing listens there


def _snowflake_key(tmp_path: Path) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "throwaway.p8"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def test_factories_imported_by_name_still_cannot_connect(tmp_path: Path) -> None:
    ch_env = {
        "CLICKHOUSE_HOST": "ch.invalid",
        "CLICKHOUSE_ADMIN_USER": "default",
        "CLICKHOUSE_ADMIN_PASSWORD": "not-a-password",
    }
    with pytest.raises(Exception, match=re.escape(OFFLINE_NETWORK_REFUSAL)):
        ch_factory_by_name(ch_env)
    sf_env = {
        "SNOWFLAKE_ACCOUNT": "aa-invalid",
        "SNOWFLAKE_ADMIN_USER": "AA_ADMIN_USER",
        "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH": str(_snowflake_key(tmp_path)),
        "SNOWFLAKE_WAREHOUSE": "ANALYTICS_XS",
    }
    with pytest.raises(Exception, match=re.escape(OFFLINE_NETWORK_REFUSAL)):
        sf_factory_by_name(sf_env)


def test_the_drivers_refuse_to_connect() -> None:
    """Covers every other admin route (N1.7's setup factories, role setup) and the read-only
    executors: all of them build one of these two driver objects."""
    import clickhouse_connect
    import snowflake.connector

    with pytest.raises(Exception, match=re.escape(OFFLINE_NETWORK_REFUSAL)):
        clickhouse_connect.get_client(host="ch.invalid", username="u", password="p")
    with pytest.raises(Exception, match=re.escape(OFFLINE_NETWORK_REFUSAL)):
        snowflake.connector.connect(account="aa-invalid", user="u", password="p")


def test_the_snapshot_download_and_size_probe_are_refused(tmp_path: Path) -> None:
    with pytest.raises(Exception, match=re.escape(OFFLINE_NETWORK_REFUSAL)):
        http_download(CLOSED, tmp_path / "pp-complete.csv")
    with pytest.raises(Exception, match=re.escape(OFFLINE_NETWORK_REFUSAL)):
        content_length(CLOSED)
    assert not (tmp_path / "pp-complete.csv").exists()


def test_a_fake_snowflake_without_a_downloader_never_downloads(tmp_path: Path) -> None:
    """The L-6 incident's second half: a load on a fake connection that forgot to inject the
    downloader would fetch the real 5.5 GB file through the default `http_download`. The pack
    copy reads a closed local port, so a broken guard fails here and fetches nothing."""
    src = resources.files("agentic_analytics.packs") / "uk_price_paid"
    pack_dir = tmp_path / "pack"
    with resources.as_file(src) as p:
        shutil.copytree(p, pack_dir)
    sql = pack_dir / "load" / "clickhouse.sql"
    text, n = re.subn(r"'http://[^']+'", f"'{CLOSED}'", sql.read_text(encoding="utf-8"))
    assert n == 1
    sql.write_text(text, encoding="utf-8")
    pack = load_pack(pack_dir)
    conn = FakeSnowflake(pack)
    with pytest.raises(Exception, match=re.escape(OFFLINE_NETWORK_REFUSAL)):
        load_snowflake(pack, conn, workdir=tmp_path / "snap")
    assert not any(s.lstrip().upper().startswith("PUT") for s in conn.statements)
