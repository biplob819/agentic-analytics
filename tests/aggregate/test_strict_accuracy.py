"""OWNER D-81: the headline accuracy is tolerant; the strict accuracy is reported beside it."""

from __future__ import annotations

import pytest

from agentic_analytics.aggregate import (
    Bootstrap,
    accuracy,
    backend_summary,
    category_accuracy,
    correct_strict,
    equivalence_passes,
    group_trials,
)
from agentic_analytics.schemas import Record
from tests.aggregate.helpers import qid, questions, trials
from tests.aggregate.test_cost_probes import RATES

N = 400


def tolerant_only(r: Record, *rules: str) -> Record:
    """A right record that matched only under the D-81 equivalences `rules`."""
    s = r.scores.model_copy(update={"correct_strict": False, "equivalences": rules})
    return r.model_copy(update={"scores": type(s).model_validate(s.model_dump())})


def strict_scored(r: Record) -> Record:
    s = r.scores.model_copy(update={"correct_strict": r.scores.correct})
    return r.model_copy(update={"scores": type(s).model_validate(s.model_dump())})


def test_strict_accuracy_counts_tolerant_only_passes_as_wrong():
    qs = questions(2)
    one = [tolerant_only(r, "pivot") for r in trials(qid(1), "RRR")]
    two = [strict_scored(r) for r in trials(qid(2), "RRW")]
    by_q = group_trials(one + two, qs)[("claude_mcp", ())]
    boot = Bootstrap(N, seed=3)
    assert accuracy(by_q, boot, "k").point == pytest.approx((1 + 2 / 3) / 2)
    assert accuracy(by_q, boot, "k", strict=True).point == pytest.approx((0 + 2 / 3) / 2)
    (cat,) = category_accuracy(by_q, qs, boot, "k")
    assert cat.accuracy_strict is not None
    assert cat.accuracy.point > cat.accuracy_strict.point == pytest.approx(1 / 3)


def test_records_scored_before_d81_count_their_correct_as_strict():
    (r,) = trials(qid(1), "R")
    assert r.scores.correct_strict is None and correct_strict(r) is True
    qs = questions(1)
    by_q = group_trials(trials(qid(1), "RWR"), qs)[("claude_mcp", ())]
    boot = Bootstrap(N, seed=3)
    # same verdicts and the same streams: the two intervals are identical
    assert accuracy(by_q, boot, "k", strict=True) == accuracy(by_q, boot, "k")


def test_equivalence_passes_count_tolerant_only_trials_by_rule():
    recs = (
        [tolerant_only(r, "pivot") for r in trials(qid(1), "RR")]
        + [tolerant_only(r, "percent_scale", "period_number") for r in trials(qid(2), "R")]
        + trials(qid(3), "RW")
    )
    assert equivalence_passes(recs) == {"percent_scale+period_number": 1, "pivot": 2}


def test_backend_summary_carries_both_accuracies():
    qs = questions(2)
    recs = [tolerant_only(r, "empty_check") for r in trials(qid(1), "R")] + scored(
        trials(qid(2), "R")
    )
    by_q = group_trials(recs, qs)[("claude_mcp", ())]
    b = backend_summary(("claude_mcp", ()), by_q, qs, Bootstrap(N, seed=0), RATES)
    assert b.accuracy.point == 1.0
    assert b.accuracy_strict is not None and b.accuracy_strict.point == 0.5
    assert b.equivalence_passes == {"empty_check": 1}
    assert all(c.accuracy_strict is not None for c in b.by_category)


def scored(recs):
    """The records as D-81 scores them when no rule fires: strict equals the headline."""
    return [
        r.model_copy(
            update={"scores": r.scores.model_copy(update={"correct_strict": r.scores.correct})}
        )
        for r in recs
    ]


def test_a_summary_of_records_scored_before_d81_shows_no_strict_pair():
    """Without D-81 verdicts the tolerant headline would be the strict score relabelled."""
    from agentic_analytics.aggregate import scored_under_d81

    old = trials(qid(1), "RWR")
    new = scored(old)
    assert not scored_under_d81(old) and scored_under_d81(new)
    qs = questions(1)
    for recs, shown in ((old, False), (new, True)):
        by_q = group_trials(recs, qs)[("claude_mcp", ())]
        b = backend_summary(("claude_mcp", ()), by_q, qs, Bootstrap(N, seed=0), RATES)
        assert (b.accuracy_strict is not None) is shown
        assert all((c.accuracy_strict is not None) is shown for c in b.by_category)
