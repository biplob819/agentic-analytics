"""AGG-04 clarification rates; AGG-05 pass^k and trial agreement (EK L-36)."""

from __future__ import annotations

import random

import pytest
from evalkit.stats import interval

from agentic_analytics.aggregate import (
    Bootstrap,
    accuracy,
    clarification_rates,
    group_trials,
    incomplete_questions,
    pass_all_trials,
    trial_agreement,
)
from tests.aggregate.helpers import qid, questions, rec, trials
from tests.schemas import factories as f

N = 400


def _by_q(records, qs):
    return group_trials(records, qs)[("claude_mcp", ())]


# AGG-04


def test_clarification_rate_on_ambiguous_and_false_rate_on_answer_questions():
    qs = questions(4, n_ambiguous=2)
    records = (
        trials(qid(1), "RRR")
        + trials(qid(2), "CRR")  # one false clarification in three trials
        + trials(qid(3), "WWW")
        + trials(qid(4), "CCC")  # always asks back on a clear question
        + trials(qid(5), "CCC", expected="clarify")
        + trials(qid(6), "CWW", expected="clarify")
    )
    clar, false = clarification_rates(_by_q(records, qs), qs, Bootstrap(N, seed=1))
    assert clar.point == pytest.approx((1 + 1 / 3) / 2)
    assert false.point == pytest.approx((0 + 1 / 3 + 0 + 1) / 4)
    for iv in (clar, false):
        assert 0.0 <= iv.lo <= iv.point <= iv.hi <= 1.0
    assert false.hi > false.lo


def test_clarifying_on_everything_shows_in_the_false_rate():
    qs = questions(3, n_ambiguous=3)
    records = [r for i in range(1, 4) for r in trials(qid(i), "CC")]
    records += [r for i in range(4, 7) for r in trials(qid(i), "CC", expected="clarify")]
    clar, false = clarification_rates(_by_q(records, qs), qs, Bootstrap(N))
    assert clar.point == 1.0 and false.point == 1.0


def test_rates_without_questions_of_that_kind_are_zero_width_zero():
    qs = questions(2)
    clar, false = clarification_rates(
        _by_q(trials(qid(1), "R") + trials(qid(2), "C"), qs), qs, Bootstrap(N)
    )
    assert (clar.point, clar.lo, clar.hi) == (0.0, 0.0, 0.0)
    assert false.point == 0.5


# AGG-05 pass^k


def _trial_first_pass_k(by_q, n_resamples, seed):
    """The EK L-36 bug: resample trials inside each question, then take all()."""
    rng = random.Random(seed)
    values = [[r.scores.correct for r in v] for v in by_q.values()]
    point = sum(all(v) for v in values) / len(values)
    boot = []
    for _ in range(n_resamples):
        qs = rng.choices(values, k=len(values))
        boot.append(sum(all(rng.choices(v, k=len(v))) for v in qs) / len(qs))
    return point, sorted(boot)


def test_pass_k_is_per_question_and_bootstrapped_over_questions_only():
    """Planted defect (EK L-36): every question is right on 2 of 3 trials, so pass^3 is 0 for
    every question. Resampling trials first draws all-correct trials about 30% of the time and
    lifts the interval off zero; the correct method keeps it at exactly zero."""
    qs = questions(20)
    records = [r for i in range(1, 21) for r in trials(qid(i), "RRW")]
    by_q = _by_q(records, qs)
    iv = pass_all_trials(by_q, Bootstrap(N, seed=4))
    assert (iv.point, iv.lo, iv.hi) == (0.0, 0.0, 0.0)
    _, biased = _trial_first_pass_k(by_q, N, seed=4)
    assert biased[int(0.975 * N)] > 0.2  # the defect the test is built to catch
    assert sum(biased) / N > 0.2


def test_pass_k_matches_question_bootstrap_of_all_trials_correct():
    qs = questions(10)
    patterns = ["RRR", "RRW", "WWW", "RRR", "RWR", "RRR", "WRR", "RRR", "WWR", "RRR"]
    records = [r for i, p in enumerate(patterns, 1) for r in trials(qid(i), p)]
    boot = Bootstrap(N, seed=9)
    iv = pass_all_trials(_by_q(records, qs), boot)
    want = interval([1.0 if p == "RRR" else 0.0 for p in patterns], boot.matrix(10))
    assert iv == want
    assert iv.point == pytest.approx(0.5)


def test_single_trial_pass_k_equals_accuracy():
    qs = questions(6)
    records = [r for i, p in enumerate("RRWRWR", 1) for r in trials(qid(i), p)]
    by_q = _by_q(records, qs)
    boot = Bootstrap(N, seed=2)
    assert pass_all_trials(by_q, boot).point == accuracy(by_q, boot, key="k").point
    assert trial_agreement(by_q, qs) == 1.0


# AGG-05 trial agreement


def test_identical_result_sets_agree_and_different_ones_do_not():
    qs = questions(4)
    records = [
        *trials(qid(1), "RRR"),  # identical rows
        rec(qid(2), "right", trial=1, rows=((1, 2),)),
        rec(qid(2), "wrong", trial=2, rows=((1, 3),)),
        rec(qid(3), "wrong", trial=1, rows=((5, 5),)),
        rec(qid(3), "wrong", trial=2, rows=((5, 5),)),  # both wrong, same rows: agree
        *trials(qid(4), "CC"),  # all clarified: agree
    ]
    assert trial_agreement(_by_q(records, qs), qs) == pytest.approx(3 / 4)


def test_errors_and_mixed_clarifications_break_agreement():
    qs = questions(3)
    records = trials(qid(1), "RC") + trials(qid(2), "EE") + trials(qid(3), "XX")
    assert trial_agreement(_by_q(records, qs), qs) == 0.0


def test_agreement_compares_rows_canonically():
    qs = questions(2)
    records = [
        rec(qid(1), "right", trial=1, rows=((1, "a"),)),
        rec(qid(1), "right", trial=2, rows=((1, "a"),)),
        rec(qid(2), "right", trial=1, rows=((1, 2),)),
        rec(qid(2), "right", trial=2, rows=((1.0, 2),)),  # int vs float: a different result set
    ]
    assert trial_agreement(_by_q(records, qs), qs) == 0.5


def test_row_order_is_ignored_unless_the_question_is_ordered():
    qs = {
        qid(1): f.question(id=qid(1)),
        qid(2): f.question(id=qid(2), ordered=True),
    }
    a, b = ((1, 2), (3, 4)), ((3, 4), (1, 2))
    records = [
        rec(qid(1), "right", trial=1, rows=a),
        rec(qid(1), "right", trial=2, rows=b),  # same multiset: agrees
        rec(qid(2), "right", trial=1, rows=a),
        rec(qid(2), "right", trial=2, rows=b),  # order is the answer: disagrees
    ]
    assert trial_agreement(_by_q(records, qs), qs) == 0.5


def test_duplicate_rows_count_in_the_multiset():
    qs = questions(1)
    records = [
        rec(qid(1), "right", trial=1, rows=((1, 2), (1, 2), (3, 4))),
        rec(qid(1), "right", trial=2, rows=((1, 2), (3, 4), (3, 4))),
    ]
    assert trial_agreement(_by_q(records, qs), qs) == 0.0


def test_pass_k_uses_only_questions_with_all_k_trials():
    """A question with one trial of three, right once, would count as passing all trials."""
    qs = questions(4)
    records = trials(qid(1), "RRW") + trials(qid(2), "R") + trials(qid(3), "RRR")
    records += trials(qid(4), "RR")
    by_q = _by_q(records, qs)
    assert incomplete_questions(by_q) == [qid(2), qid(4)]
    iv = pass_all_trials(by_q, Bootstrap(N, seed=1))
    # q-001 fails, q-003 passes; q-002 and q-004 (all right, but short) are left out
    assert iv.point == pytest.approx(1 / 2)
