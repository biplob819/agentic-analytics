"""Live: the real Cortex Analyst API and Snowflake executor (A2.2).

Needs `.env` with `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_RO_USER`, `SNOWFLAKE_RO_PRIVATE_KEY_PATH`,
`SNOWFLAKE_WAREHOUSE` (and optionally `AA_SEMANTIC_VIEW`); marked `live`, skipped unless
`AA_LIVE=1` (L-6), and excluded from CI (`pytest -m "not live"`).

Cost: at most four billed Cortex messages a run (about 0.27 trial credits at 0.067 a message)
plus seconds of XS warehouse: two answers (bank q-001 and the ambiguous q-101), one write probe
(p-w01) and one read timeout, counted as billed because the request reached Cortex. The
error-classification test sends only requests Cortex rejects (400, 401, 404) or never sends,
which are not billed.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

LIVE = pytest.mark.skipif(os.environ.get("AA_LIVE") != "1", reason="set AA_LIVE=1 to run")
PACK = Path(__file__).resolve().parents[2] / "src/agentic_analytics/packs/uk_price_paid"
pytestmark = [pytest.mark.live, LIVE]


def _env() -> dict[str, str]:  # pragma: no cover - needs real accounts
    importlib.import_module("agentic_analytics.env").load_dotenv()
    return {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_RO_USER"],
        "key": os.environ["SNOWFLAKE_RO_PRIVATE_KEY_PATH"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
        "view": os.environ.get("AA_SEMANTIC_VIEW", "ANALYTICS.UK.UK_PRICE_PAID_SEMANTIC"),
    }


def _client(e: dict[str, str], **kw: Any):  # pragma: no cover - needs real accounts
    from agentic_analytics.backends.cortex import CortexClient, KeyPairJwt

    jwt_source = kw.pop("jwt_source", None) or KeyPairJwt.from_key_file(
        e["account"], e["user"], e["key"]
    )
    return CortexClient(e["account"], kw.pop("view", e["view"]), jwt_source, **kw)


def _executor(e: dict[str, str]):  # pragma: no cover - needs real accounts
    from agentic_analytics.warehouse import SnowflakeExecutor

    return SnowflakeExecutor(e["account"], e["user"], e["key"], e["warehouse"], "ANALYTICS_RO")


def _bank(qid: str, file: str = "questions.jsonl") -> dict[str, Any]:  # pragma: no cover
    for line in (PACK / file).read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item["id"] == qid:
            return item
    raise KeyError(qid)


def test_live_cortex_answers_q001_correctly_and_handles_ambiguous_q101():  # pragma: no cover
    """Two billed messages: the SQL path end to end (guard, RO executor, template, gold) and
    the ambiguous path, where Cortex may ask back or pick a reading."""
    from agentic_analytics.backends.cortex import ask_cortex
    from agentic_analytics.schemas import ResultSet, Tolerance
    from agentic_analytics.scoring.result import match_result

    e = _env()
    client, executor = _client(e), _executor(e)
    q001 = _bank("q-001")
    answered = asyncio.run(ask_cortex(q001["text"], client, executor))
    assert answered.cortex_messages == 1
    assert answered.answer.sql is not None
    assert answered.execution is not None and answered.execution.error is None  # guard passes
    assert answered.answer.answer.startswith("The result is ")
    assert answered.answer.request_id and answered.answer.model_snapshot
    assert answered.api_latency_ms > 0 and answered.warehouse_elapsed_ms > 0
    assert answered.response is not None and answered.response.ignored == ()
    gold = json.loads((PACK / "gold/snowflake/q-001.json").read_text(encoding="utf-8"))
    ok, why = match_result(
        answered.execution,
        ResultSet.model_validate(gold["result"]),
        tolerance=Tolerance.model_validate(q001["tolerance"]),
        ordered=False,
    )
    assert ok, why

    vague = asyncio.run(ask_cortex(_bank("q-101")["text"], client, executor))
    assert vague.cortex_messages == 1
    assert vague.answer.error is None
    assert vague.answer.asked_for_clarification or vague.answer.sql is not None
    if vague.answer.sql is not None:  # a chosen reading must execute under the guard
        assert vague.execution is not None and vague.execution.error is None


def test_live_cortex_declines_the_write_probe_and_executes_nothing():  # pragma: no cover
    """One billed message. Recorded live: Cortex declines with text plus suggestions."""
    from agentic_analytics.backends.cortex import ask_cortex
    from tests.backends.cortex_fakes import FakeExecutor

    e = _env()
    executor = FakeExecutor()  # whatever Cortex returns must not reach a warehouse here
    run = asyncio.run(ask_cortex(_bank("p-w01", "probes.jsonl")["text"], _client(e), executor))
    assert run.cortex_messages == 1
    assert run.answer.sql is None and executor.calls == []
    assert run.answer.asked_for_clarification or run.answer.error == "refusal"
    assert run.answer.interpretation


def test_live_cortex_read_timeout_is_classified_timeout():  # pragma: no cover
    """At most one billed message: the request reaches Cortex, the 1 s read timeout fires."""
    from agentic_analytics.backends.cortex import ask_cortex
    from tests.backends.cortex_fakes import FakeExecutor

    e = _env()
    executor = FakeExecutor()
    client = _client(e, timeout_s=1.0)
    run = asyncio.run(ask_cortex(_bank("q-001")["text"], client, executor))
    assert run.answer.error == "timeout"
    assert run.cortex_messages == 1
    assert run.answer.sql is None and run.answer.answer == "" and executor.calls == []
    assert 900 <= run.api_latency_ms < 10_000


def test_live_cortex_errors_are_classified_config_and_leak_nothing():  # pragma: no cover
    """No billed messages: a malformed body (400), an unknown semantic view (404), a key
    Snowflake does not know (401) and a wrong key file path (never sent)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    from agentic_analytics.backends.cortex import (
        CortexClient,
        CortexConfigError,
        CortexHTTPError,
        KeyPairJwt,
        ask_cortex,
    )
    from agentic_analytics.models.errors import classify_error
    from tests.backends.cortex_fakes import FakeExecutor

    e = _env()
    executor = FakeExecutor()

    class Malformed(CortexClient):
        def request_body(self, question: str) -> dict[str, Any]:
            return {"messages": [], "semantic_view": self.semantic_view}

    def fails(client: CortexClient) -> CortexHTTPError:
        with pytest.raises(CortexHTTPError) as info:
            asyncio.run(ask_cortex("How many sales were there in 2024?", client, executor))
        assert classify_error(info.value) == "config"
        return info.value

    jwt_source = KeyPairJwt.from_key_file(e["account"], e["user"], e["key"])
    bad = fails(Malformed(e["account"], e["view"], jwt_source))
    assert (bad.status_code, bad.code) == (400, "390400")

    missing = fails(_client(e, view="ANALYTICS.UK.NO_SUCH_VIEW"))
    assert (missing.status_code, missing.code) == (404, "390404")

    stranger = KeyPairJwt(
        e["account"], e["user"], rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    denied = fails(_client(e, jwt_source=stranger))
    assert (denied.status_code, denied.code) == (401, "390144")
    assert stranger.token() not in str(denied) + repr(denied)

    with pytest.raises(CortexConfigError) as info:
        KeyPairJwt.from_key_file(e["account"], e["user"], e["key"] + ".missing")
    assert classify_error(info.value) == "config"
    assert e["key"] not in str(info.value) and info.value.__cause__ is None
    assert executor.calls == []
