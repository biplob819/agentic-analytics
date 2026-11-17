"""The gold check (Q2.2, Tech Spec §5): run, compare, tie-check, select and stamp the gold.

The check writes R3.1's gold check format (`run/gold.py`: `gold/gold_check.json`, plus a
`gold/check-<ts>.json` history copy) and the `gold/<engine>/<qid>.json` results; `dual` holds the
dual-authorship helper (QST-04).
"""

from agentic_analytics.gold.check import (
    GOLD_ROW_CAP,
    SELECTION_SEED,
    GoldCheckOutcome,
    GoldConfigError,
    ProbeOutcome,
    QuestionOutcome,
    check_probe,
    check_question,
    configured_executors,
    cross_engine_mismatch,
    identical,
    ranking_index,
    rewrite_limit,
    run_gold_check,
    select_questions,
    separated,
)
from agentic_analytics.gold.dual import (
    DualAuthorError,
    DualDraft,
    DualResult,
    compare_dual,
    dual_authorship,
    gpt_sol_sql,
    make_gpt_sol,
    table_ddl,
)

__all__ = [
    "GOLD_ROW_CAP",
    "SELECTION_SEED",
    "DualAuthorError",
    "DualDraft",
    "DualResult",
    "GoldCheckOutcome",
    "GoldConfigError",
    "ProbeOutcome",
    "QuestionOutcome",
    "check_probe",
    "check_question",
    "compare_dual",
    "configured_executors",
    "cross_engine_mismatch",
    "dual_authorship",
    "gpt_sol_sql",
    "identical",
    "make_gpt_sol",
    "ranking_index",
    "rewrite_limit",
    "run_gold_check",
    "select_questions",
    "separated",
    "table_ddl",
]
