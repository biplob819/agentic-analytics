"""V5.1 MON-09 (OWNER D-100, D-119): the clarification and false-clarification intervals are
EvalKit's clustered `clarification_rates` (questions, then answers within each question); the
point rates are this project's per-question means, unchanged."""

from __future__ import annotations

import pytest
from evalkit.scorers import clarification_rates as ek_rates
from evalkit.stats import interval

from agentic_analytics.aggregate import (
    Bootstrap,
    clarification_ci_method,
    clarification_rates,
    evalkit_clarification,
    group_trials,
    two_level_interval,
)
from tests.aggregate.helpers import qid, questions, trials

N = 1000


def _by_q(records, qs):
    return group_trials(records, qs)[("claude_mcp", ())]


def _mixed():
    qs = questions(6, n_ambiguous=4)
    records = (
        trials(qid(1), "RRR")
        + trials(qid(2), "CRR")
        + trials(qid(3), "WWW")
        + trials(qid(4), "CCR")
        + trials(qid(5), "RRR")
        + trials(qid(6), "RCR")
        + trials(qid(7), "CCC", expected="clarify")
        + trials(qid(8), "CWW", expected="clarify")
        + trials(qid(9), "CCW", expected="clarify")
        + trials(qid(10), "WWW", expected="clarify")
    )
    return _by_q(records, qs), qs


def test_the_summary_interval_is_evalkits_clustered_one_and_the_point_is_unchanged():
    by_q, qs = _mixed()
    boot = Bootstrap(N, seed=3)
    clar, false = clarification_rates(by_q, qs, boot)
    exp, asked, cluster = [], [], []
    for q in sorted(by_q):
        for r in by_q[q]:
            exp.append(qs[q].expected == "clarify")
            asked.append(r.scores.clarified)
            cluster.append(q)
    ek = ek_rates(exp, asked, cluster=cluster, seed=3, n_resamples=N)
    assert ek.method == "cluster-bootstrap"
    assert (clar, false) == (ek.ask_rate, ek.false_clarification_rate)
    # the point: the mean of per-question shares, as before D-119
    assert clar.point == pytest.approx((1 + 1 / 3 + 2 / 3 + 0) / 4)
    assert false.point == pytest.approx((0 + 1 / 3 + 0 + 2 / 3 + 0 + 1 / 3) / 6)


def test_the_clustered_interval_is_the_two_level_bootstrap_within_lattice_error():
    """The same matrix of questions, then answers within each drawn question: within a
    couple of lattice steps (1 / (k x n)) of `aggregate.two_level_interval`, and at least as
    wide as the one-level bootstrap over per-question shares when trials disagree."""
    by_q, qs = _mixed()
    boot = Bootstrap(N, seed=0)
    clar, false = clarification_rates(by_q, qs, boot)
    for iv, kind in ((clar, "clarify"), (false, "answer")):
        ids = [q for q in by_q if qs[q].expected == kind]
        vals = [[1.0 if r.scores.clarified else 0.0 for r in by_q[q]] for q in ids]
        two = two_level_interval(vals, boot, f"k:{kind}")
        step = 1 / (3 * len(ids))
        assert abs(two.lo - iv.lo) <= 2 * step + 1e-9 and abs(two.hi - iv.hi) <= 2 * step + 1e-9
        one = interval([sum(v) / len(v) for v in vals], boot.matrix(len(ids)))
        assert iv.hi - iv.lo >= one.hi - one.lo - step


def test_one_trial_per_question_gives_exact_clopper_pearson():
    qs = questions(3, n_ambiguous=2)
    records = (
        trials(qid(1), "R") + trials(qid(2), "R") + trials(qid(3), "W")
        + trials(qid(4), "W", expected="clarify") + trials(qid(5), "W", expected="clarify")
    )  # fmt: skip
    by_q = _by_q(records, qs)
    clar, false = clarification_rates(by_q, qs, Bootstrap(N))
    assert clar.point == 0.0 and clar.hi == pytest.approx(1 - 0.025 ** (1 / 2))  # 0 of 2
    assert false.point == 0.0 and false.hi == pytest.approx(1 - 0.025 ** (1 / 3))  # 0 of 3
    assert clarification_ci_method(by_q, qs, N, 0) == "clopper-pearson"


def test_the_ci_method_names_evalkits_bootstrap():
    by_q, qs = _mixed()
    method = clarification_ci_method(by_q, qs, N, 5)
    assert method == evalkit_clarification(by_q, qs, Bootstrap(N, seed=5)).ci_method
    assert "cluster bootstrap" in method and f"{N} resamples, seed 5" in method


def test_the_intervals_repeat_for_a_seed():
    by_q, qs = _mixed()
    a = clarification_rates(by_q, qs, Bootstrap(N, seed=0))
    assert a == clarification_rates(by_q, qs, Bootstrap(N, seed=0))
    b = clarification_rates(by_q, qs, Bootstrap(N, seed=1))
    assert [x.point for x in a] == [x.point for x in b]  # another seed moves no point
