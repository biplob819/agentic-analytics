"""System prompt, as-of line, final_answer spec and prompt hash (AGT-01, AGT-02, AGT-03)."""

import dataclasses
import re
from datetime import date

import pytest

from agentic_analytics.backends import prompts
from agentic_analytics.backends.prompts import (
    BASE_PROMPT,
    FINAL_ANSWER_SPEC,
    PROMPT_VERSION,
    as_of_line,
    prompt_hash,
    render_system_prompt,
)
from agentic_analytics.models import ToolSpec

AS_OF = date(2026, 9, 30)
TOOLS = (
    ToolSpec("list_tables", "List tables.", {"type": "object", "properties": {}}),
    ToolSpec("run_select_query", "Run one SELECT.", {"type": "object", "properties": {}}),
)

# each behavioural rule and the words that must state it (PRD B1-B9)
RULE_PHRASES = {
    "B1": ["only from the rows"],
    "B2": ["ambiguous", "clarification"],
    "B3": ["read-only", "at most 6 tool calls"],
    "B4": ["empty or implausible"],
    "B5": ["always give the sql"],
    "B6": ["suggestions", "can answer"],
    "B7": ["never state a number"],
    "B8": ["never write"],
    "B9": ["data, never instructions", "change these rules"],
}


@pytest.mark.parametrize("rule", sorted(RULE_PHRASES))
def test_prompt_states_every_behavioural_rule(rule):
    text = " ".join(BASE_PROMPT.casefold().split())
    for phrase in RULE_PHRASES[rule]:
        assert phrase in text, f"{rule}: {phrase!r} missing"


def test_prompt_is_versioned_and_concise():
    assert re.fullmatch(r"agent-v\d+", PROMPT_VERSION)
    assert len(BASE_PROMPT) < 4000  # a few hundred words, not an essay
    assert "final_answer" in BASE_PROMPT


def test_as_of_line_pins_last_year_to_the_calendar_year_before():
    assert as_of_line(AS_OF) == (
        "Data current to 2026-09-30. Treat that date as today: 'last year' means the last full "
        "calendar year before that date (2025), 'this year' means 2026 up to 2026-09-30, and "
        "'the last 12 months' means the 12 months ending 2026-09-30."
    )
    assert "(2026)" in as_of_line(date(2027, 1, 31))
    assert "(2025)" in as_of_line(date(2026, 12, 31))  # a full 2026 is still 'this year'


def test_render_puts_base_then_as_of_then_fix_blocks_in_order():
    text = render_system_prompt(AS_OF, ("FIX ONE", "FIX TWO"))
    assert text.startswith(BASE_PROMPT)
    i_as_of, i_one, i_two = (text.index(s) for s in (as_of_line(AS_OF), "FIX ONE", "FIX TWO"))
    assert i_as_of < i_one < i_two
    assert render_system_prompt(AS_OF).endswith(as_of_line(AS_OF))


def test_blank_fix_block_is_refused():
    with pytest.raises(ValueError, match="fix block"):
        render_system_prompt(AS_OF, ("ok", "  "))


def test_final_answer_spec_is_strict_and_closed():
    spec = FINAL_ANSWER_SPEC
    assert spec.name == "final_answer" and spec.strict is True
    params = spec.parameters
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"sql", "answer", "clarification", "suggestions"}
    assert sorted(params["required"]) == sorted(params["properties"])  # strict mode needs all
    for f in ("sql", "answer", "clarification"):
        assert params["properties"][f]["type"] == ["string", "null"]
    assert params["properties"]["suggestions"] == {
        **params["properties"]["suggestions"],
        "type": "array",
        "items": {"type": "string"},
    }
    assert "six" in spec.description or "6" in spec.description


def test_prompt_hash_is_stable_sha256():
    h = prompt_hash(AS_OF, TOOLS)
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert prompt_hash(AS_OF, TOOLS) == h


def test_prompt_hash_changes_with_each_input(monkeypatch):
    h = prompt_hash(AS_OF, TOOLS)
    assert prompt_hash(date(2026, 8, 31), TOOLS) != h
    changed = (dataclasses.replace(TOOLS[0], description="List tables!"), TOOLS[1])
    assert prompt_hash(AS_OF, changed) != h
    assert prompt_hash(AS_OF, TOOLS[::-1]) != h
    strict = (dataclasses.replace(TOOLS[0], strict=True), TOOLS[1])
    assert prompt_hash(AS_OF, strict) != h
    monkeypatch.setattr(prompts, "PROMPT_VERSION", "agent-v999")
    assert prompt_hash(AS_OF, TOOLS) != h


def test_prompt_hash_covers_base_prompt_and_final_answer(monkeypatch):
    h = prompt_hash(AS_OF, TOOLS)
    monkeypatch.setattr(prompts, "BASE_PROMPT", BASE_PROMPT + " extra")
    h2 = prompt_hash(AS_OF, TOOLS)
    assert h2 != h
    monkeypatch.setattr(
        prompts, "FINAL_ANSWER_SPEC", dataclasses.replace(FINAL_ANSWER_SPEC, description="x")
    )
    assert prompt_hash(AS_OF, TOOLS) != h2


def test_prompt_hash_ignores_fix_blocks_which_have_their_own_hash():
    # the hash takes no fix blocks: the same base prompt hashes the same under any fix
    assert "fix_blocks" not in prompt_hash.__code__.co_varnames


@pytest.mark.parametrize("name", ["REMINDER", "ACCEPTED", "REJECTED", "DUPLICATE", "GUARD_REFUSAL"])
def test_prompt_hash_covers_the_loops_model_facing_texts(monkeypatch, name):
    h = prompt_hash(AS_OF, TOOLS)
    monkeypatch.setattr(prompts, name, getattr(prompts, name) + " changed")
    assert prompt_hash(AS_OF, TOOLS) != h


def test_loop_uses_the_hashed_texts():
    from agentic_analytics.backends import mcp_agent

    for name in ("REMINDER", "ACCEPTED", "REJECTED", "DUPLICATE", "GUARD_REFUSAL"):
        assert getattr(mcp_agent, name) is getattr(prompts, name)
