"""Fixes as flags (F3.1, Tech Spec §7): descriptions, examples and self-check.

- FIX-01 `descriptions`: N1.4's descriptions block for the pack's semantic model.
- FIX-02 `examples`: `fixes/examples.yaml`, five verified question-and-SQL pairs.
- FIX-03 leak check: `text_leaks` (word 3-grams), `structural_leaks` (SQL skeletons) and
  `result_leaks` (live).
- FIX-04 `self-check`: `fixes/self_check.yaml` rules and `make_self_check(executor, rules)`.
- FIX-05 freeze: `fix_hash`, `freeze_fixes` and `fix_hash_matches_frozen` (gate 8).
"""

from agentic_analytics.fixes.examples import (
    EXAMPLES_FILE,
    Example,
    FixError,
    examples_block,
    lint_example,
    load_examples,
    parse_examples,
)
from agentic_analytics.fixes.freeze import (
    FIX_FILES,
    FROZEN_FILE,
    FrozenRecord,
    current_fix_hash,
    descriptions_text,
    fix_blocks,
    fix_hash,
    fix_hash_matches_frozen,
    freeze_fixes,
    frozen_fix_hash,
    frozen_problems,
    load_frozen,
)
from agentic_analytics.fixes.leak import (
    MAX_OVERLAP,
    Leak,
    overlap,
    result_leaks,
    result_leaks_live,
    skeleton,
    structural_leaks,
    text_leaks,
)
from agentic_analytics.fixes.self_check import (
    RULE_IDS,
    SELF_CHECK_FILE,
    SelfCheckRules,
    TableStats,
    judge,
    load_rules,
    make_self_check,
    parse_rules,
)

__all__ = [
    "EXAMPLES_FILE",
    "FIX_FILES",
    "FROZEN_FILE",
    "MAX_OVERLAP",
    "RULE_IDS",
    "SELF_CHECK_FILE",
    "Example",
    "FixError",
    "FrozenRecord",
    "Leak",
    "SelfCheckRules",
    "TableStats",
    "current_fix_hash",
    "descriptions_text",
    "examples_block",
    "fix_blocks",
    "fix_hash",
    "fix_hash_matches_frozen",
    "freeze_fixes",
    "frozen_fix_hash",
    "frozen_problems",
    "judge",
    "lint_example",
    "load_examples",
    "load_frozen",
    "load_rules",
    "make_self_check",
    "overlap",
    "parse_examples",
    "parse_rules",
    "result_leaks",
    "result_leaks_live",
    "skeleton",
    "structural_leaks",
    "text_leaks",
]
