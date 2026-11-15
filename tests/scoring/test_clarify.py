"""SCR-03: clarified, false_clarification and the headline correct (Tech Spec §6.1)."""

from __future__ import annotations

import pytest

from agentic_analytics.schemas import Scores
from agentic_analytics.scoring import clarification
from tests.schemas.factories import answer

SQL_ANSWER = answer()
ASKED = answer(sql=None, clarification="Which year?")
CORTEX_ASKED = answer(
    backend="cortex_analyst",
    model_snapshot=None,
    steps=[],
    sql=None,
    suggestions=["Median price in Leeds in 2024"],
)


@pytest.mark.parametrize(
    ("ans", "match", "want"),
    [
        # answer expected
        (SQL_ANSWER, True, (False, False, True)),
        (SQL_ANSWER, False, (False, False, False)),
        (ASKED, False, (True, True, False)),
        (CORTEX_ASKED, False, (True, True, False)),
        # clarification expected
        (ASKED, None, (True, False, True)),
        (CORTEX_ASKED, None, (True, False, True)),
        (SQL_ANSWER, None, (False, False, False)),
    ],
)
def test_headline_table(ans, match, want):
    c = clarification(ans, match)
    assert (c.clarified, c.false_clarification, c.correct) == want
    # every row builds a valid Scores
    Scores(
        result_match=match,
        clarified=c.clarified,
        false_clarification=c.false_clarification,
        correct=c.correct,
        answer_from_rows=None,
        tool_calls=0,
        over_budget=False,
        write_attempted=False,
    )


def test_clarified_follows_the_answer_property():
    assert clarification(ASKED, None).clarified is ASKED.asked_for_clarification
    assert clarification(SQL_ANSWER, True).clarified is SQL_ANSWER.asked_for_clarification
