"""A deterministic fake world for the runner tests and the golden run (R3.1).

Nothing here touches a network or an account. The pieces are the project's real code over the
committed fakes:

- MCP: the real N1.5 `Toolbox` over the in-process server of `tests/mcp/fake_server.py`; the
  only change (`RecordedToolbox`) replaces each call's wall-clock latency with the recorded one
  so summaries are byte-stable.
- Models: `ReplayModel` implements the `AgentModel` protocol from recorded turns keyed by the
  question text (the shape of `tests/models/fakes.py` turns, as plain JSON).
- Cortex: the real `CortexClient` and `ask_cortex` over an `httpx2.MockTransport` that replays
  recorded response bodies (`tests/backends/cortex_fakes.py` shapes).
- Warehouses: the real `ClickHouseExecutor` and `SnowflakeExecutor` (SQL guard included) over
  `tests/warehouse/fakes.py` drivers that answer recorded SQL; unknown SQL is a driver error.

`World.recording()` produces the recording as JSON; `services_from_recording()` replays one.
A2.1/A2.2 live recordings replace the fake-generated ones (see `tests/golden/record.py`).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import tempfile
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any

import httpx2
from clickhouse_connect.driver.exceptions import DatabaseError
from snowflake.connector.errors import ProgrammingError

from agentic_analytics.backends.cortex import CortexClient
from agentic_analytics.mcp import McpConnection, Toolbox, ToolOutcome
from agentic_analytics.models import Conversation, ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from agentic_analytics.packs import load_pack
from agentic_analytics.questions import bind_as_of, load_probes, load_questions
from agentic_analytics.run.gold import (
    GoldCheck,
    ProbeGold,
    compute_gold_check_id,
    probe_engines,
    write_gold_check,
)
from agentic_analytics.run.runner import RunOptions, Services, smoke_questions
from agentic_analytics.schemas import (
    ENGINES,
    Engine,
    GoldResult,
    Probe,
    Question,
    ResultSet,
    RunConfig,
)
from agentic_analytics.tracing import Redactor
from agentic_analytics.warehouse import ClickHouseExecutor, SnowflakeExecutor
from tests.backends.cortex_fakes import ACCOUNT, VIEW, make_jwt
from tests.backends.cortex_fakes import no_sleep as cortex_no_sleep
from tests.mcp import fake_server
from tests.models.fakes import no_sleep
from tests.warehouse.fakes import (
    FakeCHClient,
    FakeConnect,
    FakeConnection,
    FakeCursor,
    StepClock,
    ch_result,
    write_rsa_key,
)

SNAPSHOT = "5e" * 32
AS_OF = date(2026, 9, 30)
T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
MCP_LATENCY_MS = 85
ASK_WALL_MS = 2_400  # every ask's wall time under `fixed_stopwatch` (R3.1)
REGION = "AWS_AP_SOUTHEAST_7"  # what the fake Snowflake answers to CURRENT_REGION()
MODELS = {"claude_mcp": "claude-sonnet-5-5", "gpt_mcp": "gpt-6.1-sol"}
SNAPSHOTS = {"claude_mcp": "claude-sonnet-5-5-20261101", "gpt_mcp": "gpt-6.1-sol-2026-10-01"}
PROVIDERS = {"claude_mcp": "anthropic", "gpt_mcp": "openai"}
PROBE_IDS = ("p-w01", "p-iq01")
ROW_COUNT = 30_000_000
BUNDLED = load_pack("uk_price_paid")
TABLES = {"clickhouse": "uk.uk_price_paid", "snowflake": "ANALYTICS.UK.UK_PRICE_PAID"}


def _n(text: str, mod: int) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % mod


def gold_value(qid: str) -> int:
    return 150_000 + _n(qid, 300_000)


def gold_column(q: Question) -> str:
    return q.tolerance.columns[0] if q.tolerance.columns else "value"


def gold_rows(q: Question, multirow: bool = False) -> list[list[Any]]:
    """One value; with `multirow`, top-N questions get `top_n` ranked rows (label, value)."""
    v = gold_value(q.id)
    if q.top_n and multirow:
        return [[f"L{i:02d}", v - 7_000 * i] for i in range(q.top_n)]
    return [[v]]


def gold_columns(q: Question, multirow: bool = False) -> list[str]:
    return ["label", gold_column(q)] if q.top_n and multirow else [gold_column(q)]


def gold_result(q: Question, multirow: bool = False) -> ResultSet:
    return ResultSet(
        columns=tuple(gold_columns(q, multirow)),
        rows=tuple(tuple(r) for r in gold_rows(q, multirow)),
        ordered=q.ordered,
    )


# Outcomes per backend, by the question's position in its category (0 or 1) and category.
# Each backend gets right answers, a wrong one, an execution failure, a clarification and an
# error somewhere, so every summary metric has something to count.
def outcome(backend: str, q: Question, pos: int) -> str:
    if q.category == "ambiguous":
        return {"claude_mcp": "clarify", "gpt_mcp": "clarify" if pos == 0 else "interpret"}.get(
            backend, "clarify"
        )
    table = {
        "claude_mcp": {("yoy", 1): "wrong", ("geography", 0): "clarify"},
        "gpt_mcp": {("time", 1): "wrong", ("top_n", 0): "sql_error", ("yoy", 0): "refusal"},
        "cortex_analyst": {("top_n", 1): "wrong", ("aggregation", 1): "sql_error"},
    }
    return table[backend].get((q.category, pos), "correct")


def mcp_turn(
    calls: Sequence[Mapping[str, Any]], *, stop: str = "tool_use", text: str = "", seed: str = ""
) -> dict[str, Any]:
    t_in = 1500 + _n("in" + seed, 1500)
    return {
        "text": text,
        "calls": [dict(c) for c in calls],
        "stop": stop,
        "usage": {
            "in": t_in,
            "out": 60 + _n("out" + seed, 200),
            "reasoning": 0,
            "cached": t_in // 2,
        },
        "cost_usd": round(0.002 + _n("c" + seed, 4000) / 1_000_000, 6),
        "latency_ms": 700 + _n("l" + seed, 2000),
        "request_id": "req-" + hashlib.sha256(seed.encode()).hexdigest()[:10],
    }


def final_call(
    sql: str | None = None, answer: str | None = None, clarification: str | None = None
) -> dict[str, Any]:
    return {
        "name": "final_answer",
        "arguments": {
            "sql": sql,
            "answer": answer,
            "clarification": clarification,
            "suggestions": [],
        },
    }


def _per_trial(items: list[Any]) -> Any:
    """One recording for every trial, or {"trials": [...]} when trials differ."""
    return items[0] if len(items) == 1 else {"trials": items}


def fix_markers() -> dict[str, str]:
    """A line of each prompt fix block, by which the replay model knows a fix is applied."""
    from agentic_analytics.fixes import fix_blocks

    blocks = fix_blocks(BUNDLED)
    return {f: blocks[f].strip().splitlines()[0] for f in ("descriptions", "examples")}


@dataclass
class World:
    """`variants[(backend, qid)]` gives one outcome per trial (overriding `outcome`);
    `fix_variants[(backend, fix, qid)]` the outcomes under a fix (default: the baseline's).
    `edges` (the golden run) gives top-N questions ranked multi-row gold and makes a wrong
    figure clearly wrong (2v+1) rather than off by one."""

    questions: list[Question]
    probes: list[Probe]
    variants: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    fix_variants: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    edges: bool = False
    warehouse: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {"clickhouse": {}, "snowflake": {}}
    )

    @classmethod
    def from_bundled(cls, ids: Sequence[str] | None = None, **kw: Any) -> World:
        bank = load_questions(BUNDLED)
        qs = smoke_questions(bank) if ids is None else [q for q in bank if q.id in set(ids)]
        probes = [p for p in load_probes(BUNDLED) if p.id in PROBE_IDS]
        return cls(qs, probes, **kw)

    def positions(self) -> dict[str, int]:
        seen: dict[str, int] = {}
        out: dict[str, int] = {}
        for q in sorted(self.questions, key=lambda q: q.id):
            out[q.id] = seen.get(q.category, 0)
            seen[q.category] = out[q.id] + 1
        return out

    def gold(self, engines: Iterable[Engine] = ENGINES) -> dict[tuple[str, str], GoldResult]:
        out: dict[tuple[str, str], GoldResult] = {}
        for q in self.questions:
            if q.expected != "answer" or q.gold_sql is None:
                continue
            for e in engines:
                out[(q.id, e)] = GoldResult(
                    schema_version="1.0",
                    question_id=q.id,
                    engine=e,
                    sql_hash=hashlib.sha256(q.gold_sql[e].encode("utf-8")).hexdigest(),
                    result=gold_result(q, self.edges),
                    elapsed_ms=100,
                    executed_at=T0,
                )
        return out

    def probe_gold(self, engines: Iterable[Engine] = ENGINES) -> dict[tuple[str, str], ProbeGold]:
        """Stored gold for the probes that have gold SQL, on the engines their backends use."""
        out: dict[tuple[str, str], ProbeGold] = {}
        for p in self.probes:
            for e in probe_engines(p, tuple(engines)):
                assert p.gold_sql is not None
                out[(p.id, e)] = ProbeGold(
                    schema_version="1.0",
                    probe_id=p.id,
                    engine=e,
                    sql_hash=hashlib.sha256(p.gold_sql[e].encode("utf-8")).hexdigest(),
                    result=ResultSet(columns=("sales",), rows=((812_345,),)),
                    elapsed_ms=100,
                    executed_at=T0,
                )
        return out

    def _table(self, engine: str, sql: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]):
        self.warehouse[engine][sql] = {"columns": list(columns), "rows": [list(r) for r in rows]}

    def recording(self) -> dict[str, Any]:
        """Model turns per MCP backend and question text, Cortex bodies, warehouse answers."""
        pos = self.positions()
        agents: dict[str, dict[str, Any]] = {"claude_mcp": {}, "gpt_mcp": {}}
        fix_agents: dict[str, dict[str, dict[str, Any]]] = {}
        cortex: dict[str, Any] = {}
        for db, sql, col in (
            ("clickhouse", "SELECT count() AS n FROM uk.uk_price_paid", "n"),
            ("snowflake", "SELECT COUNT(*) AS N FROM ANALYTICS.UK.UK_PRICE_PAID", "N"),
        ):
            self._table(db, sql, [col], [[ROW_COUNT]])
        for q in self.questions:
            for backend in ("claude_mcp", "gpt_mcp", "cortex_analyst"):
                base = self.variants.get((backend, q.id), [outcome(backend, q, pos[q.id])])
                seed = f"{backend}:{q.id}"
                if backend == "cortex_analyst":
                    cortex[q.text] = _per_trial(
                        [
                            self._cortex_body(k, self._answer(k, q, "snowflake")[0], seed)
                            for k in base
                        ]
                    )
                    continue
                agents[backend][q.text] = _per_trial(
                    [self._agent_script(k, *self._answer(k, q, "clickhouse"), seed) for k in base]
                )
                for (b, fix, qid), kinds in sorted(self.fix_variants.items()):
                    if b == backend and qid == q.id:
                        fix_agents.setdefault(backend, {}).setdefault(fix, {})[q.text] = _per_trial(
                            [
                                self._agent_script(
                                    k, *self._answer(k, q, "clickhouse"), f"{seed}:{fix}"
                                )
                                for k in kinds
                            ]
                        )
        for p in self.probes:
            for backend in p.backends:
                seed = f"{backend}:{p.id}"
                if p.kind == "write":
                    if backend == "cortex_analyst":
                        cortex[p.text] = {
                            "message": {
                                "role": "analyst",
                                "content": [
                                    {"type": "text", "text": "I can only answer questions."}
                                ],
                            },
                            "request_id": "req-" + seed,
                            "warnings": [],
                            "response_metadata": {"model_names": ["claude-sonnet-5-5"]},
                        }
                    else:
                        agents[backend][p.text] = [
                            mcp_turn(
                                [final_call(clarification="I can only read data, not change it.")],
                                seed=seed,
                            )
                        ]
                    continue
                assert p.gold_sql is not None
                engine = "snowflake" if backend == "cortex_analyst" else "clickhouse"
                sql = p.gold_sql[engine]
                self._table(engine, sql, ["sales"], [[812_345]])
                if backend == "cortex_analyst":
                    cortex[p.text] = self._cortex_body("correct", sql, seed)
                else:
                    agents[backend][p.text] = self._agent_script("correct", sql, 812_345, seed)
        return {
            "note": (
                "Recorded from the fakes in tests/run/world.py (R3.1). A2.1 and A2.2 live "
                "recordings replace these; regenerate with `uv run python -m tests.golden.record`."
            ),
            "as_of": AS_OF.isoformat(),
            "mcp_latency_ms": MCP_LATENCY_MS,
            "models": MODELS,
            "snapshots": SNAPSHOTS,
            "agents": agents,
            "agents_fixes": fix_agents,
            "fix_markers": fix_markers() if fix_agents else {},
            "cortex": cortex,
            "warehouse": self.warehouse,
        }

    def _answer(self, kind: str, q: Question, engine: str) -> tuple[str | None, Any]:
        """The SQL an answer of this kind gives, and the figure its text quotes; the rows that
        SQL returns are recorded in the warehouse."""
        cols, rows = gold_columns(q, self.edges), gold_rows(q, self.edges)
        good = bind_as_of(q.gold_sql[engine], AS_OF) if q.gold_sql else None
        if kind in ("clarify", "refusal"):
            return None, None
        if kind == "sql_error":
            return f"SELECT missing_column FROM {TABLES[engine]}", None
        if kind == "interpret" or good is None:  # an answer to an ambiguous question
            assert q.interpretations
            sql = bind_as_of(q.interpretations[0][engine], AS_OF)
            self._table(engine, sql, ["value"], [[4242]])
            return sql, 4242
        inner = good.rstrip().rstrip(";")
        factor = {"tol_in": 1.004, "tol_out": 1.006}
        if kind == "correct":
            sql, out = good, rows
        elif kind in factor:  # just inside / just outside a 0.5% tolerance
            value = round(rows[0][-1] * factor[kind], 2)
            sql, out = f"SELECT {value} AS {cols[-1]}", [[value]]
        elif kind == "wrong" and len(rows) == 1:
            value = rows[0][-1] * 2 + 1 if self.edges else rows[0][-1] + 1
            sql, out = f"SELECT {value} AS {cols[-1]}", [[value]]
        elif kind == "wrong":  # a missing row
            sql, out = f"SELECT * FROM ({inner}) AS t LIMIT 1", rows[:1]
        elif kind == "permuted":  # the right rows in another order
            sql, out = f"SELECT * FROM ({inner}) AS t ORDER BY 2 ASC", list(reversed(rows))
        elif kind == "extra_row":  # the right rows and one more
            sql, out = f"SELECT * FROM ({inner}) AS t LIMIT 1000", [*rows, ["EXTRA", 1]]
        else:
            raise ValueError(kind)
        self._table(engine, sql, cols if len(out[0]) == len(cols) else cols[-1:], out)
        return sql, out[0][-1]

    @staticmethod
    def _agent_script(kind: str, sql: str | None, value: Any, seed: str) -> list[dict[str, Any]]:
        if kind == "refusal":
            return [mcp_turn([], stop="refusal", text="I can't help with that.", seed=seed)]
        if kind == "clarify":
            return [
                mcp_turn(
                    [final_call(clarification="Which period and property type do you mean?")],
                    seed=seed,
                )
            ]
        assert sql is not None
        look = {"name": "run_select_query", "arguments": {"query": sql}}
        return [
            mcp_turn([look], seed=seed + ":1"),
            mcp_turn([final_call(sql=sql, answer=f"The answer is {value}.")], seed=seed + ":2"),
        ]

    @staticmethod
    def _cortex_body(kind: str, sql: str | None, seed: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": "Interpretation: ..."}]
        if kind == "clarify" or (kind == "interpret" and sql is None):
            content.append({"type": "suggestions", "suggestions": ["Average price in 2024?"]})
        elif kind == "refusal":
            pass
        else:
            assert sql is not None
            content.append(
                {"type": "sql", "statement": sql, "confidence": {"verified_query_used": None}}
            )
        return {
            "message": {"role": "analyst", "content": content},
            "request_id": "req-" + hashlib.sha256(seed.encode()).hexdigest()[:10],
            "warnings": [],
            "response_metadata": {"model_names": ["claude-sonnet-5-5"]},
        }


# -- replay ----------------------------------------------------------------------------------


@dataclass
class Counter:
    model_steps: int = 0
    cortex_requests: int = 0
    executions: dict[str, int] = field(default_factory=lambda: {"clickhouse": 0, "snowflake": 0})
    mcp_connections: int = 0

    @property
    def total(self) -> int:
        return (
            self.model_steps
            + self.cortex_requests
            + sum(self.executions.values())
            + self.mcp_connections
        )


def _turn(data: Mapping[str, Any], index: int, snapshot: str) -> ModelTurn:
    u = data["usage"]
    calls = tuple(
        ToolCall(id=f"call-{index}-{i}", name=c["name"], arguments=dict(c["arguments"]))
        for i, c in enumerate(data["calls"])
    )
    return ModelTurn(
        text=data["text"],
        tool_calls=calls,
        stop=data["stop"],
        usage=Usage(
            tokens_in=u["in"],
            tokens_out=u["out"],
            tokens_reasoning=u["reasoning"],
            tokens_cached=u["cached"],
        ),
        cost_usd=data["cost_usd"],
        latency_ms=data["latency_ms"],
        model_snapshot=snapshot,
        request_id=data["request_id"],
    )


class ReplayModel:
    """`AgentModel` from recorded turns keyed by the question (user) text. Stateless across
    conversations, so concurrent workers can share it."""

    def __init__(
        self,
        backend: str,
        scripts: Mapping[str, Any],
        counter: Counter,
        *,
        fail_with: BaseException | None = None,
        fixes: Mapping[str, Mapping[str, Any]] | None = None,
        markers: Mapping[str, str] | None = None,
    ) -> None:
        self.provider = PROVIDERS[backend]
        self.model = MODELS[backend]
        self.effort = "medium"
        self.snapshot = SNAPSHOTS[backend]
        self.scripts, self.counter, self.fail_with = scripts, counter, fail_with
        self.fixes, self.markers = dict(fixes or {}), dict(markers or {})
        self.systems: list[str] = []
        self.starts: dict[tuple[str, str], int] = {}

    def start(self, system: str, user: str, tools: tuple[ToolSpec, ...]) -> Conversation:
        """Picks the recording for the fix the system prompt carries; a recording with
        per-trial scripts is replayed in start order (the golden run uses one worker)."""
        self.systems.append(system)
        table, variant = self.scripts, ""
        for fix, marker in sorted(self.markers.items()):
            if marker in system and user in self.fixes.get(fix, {}):
                table, variant = self.fixes[fix], fix
        entry = table[user]
        n = self.starts.get((variant, user), 0)
        self.starts[(variant, user)] = n + 1
        if isinstance(entry, dict):
            entry = entry["trials"][min(n, len(entry["trials"]) - 1)]
        conv = Conversation(system=system, tools=tools)
        conv.history.append({"script": entry, "turn": 0})
        return conv

    async def step(self, conv: Conversation) -> ModelTurn:
        self.counter.model_steps += 1
        if self.fail_with is not None:
            raise self.fail_with
        state = conv.history[0]
        script = state["script"]
        i = state["turn"]
        state["turn"] += 1
        # past the recording (a self-check feedback turn): the model repeats its last turn
        return _turn(script[min(i, len(script) - 1)], i, self.snapshot)

    def add_tool_results(self, conv: Conversation, results: list[ToolResult]) -> None:
        return None

    def add_user(self, conv: Conversation, text: str) -> None:
        return None


class RecordedToolbox(Toolbox):
    """The real Toolbox; each call's latency is the recorded one instead of the wall clock."""

    latency_ms = MCP_LATENCY_MS

    async def call(self, name: str, arguments: dict[str, Any] | None) -> ToolOutcome:
        outcome = await super().call(name, arguments)
        return dataclasses.replace(outcome, latency_ms=self.latency_ms)


def toolbox_source(cfg: RunConfig, counter: Counter, latency_ms: int = MCP_LATENCY_MS):
    @contextlib.asynccontextmanager
    async def source() -> AsyncIterator[Callable[[], Toolbox]]:
        counter.mcp_connections += 1
        async with McpConnection(server=fake_server.make_server()) as conn:

            def new() -> Toolbox:
                box = RecordedToolbox(
                    conn,
                    "svc-fake",
                    ("uk", "uk_probes"),
                    max_calls=cfg.max_tool_calls,
                    row_cap=cfg.row_cap_model,
                    retry={"sleep": no_sleep},
                )
                box.latency_ms = latency_ms
                return box

            yield new

    return source


@cache
def _rsa_key_path() -> str:
    folder = Path(tempfile.mkdtemp(prefix="aa-run-tests-"))
    return str(write_rsa_key(folder / "ro.p8"))


def _ch_handler(table: Mapping[str, Mapping[str, Any]], counter: Counter):
    def handler(sql: str, settings: dict[str, Any] | None) -> Any:
        counter.executions["clickhouse"] += 1
        hit = table.get(sql)
        if hit is None:
            return DatabaseError(
                "Code: 47. DB::Exception: Unknown identifier. (UNKNOWN_IDENTIFIER)"
            )
        return ch_result(hit["columns"], [tuple(r) for r in hit["rows"]])

    return handler


def _sf_handler(table: Mapping[str, Mapping[str, Any]], counter: Counter):
    from agentic_analytics.run.runner import REGION_SQL

    def handler(sql: str) -> FakeCursor:
        counter.executions["snowflake"] += 1
        if sql == REGION_SQL:  # the runner's OWNER D-62 region read at run start
            return FakeCursor(["REGION"], [(REGION,)])
        hit = table.get(sql)
        if hit is None:
            err = ProgrammingError("SQL compilation error: invalid identifier", errno=904)
            return FakeCursor(error=err)
        return FakeCursor(hit["columns"], [tuple(r) for r in hit["rows"]])

    return handler


def _cortex_transport(bodies: Mapping[str, Any], counter: Counter) -> httpx2.MockTransport:
    seen: dict[str, int] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        counter.cortex_requests += 1
        sent = json.loads(request.content)["messages"][0]["content"][0]["text"]
        question = sent.split("\n\n", 1)[1]
        body = bodies[question]
        if isinstance(body, dict) and "trials" in body:  # per-trial bodies, in request order
            n = seen.get(question, 0)
            seen[question] = n + 1
            body = body["trials"][min(n, len(body["trials"]) - 1)]
        return httpx2.Response(200, json=body)

    return httpx2.MockTransport(handler)


def services_from_recording(
    rec: Mapping[str, Any],
    cfg: RunConfig,
    counter: Counter | None = None,
    *,
    engines: Sequence[Engine] = ENGINES,
    fail_with: BaseException | None = None,
) -> Services:
    counter = counter if counter is not None else Counter()
    wh = rec["warehouse"]
    services = Services()
    if "clickhouse" in engines:
        services.clickhouse = ClickHouseExecutor(
            "fake-host",
            "analytics_ro",
            "not-a-password",
            client=FakeCHClient(handler=_ch_handler(wh["clickhouse"], counter)),
            clock=StepClock(),
        )
        services.toolbox_source = toolbox_source(cfg, counter, rec["mcp_latency_ms"])
        for b in ("claude_mcp", "gpt_mcp"):
            services.models[b] = ReplayModel(
                b,
                rec["agents"][b],
                counter,
                fail_with=fail_with,
                fixes=rec.get("agents_fixes", {}).get(b),
                markers=rec.get("fix_markers"),
            )
    if "snowflake" in engines:
        services.snowflake = SnowflakeExecutor(
            ACCOUNT,
            "AA_RO",
            _rsa_key_path(),
            "AA_XS",
            "ANALYTICS_RO",
            connect=FakeConnect(FakeConnection(handler=_sf_handler(wh["snowflake"], counter))),
            clock=StepClock(),
        )
        services.cortex = CortexClient(
            ACCOUNT,
            VIEW,
            make_jwt(),
            transport=_cortex_transport(rec["cortex"], counter),
            sleep=cortex_no_sleep,
        )
    return services


# -- packs and measurements ------------------------------------------------------------------


def write_pack(
    root: Path,
    world: World,
    *,
    engines: Sequence[Engine] = ENGINES,
    snapshot: str = SNAPSHOT,
    status: str = "pass",
    with_check: bool = True,
    as_of: date = AS_OF,
) -> Path:
    """A pack directory: the bundled manifest, the world's questions and probes, gold results
    for `engines` and a gold check over them."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.yaml").write_text(
        BUNDLED.root.joinpath("pack.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "questions.jsonl").write_text(
        "".join(q.model_dump_json() + "\n" for q in world.questions), encoding="utf-8"
    )
    (root / "probes.jsonl").write_text(
        "".join(p.model_dump_json() + "\n" for p in world.probes), encoding="utf-8"
    )
    gold: dict[tuple[str, str], Any] = {**world.gold(engines), **world.probe_gold(engines)}
    for (qid, engine), g in gold.items():
        d = root / "gold" / engine
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{qid}.json").write_text(
            json.dumps(g.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    listed = [p for p in world.probes if probe_engines(p, tuple(engines))]
    if with_check:
        check = GoldCheck(
            schema_version="1.0",
            kind="gold_check",
            pack="uk_price_paid@1.0",
            snapshot_sha256=snapshot,
            as_of=as_of,
            engines=tuple(e for e in ENGINES if e in engines),
            mode="cross_engine" if len(engines) == 2 else "single_engine",
            status=status,  # type: ignore[arg-type]
            questions=tuple(sorted(q.id for q in world.questions)),
            probes=tuple(sorted(p.id for p in listed)),
            failures=() if status == "pass" else ("q-001: not deterministic",),
            checked_at=T0,
            gold_check_id=compute_gold_check_id(
                world.questions, gold, snapshot, as_of=as_of, probes=listed
            ),
        )
        write_gold_check(root, check)
    return root


def write_measurements(
    folder: Path,
    *,
    snapshot: str = SNAPSHOT,
    engines: Sequence[Engine] = ENGINES,
    parity_status: str | None = None,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "snapshot-2026-11-01.json").write_text(
        json.dumps({"sha256": snapshot, "downloaded_at": "2026-11-01T09:00:00+00:00"}),
        encoding="utf-8",
    )
    status = parity_status or ("pass" if len(engines) == 2 else "single_engine")
    (folder / "parity-2026-11-01.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "kind": "parity",
                "pack": "uk_price_paid@1.0",
                "engines": list(engines),
                "status": status,
                "passed": status != "fail",
                "snapshot_sha256": snapshot,
                "generated_at": "2026-11-01T10:00:00+00:00",
                "fingerprints": [],
            }
        ),
        encoding="utf-8",
    )
    return folder


def config(**kw: Any) -> RunConfig:
    base: dict[str, Any] = dict(
        backends=("claude_mcp", "gpt_mcp", "cortex_analyst"),
        models=MODELS,
        trials=1,
        as_of=AS_OF,
        seed=7,
        concurrency={"claude_mcp": 2, "gpt_mcp": 2, "cortex_analyst": 2},
    )
    base.update(kw)
    if "backends" in kw:
        base["models"] = {k: v for k, v in MODELS.items() if k in base["backends"]}
        base["concurrency"] = {
            k: v for k, v in base["concurrency"].items() if k in base["backends"]
        }
    return RunConfig.model_validate(base)


class Clock:
    """A datetime clock that advances one second per call."""

    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def __call__(self) -> datetime:
        from datetime import timedelta

        out = self.t
        self.t = self.t + timedelta(seconds=1)
        return out


def fixed_now() -> datetime:
    return T0


def fixed_stopwatch(ms: int = ASK_WALL_MS) -> Callable[[], Callable[[], int]]:
    """A stopwatch factory whose every watch reads `ms`, so ask wall times (and Cortex latency
    in the summary) do not depend on the machine."""

    def watch() -> int:
        return ms

    return lambda: watch


def options(tmp: Path, **kw: Any) -> RunOptions:
    base: dict[str, Any] = dict(
        results_dir=tmp / "results",
        cache_dir=tmp / ".cache",
        measurements_dir=tmp / "measurements",
        ledger_path=tmp / "ledger.jsonl",
        now=fixed_now,
        stopwatch=fixed_stopwatch(),
        git_sha="abc1234",
        n_resamples=200,
        redactor=Redactor({}),
    )
    base.update(kw)
    return RunOptions(**base)


@dataclass
class Setup:
    """A ready-to-run tmp world: pack, measurements, recording and a call counter."""

    tmp: Path
    world: World
    rec: dict[str, Any]
    counter: Counter
    engines: tuple[Engine, ...] = ENGINES

    @property
    def pack_dir(self) -> Path:
        return self.tmp / "pack"

    def pack(self):
        return load_pack(self.pack_dir)

    def services(self, cfg: RunConfig, **kw: Any) -> Services:
        return services_from_recording(self.rec, cfg, self.counter, engines=self.engines, **kw)


def copy_bundled_fixes(pack_dir: Path, *, reason: str = "test freeze") -> None:
    """The bundled pack's fix files, load DDL and semantic.yaml, frozen afresh in this pack
    (never the bundled FROZEN.json, which F3.1 re-freezes on its own schedule)."""
    import shutil

    from agentic_analytics.fixes import freeze_fixes

    src = Path(str(BUNDLED.root))
    (pack_dir / "fixes").mkdir(parents=True, exist_ok=True)
    for f in (src / "fixes").glob("*.yaml"):
        shutil.copyfile(f, pack_dir / "fixes" / f.name)
    shutil.copytree(src / "load", pack_dir / "load", dirs_exist_ok=True)
    shutil.copyfile(src / "semantic.yaml", pack_dir / "semantic.yaml")
    freeze_fixes(load_pack(pack_dir), force=True, reason=reason, now=lambda: T0)


def setup(
    tmp: Path, *, engines: Sequence[Engine] = ENGINES, frozen: bool = True, **pack_kw: Any
) -> Setup:
    """A world pack (with the bundled frozen fixes unless `frozen=False`) and measurements."""
    world = World.from_bundled()
    write_pack(tmp / "pack", world, engines=engines, **pack_kw)
    if frozen:
        copy_bundled_fixes(tmp / "pack")
    write_measurements(tmp / "measurements", engines=engines)
    return Setup(tmp, world, world.recording(), Counter(), tuple(engines))
