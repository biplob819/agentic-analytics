"""AGG-01 accuracy with a two-level bootstrap; AGG-02 accuracy by category."""

from __future__ import annotations

import math

import pytest
from evalkit.stats import Resamples, interval, interval_from

from agentic_analytics.aggregate import (
    Bootstrap,
    accuracy,
    category_accuracy,
    group_trials,
    two_level_interval,
)
from tests.aggregate.helpers import qid, questions, rec, trials
from tests.schemas import factories as f

N = 400  # resamples in tests; production uses 10,000


def _by_q(records, qs):
    return group_trials(records, qs)[("claude_mcp", ())]


def test_interval_from_is_part_of_the_evalkit_contract():
    # interval_from is imported here beside the symbols HAR-06 already pins (EK L-10).
    iv = interval_from(0.5, [0.4, 0.5, 0.6])
    assert iv.lo <= 0.5 <= iv.hi


def test_accuracy_point_is_mean_of_per_question_means():
    qs = questions(2)
    records = trials(qid(1), "RRW") + trials(qid(2), "R")
    iv = accuracy(_by_q(records, qs), Bootstrap(N, seed=1), key="k")
    assert iv.point == pytest.approx((2 / 3 + 1) / 2)
    assert 0.0 <= iv.lo <= iv.point <= iv.hi <= 1.0


def test_constant_outcomes_give_a_zero_width_interval():
    qs = questions(5)
    records = [r for i in range(1, 6) for r in trials(qid(i), "RRR")]
    iv = accuracy(_by_q(records, qs), Bootstrap(N, seed=1), key="k")
    assert (iv.point, iv.lo, iv.hi) == (1.0, 1.0, 1.0)


def test_second_level_resamples_trials_within_questions():
    """Planted defect: a question-only bootstrap sees every question at 2/3 and gives a
    zero-width interval; the two-level bootstrap must add within-question trial noise."""
    qs = questions(20)
    records = [r for i in range(1, 21) for r in trials(qid(i), "RRW")]
    by_q = _by_q(records, qs)
    boot = Bootstrap(N, seed=3)
    two = accuracy(by_q, boot, key="k")
    per_q = [math.fsum(1.0 if r.scores.correct else 0.0 for r in v) / len(v) for v in by_q.values()]
    one = interval(per_q, Resamples(len(per_q), N, seed=3))
    assert one.lo == one.hi == pytest.approx(2 / 3)  # the defect the check must catch
    assert two.lo < two.point < two.hi
    assert two.hi - two.lo > 0.05


def test_two_level_interval_matches_a_hand_rolled_reference():
    values = [[1.0, 0.0, 1.0], [0.0, 0.0, 0.0], [1.0], [1.0, 1.0, 0.0]]
    boot = Bootstrap(200, seed=5)
    a = two_level_interval(values, boot, key="x")
    b = two_level_interval(values, boot, key="x")
    assert a == b
    assert a.point == pytest.approx(math.fsum([2 / 3, 0.0, 1.0, 2 / 3]) / 4)
    c = two_level_interval(values, Bootstrap(200, seed=6), key="x")
    assert c.point == a.point and (c.lo, c.hi) != (a.lo, a.hi)


def test_two_level_interval_rejects_empty_input():
    with pytest.raises(ValueError):
        two_level_interval([], Bootstrap(200), key="x")
    with pytest.raises(ValueError):
        two_level_interval([[1.0], []], Bootstrap(200), key="x")


def test_bootstrap_rejects_too_few_resamples():
    with pytest.raises(ValueError):
        Bootstrap(10).matrix(5)


def test_bootstrap_reuses_one_matrix_per_size():
    boot = Bootstrap(200, seed=2)
    assert boot.matrix(7) is boot.matrix(7)
    assert boot.matrix(7).rows == Resamples(7, 200, seed=2).rows


def test_zero_correct_gives_zero_accuracy():
    qs = questions(3)
    records = [r for i in range(1, 4) for r in trials(qid(i), "WXE")]
    iv = accuracy(_by_q(records, qs), Bootstrap(N), key="k")
    assert (iv.point, iv.lo, iv.hi) == (0.0, 0.0, 0.0)


def test_category_accuracy_has_n_and_ordered_categories():
    qs = {
        qid(1): f.question(id=qid(1), category="time"),
        qid(2): f.question(id=qid(2), category="time"),
        qid(3): f.question(id=qid(3), category="aggregation"),
        qid(4): f.ambiguous(id=qid(4)),
    }
    records = (
        trials(qid(1), "RRR")
        + trials(qid(2), "WWW")
        + trials(qid(3), "RWR")
        + trials(qid(4), "CCW", expected="clarify")
    )
    cats = category_accuracy(_by_q(records, qs), qs, Bootstrap(N, seed=1), key="k")
    assert [c.category for c in cats] == ["aggregation", "time", "ambiguous"]
    assert [c.n for c in cats] == [1, 2, 1]
    assert cats[1].accuracy.point == pytest.approx(0.5)
    assert cats[2].accuracy.point == pytest.approx(2 / 3)
    for c in cats:
        assert c.accuracy.lo <= c.accuracy.point <= c.accuracy.hi


def test_category_with_no_questions_is_omitted():
    qs = questions(2, category="geography")
    records = trials(qid(1), "R") + trials(qid(2), "W")
    cats = category_accuracy(_by_q(records, qs), qs, Bootstrap(N), key="k")
    assert [(c.category, c.n) for c in cats] == [("geography", 2)]


def test_questions_without_records_are_not_counted():
    qs = questions(3)
    records = trials(qid(1), "RRR") + trials(qid(2), "W")  # q-003 never ran
    by_q = _by_q(records, qs)
    assert sorted(by_q) == [qid(1), qid(2)]
    assert accuracy(by_q, Bootstrap(N), key="k").point == pytest.approx(0.5)
    cats = category_accuracy(by_q, qs, Bootstrap(N), key="k")
    assert cats[0].n == 2


def test_group_trials_excludes_probes_and_rejects_bad_input():
    qs = questions(1)
    probe = rec("p-w01", "right")
    groups = group_trials([*trials(qid(1), "R"), probe], qs)
    assert list(groups[("claude_mcp", ())]) == [qid(1)]
    with pytest.raises(ValueError, match="twice"):
        group_trials(trials(qid(1), "R") + trials(qid(1), "W"), qs)
    with pytest.raises(ValueError, match="unknown question"):
        group_trials(trials(qid(9), "R"), qs)


def test_group_trials_orders_trials_and_keeps_configs_apart():
    qs = questions(1)
    records = [
        rec(qid(1), trial=3),
        rec(qid(1), trial=1),
        rec(qid(1), trial=2),
        rec(qid(1), fixes=("examples",)),
    ]
    groups = group_trials(records, qs)
    assert [r.trial for r in groups[("claude_mcp", ())][qid(1)]] == [1, 2, 3]
    examples: tuple = ("examples",)
    assert len(groups[("claude_mcp", examples)][qid(1)]) == 1


def test_question_expectation_must_match_the_record_scores():
    qs = questions(1)  # answer-expected
    with pytest.raises(ValueError, match="expected"):
        group_trials([rec(qid(1), "clarify", expected="clarify")], qs)
