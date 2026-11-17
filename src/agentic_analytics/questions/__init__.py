"""Question bank, probes, writing-rule lint and `pack check` (Q2.1: QST-01..03, QST-06..08).

`load_questions(pack)` / `load_probes(pack)` read a pack's JSONL files into schemas;
`lint_questions` and `lint_probes` apply the gold-SQL writing rules offline; `check_pack(pack)`
is the core of `agentic-analytics pack check`.
"""

from agentic_analytics.questions.bank import (
    AS_OF_PLACEHOLDER,
    PROBES_FILE,
    QUESTIONS_FILE,
    BankError,
    bind_as_of,
    load_probes,
    load_questions,
    parse_probes,
    parse_questions,
)
from agentic_analytics.questions.lint import (
    RULES,
    LintContext,
    LintIssue,
    PackReport,
    check_pack,
    check_quotas,
    context_for,
    lint_probe,
    lint_probes,
    lint_question,
    lint_questions,
)

__all__ = [
    "AS_OF_PLACEHOLDER",
    "PROBES_FILE",
    "QUESTIONS_FILE",
    "RULES",
    "BankError",
    "LintContext",
    "LintIssue",
    "PackReport",
    "bind_as_of",
    "check_pack",
    "check_quotas",
    "context_for",
    "lint_probe",
    "lint_probes",
    "lint_question",
    "lint_questions",
    "load_probes",
    "load_questions",
    "parse_probes",
    "parse_questions",
]
