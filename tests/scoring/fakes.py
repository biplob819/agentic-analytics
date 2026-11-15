"""A scripted executor that satisfies the scoring `Executor` protocol without a warehouse."""

from __future__ import annotations

from dataclasses import dataclass, field

from agentic_analytics.schemas import Engine, Execution, ResultSet


@dataclass
class FakeExecutor:
    engine: Engine = "clickhouse"
    result: ResultSet | None = None
    error: str | None = None
    rows_total: int | None = None
    echo_sql: bool = True
    report_engine: Engine | None = None  # an engine to report instead of its own
    calls: list[tuple[str, int]] = field(default_factory=list)

    def execute(self, sql: str, *, row_cap: int) -> Execution:
        self.calls.append((sql, row_cap))
        if self.error is not None:
            return Execution.model_validate(
                dict(
                    engine=self.report_engine or self.engine,
                    sql=sql if self.echo_sql else sql + " ",
                    result=None,
                    rows_total=None,
                    truncated=False,
                    elapsed_ms=5,
                    error=self.error,
                )
            )
        rs = self.result or ResultSet(columns=("n",), rows=((1,),))
        total = self.rows_total if self.rows_total is not None else len(rs.rows)
        shown = ResultSet(columns=rs.columns, rows=rs.rows[:row_cap])
        return Execution(
            engine=self.report_engine or self.engine,
            sql=sql if self.echo_sql else sql + " ",
            result=shown,
            rows_total=total,
            truncated=len(shown.rows) < total,
            elapsed_ms=5,
        )
