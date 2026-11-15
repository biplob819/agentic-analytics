"""Clarification and the headline `correct` (SCR-03, Tech Spec §6.1, C4, US3).

| Question expects | `correct` is true when |
| An answer        | the backend did not ask back and the result matched |
| A clarification  | the backend asked back |

`false_clarification` is asking back on an answer-expected question, so asking back on
everything cannot look good. `result_match is None` marks a question where no answer is
expected (clarify-expected, or a probe without gold).
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_analytics.schemas import AnalyticsAnswer


@dataclass(frozen=True)
class Clarification:
    clarified: bool
    false_clarification: bool
    correct: bool


def clarification(answer: AnalyticsAnswer, result_match: bool | None) -> Clarification:
    clarified = answer.asked_for_clarification
    if result_match is None:
        return Clarification(clarified=clarified, false_clarification=False, correct=clarified)
    return Clarification(
        clarified=clarified,
        false_clarification=clarified,
        correct=result_match and not clarified,
    )
