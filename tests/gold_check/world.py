"""A five-question pack and a clean fake warehouse for the gold-check tests.

Questions (real bundled lines): q-001 count, q-002 median (0.5% tolerance), q-003 average,
q-026 relative to the as-of date, q-071 top 3, q-101 ambiguous. Quotas: aggregation 2, time 1,
top_n 1, ambiguous 1, so the selection keeps 5 of the 6 (two of the three aggregation
questions). Probes: p-iq01 (all backends: gold on both engines) and p-id01 (MCP agents only:
gold on ClickHouse only). Snowflake answers with uppercase column names, as it does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentic_analytics.gold import rewrite_limit
from agentic_analytics.packs import Pack, load_pack
from agentic_analytics.questions import load_questions
from agentic_analytics.schemas import Probe, Question, ResultSet
from tests.gold_check.fakes import (
    FakeExecutor,
    bound,
    bundled_probes,
    bundled_questions,
    rs,
    write_pack,
)

IDS = ("q-001", "q-002", "q-003", "q-026", "q-071", "q-101")
PROBE_IDS = ("p-iq01", "p-id01")
QUOTAS = {"aggregation": 2, "time": 1, "top_n": 1, "ambiguous": 1}

CH: dict[str, ResultSet] = {
    "q-001": rs(["sales"], [1_000_000]),
    "q-002": rs(["median_price"], [290_000.0]),
    "q-003": rs(["avg_price"], [612_345.5]),
    "q-026": rs(["sales"], [850_000]),
    "q-071": rs(
        ["type", "sales"], ["terraced", 300_000], ["semi-detached", 250_000], ["flat", 200_000]
    ),
}
SF: dict[str, ResultSet] = {
    "q-001": rs(["SALES"], [1_000_000]),
    "q-002": rs(["MEDIAN_PRICE"], [290_500.0]),  # 0.17% away: inside the 0.5% tolerance
    "q-003": rs(["AVG_PRICE"], [612_345.5]),
    "q-026": rs(["SALES"], [850_000]),
    "q-071": rs(
        ["TYPE", "SALES"], ["terraced", 300_000], ["semi-detached", 250_000], ["flat", 200_000]
    ),
}
TOP4 = rs(
    ["type", "sales"],
    ["terraced", 300_000],
    ["semi-detached", 250_000],
    ["flat", 200_000],
    ["detached", 150_000],
)


@dataclass
class GoldWorld:
    root: Path
    questions: list[Question]
    ch: FakeExecutor
    sf: FakeExecutor
    probes: list[Probe]

    @property
    def pack(self) -> Pack:
        return load_pack(self.root)

    def p(self, pid: str) -> Probe:
        return next(p for p in self.probes if p.id == pid)

    def q(self, qid: str) -> Question:
        return next(q for q in self.questions if q.id == qid)

    def executors(self, *engines: str) -> dict[str, FakeExecutor]:
        both = {"clickhouse": self.ch, "snowflake": self.sf}
        return {e: both[e] for e in (engines or ("clickhouse", "snowflake"))}

    def top_sql(self, engine: str = "clickhouse") -> str:
        q = self.q("q-071")
        return rewrite_limit(bound(q, engine), engine, 3)  # type: ignore[arg-type]


def make_world(tmp: Path) -> GoldWorld:
    questions = bundled_questions(*IDS)
    probes = bundled_probes(*PROBE_IDS)
    root = write_pack(tmp / "pack", questions, QUOTAS, probes=probes)
    questions = list(load_questions(load_pack(root)))
    ch = FakeExecutor("clickhouse")
    sf = FakeExecutor("snowflake")
    for q in questions:
        if q.expected != "answer":
            continue
        ch.set(bound(q, "clickhouse"), CH[q.id])
        sf.set(bound(q, "snowflake"), SF[q.id])
    # p-iq01's gold is q-001's SQL; p-id01 reads the probes table (ClickHouse only)
    ch.set(bound(probes[1], "clickhouse"), rs(["town", "sales"], ["LONDON", 1234]))
    world = GoldWorld(root, questions, ch, sf, probes)
    ch.set(world.top_sql(), TOP4)
    return world
