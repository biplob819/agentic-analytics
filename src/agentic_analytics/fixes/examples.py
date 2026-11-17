"""The `examples` fix (FIX-02, Tech Spec §7): verified question-and-SQL pairs in the prompt.

A pack keeps its examples in `fixes/examples.yaml`::

    schema_version: "1.0"
    engine: clickhouse            # the MCP agents write ClickHouse SQL
    examples:
      - id: ex-1
        question: ...
        sql: SELECT ...

`load_examples(pack)` validates the file; `examples_block(examples)` is the exact text the fix
adds to the MCP agents' system prompt. Examples are written in the style of the benchmark but
must not overlap it (FIX-03, `fixes.leak`); `lint_example(example, ctx)` applies Q2.1's
single-engine writing rules (exact functions, uppercase literals, the guard, known tables and
columns) so an example never teaches the agent an approximate function.
"""

from __future__ import annotations

from typing import Annotated, Literal

import yaml
from pydantic import Field, StringConstraints, ValidationError, model_validator

from agentic_analytics.packs import Pack, PackError, read_pack_text
from agentic_analytics.questions.lint import LintContext, LintIssue, _lint_sql
from agentic_analytics.schemas._base import Model, NonBlank, Versioned
from agentic_analytics.schemas.config import _UniqueKeyLoader

EXAMPLES_FILE = "fixes/examples.yaml"
BLOCK_TITLE = "VERIFIED EXAMPLES"


class FixError(ValueError):
    """A fix file is missing or malformed, or the frozen record does not hold (exit code 2)."""


class Example(Model):
    id: Annotated[str, StringConstraints(pattern=r"^ex-[0-9]+$")]
    question: NonBlank
    sql: NonBlank


class ExamplesFile(Versioned):
    engine: Literal["clickhouse"]
    examples: Annotated[tuple[Example, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> ExamplesFile:
        ids = [e.id for e in self.examples]
        if len(set(ids)) != len(ids):
            raise ValueError("example ids must be unique")
        return self


def load_yaml(text: str, where: str) -> object:
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise FixError(f"{where}: invalid YAML: {exc}") from exc


def parse_examples(text: str, where: str = EXAMPLES_FILE) -> tuple[Example, ...]:
    data = load_yaml(text, where)
    try:
        return ExamplesFile.model_validate(data).examples
    except ValidationError as exc:
        raise FixError(f"{where}: {exc}") from exc


def load_examples(pack: Pack) -> tuple[Example, ...]:
    """The pack's `fixes/examples.yaml`, validated. FixError if missing or malformed."""
    try:
        text = read_pack_text(pack, EXAMPLES_FILE)
    except PackError as exc:
        raise FixError(str(exc)) from exc
    return parse_examples(text, f"{pack.ref} {EXAMPLES_FILE}")


BLOCK_INTRO = (
    "{n} verified question-and-SQL pairs for this ClickHouse table. They show the conventions "
    "(uppercase place names, the stored enum values, exact functions, date filtering, the "
    "new-build flag and tenure); they are not the questions you will be asked."
)


def examples_block(examples: tuple[Example, ...] | list[Example]) -> str:
    """The `examples` fix text: the title, `BLOCK_INTRO`, then each pair in file order."""
    lines = [BLOCK_TITLE, BLOCK_INTRO.format(n=len(examples))]
    for n, ex in enumerate(examples, 1):
        lines += ["", f"Example {n}", f"Question: {ex.question}", "SQL:", ex.sql.strip()]
    return "\n".join(lines) + "\n"


def lint_example(example: Example, ctx: LintContext) -> list[LintIssue]:
    """Q2.1's single-engine writing rules over one example's ClickHouse SQL."""
    issues: list[LintIssue] = []

    def issue(rule: str, message: str) -> None:
        issues.append(LintIssue(example.id, rule, message, "clickhouse", "sql"))

    _lint_sql(
        example.sql,
        "clickhouse",
        ctx,
        allowed_tables=ctx.tables.get("clickhouse", frozenset()),
        issue=issue,
    )
    return issues


__all__ = [
    "BLOCK_INTRO",
    "BLOCK_TITLE",
    "EXAMPLES_FILE",
    "Example",
    "ExamplesFile",
    "FixError",
    "examples_block",
    "lint_example",
    "load_examples",
    "parse_examples",
]
