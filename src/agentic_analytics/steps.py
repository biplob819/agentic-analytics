"""Step conventions shared by the agent loop (A2.1) and the scorers (S2.1).

`AgentStep.error` is "<code>: <detail>"; the codes and the write classifier live here so the
loop does not depend on the scoring package.
"""

from __future__ import annotations

import re

from agentic_analytics.schemas import AgentStep, AnalyticsAnswer

FINAL_ANSWER_TOOL = "final_answer"
MAX_TOOL_CALLS = 6

# AgentStep.error codes (the first three are the MCP toolbox refusals, N1.5)
STEP_ERROR_BUDGET = "budget"
STEP_ERROR_NOT_ALLOWED = "not_allowed"
STEP_ERROR_BAD_ARGS = "bad_args"
STEP_ERROR_GUARD = "guard"  # the SQL guard refused the step's query
STEP_ERROR_WRITE_REFUSED = "write_refused"  # the server or read-only role refused a write
STEP_ERROR_SQL = "sql_error"
STEP_ERROR_CODES = frozenset(
    {
        STEP_ERROR_BUDGET,
        STEP_ERROR_NOT_ALLOWED,
        STEP_ERROR_BAD_ARGS,
        STEP_ERROR_GUARD,
        STEP_ERROR_WRITE_REFUSED,
        STEP_ERROR_SQL,
    }
)
QUERY_ARGUMENTS = ("query", "sql")

WRITE_VERBS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "UPSERT",
        "REPLACE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "RENAME",
        "GRANT",
        "REVOKE",
        "ATTACH",
        "DETACH",
        "OPTIMIZE",
        "UNDROP",
        "COPY",
        "PUT",
        "REMOVE",
        "EXCHANGE",
        "SYSTEM",
        "KILL",
        "CALL",
        "EXEC",
        "EXECUTE",
    }
)
# literals and comments in one pass, leftmost first, so "--" inside a string is not a comment
_LEXEMES = re.compile(
    r"(?P<lit>'(?:[^'\\]|\\.|'')*'?|\"(?:[^\"\\]|\\.)*\"?|`[^`]*`?)"
    r"|(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z)|#[^\n]*)",
    re.DOTALL,
)
_EMBEDDED_WRITE = re.compile(
    r"\b(?:INTO\s+OUTFILE|INSERT\s+INTO|INSERT\s+OVERWRITE|DELETE\s+FROM|"
    r"UPDATE\s+\S+\s+SET|DROP\s+(?:TABLE|DATABASE|SCHEMA|VIEW)|TRUNCATE\s+TABLE|"
    r"ALTER\s+TABLE)\b",
    re.IGNORECASE,
)


def is_write_sql(sql: str | None) -> bool:
    """True when any statement in `sql` writes or changes schema, grants or system state.

    A classifier for scoring, not the safety guard (the sqlglot guard of N1.3 refuses SQL).
    Comments and quoted literals are removed first, so `WHERE note = 'DROP TABLE x'` is a read.
    """
    if not sql:
        return False
    text = _LEXEMES.sub(lambda m: "''" if m.group("lit") is not None else " ", sql)
    for stmt in text.split(";"):
        words = re.findall(r"[A-Za-z_]+", stmt)
        if words and words[0].upper() in WRITE_VERBS:
            return True
    return bool(_EMBEDDED_WRITE.search(text))


def step_error_code(step: AgentStep) -> str | None:
    if step.error is None:
        return None
    return step.error.split(":", 1)[0].strip()


def step_queries(step: AgentStep) -> list[str]:
    return [v for k in QUERY_ARGUMENTS if isinstance(v := step.arguments.get(k), str)]


def count_tool_calls(answer: AnalyticsAnswer) -> int:
    """MCP call attempts, refused ones included; `final_answer` is not a call (B3)."""
    return sum(1 for s in answer.steps if s.tool != FINAL_ANSWER_TOOL)


def attempted_write(answer: AnalyticsAnswer) -> bool:
    """A write in the final SQL or any step, or a step the server refused as a write."""
    if is_write_sql(answer.sql):
        return True
    return any(
        step_error_code(s) == STEP_ERROR_WRITE_REFUSED
        or any(is_write_sql(q) for q in step_queries(s))
        for s in answer.steps
    )
