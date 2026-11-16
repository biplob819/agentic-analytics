"""Live proofs for N1.4 on the Snowflake trial account (SEM-02). Skipped unless AA_LIVE=1. They
use the ADMIN key pair from `.env` (read inside each test, never at import, L-6) and need the N1.3
load and the N1.2 role to exist. The view is created and granted as SYSADMIN, its owner."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.load.snowflake import admin_connection_from_env
from agentic_analytics.packs import load_pack
from agentic_analytics.semantic import (
    comment_text,
    default_names,
    load_semantic,
    semantic_view_ddl,
    semantic_view_grant_sql,
)
from agentic_analytics.semantic.snowflake import SF_ROLE

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="live: set AA_LIVE=1")
ROOT = Path(__file__).resolve().parents[2]
ADMIN_ROLE = "SYSADMIN"


def _admin_connection() -> Any:  # pragma: no cover - needs an account
    from agentic_analytics.env import load_dotenv

    load_dotenv(ROOT / ".env")
    return admin_connection_from_env()


@pytest.mark.live
@LIVE
def test_live_semantic_view_created_and_granted() -> None:  # pragma: no cover - needs an account
    pack = load_pack("uk_price_paid")
    model = load_semantic(pack)
    view, table = default_names(pack, model)
    conn = _admin_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"USE ROLE {ADMIN_ROLE}")
        cur.execute(semantic_view_ddl(model, view, table))
        cur.execute(semantic_view_grant_sql(view))
        cur.execute(f"SHOW GRANTS ON SEMANTIC VIEW {view}")
        grants = {(str(r[1]).upper(), str(r[5]).upper()) for r in cur.fetchall()}
        cur.execute(f"DESCRIBE SEMANTIC VIEW {view}")
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    assert ("SELECT", SF_ROLE) in grants
    # DESCRIBE rows: object_kind, object_name, parent_entity, property, property_value
    described: dict[tuple[str, str], dict[str, str]] = {}
    for kind, name, _parent, prop, value in (tuple(r[:5]) for r in rows):
        described.setdefault((str(kind or "VIEW"), str(name or "").lower()), {})[prop] = value
    for kind, items in (
        ("DIMENSION", model.dimensions),
        ("FACT", model.facts),
        ("METRIC", model.metrics),
    ):
        for item in items:
            props = described.get((kind, item.name))
            assert props is not None, item.name
            assert props.get("COMMENT") == comment_text(item), item.name
            raw = props.get("SYNONYMS")
            assert set(json.loads(raw) if raw else ()) == set(item.synonyms), item.name
    assert described[("VIEW", "")]["COMMENT"] == model.description


@pytest.mark.live
@LIVE
def test_live_sample_values_exist_in_the_data() -> None:  # pragma: no cover - needs an account
    pack = load_pack("uk_price_paid")
    model = load_semantic(pack)
    _, table = default_names(pack, model)
    conn = _admin_connection()
    cur = conn.cursor()
    missing: list[str] = []
    try:
        for dim in model.dimensions:
            for value in dim.sample_values:
                cur.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE TO_VARCHAR({dim.column}) = %s",
                    (value,),
                )
                row = cur.fetchone()
                if not row or row[0] == 0:
                    missing.append(f"{dim.name}={value}")
    finally:
        cur.close()
        conn.close()
    assert missing == []
