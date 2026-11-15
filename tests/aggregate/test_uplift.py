"""AGG-03 fix uplift: paired bootstrap over questions against the same backend's baseline."""

from __future__ import annotations

import pytest
from evalkit.stats import holm, paired

from agentic_analytics.aggregate import Bootstrap, fix_uplifts, group_trials
from tests.aggregate.helpers import qid, questions, trials

N = 400


def _config(n: int, pattern: str, **kw):
    """Records for q-001..q-n where question i takes pattern[i % len(pattern)] as its trials."""
    parts = pattern.split()
    return [r for i in range(1, n + 1) for r in trials(qid(i), parts[(i - 1) % len(parts)], **kw)]


def test_clear_uplift_is_separated_and_in_points():
    qs = questions(20)
    records = _config(20, "WWW") + _config(20, "RRR", fixes=("descriptions",))
    (u,) = fix_uplifts(group_trials(records, qs), Bootstrap(N, seed=1))
    assert (u.backend, u.fix) == ("claude_mcp", "descriptions")
    assert u.delta_points.point == pytest.approx(100.0)
    assert u.p_value < 0.05
    assert u.separated


def test_no_change_is_not_separated():
    qs = questions(20)
    records = _config(20, "RWR WRR") + _config(20, "RWR WRR", fixes=("examples",))
    (u,) = fix_uplifts(group_trials(records, qs), Bootstrap(N, seed=1))
    assert u.delta_points.point == 0.0
    assert u.p_value == 1.0
    assert not u.separated


def test_uplift_uses_per_question_accuracy_paired_over_shared_questions():
    """The unshared questions sit first (fix only) and in the middle (baseline only), so
    pairing by position instead of by question id gives a different answer."""
    qs = questions(5)
    sc = ("self_check",)
    base = trials(qid(2), "RWW") + trials(qid(3), "WWW") + trials(qid(4), "RRR")
    base += trials(qid(5), "WWW")
    fix = trials(qid(1), "RRR", fixes=sc) + trials(qid(2), "RRW", fixes=sc)
    fix += trials(qid(4), "RRR", fixes=sc) + trials(qid(5), "RWW", fixes=sc)
    (u,) = fix_uplifts(group_trials(base + fix, qs), Bootstrap(N, seed=1))
    # shared q-002, q-004, q-005: (2/3 - 1/3), (1 - 1), (1/3 - 0), mean ×100
    assert u.delta_points.point == pytest.approx(100 * (2 / 9))


def test_holm_runs_across_the_fixes_of_one_backend():
    """A fix whose raw p-value is below 0.05 but above Holm's threshold for its rank is not
    separated; `separated` agrees with evalkit's holm over that backend's family."""
    qs = questions(40)
    base = _config(40, "WWW")
    strong = _config(40, "RRR", fixes=("descriptions",))
    # weak: 4 of 40 questions improve, so the bootstrap p is small but not tiny
    weak = [
        r
        for i in range(1, 41)
        for r in trials(qid(i), "RRR" if i <= 4 else "WWW", fixes=("examples",))
    ]
    flat = _config(40, "WWW", fixes=("self_check",))
    boot = Bootstrap(N, seed=11)
    ups = {u.fix: u for u in fix_uplifts(group_trials(base + strong + weak + flat, qs), boot)}
    assert list(ups) == ["descriptions", "examples", "self_check"]
    pvals = {k: u.p_value for k, u in ups.items()}
    rejected = holm(pvals)
    for k, u in ups.items():
        excludes_zero = not (u.delta_points.lo <= 0.0 <= u.delta_points.hi)
        assert u.separated == (rejected[k] and excludes_zero)
    # The weak fix alone: same p-value, recomputed with paired() on the same resamples.
    a = [100.0 if i <= 4 else 0.0 for i in range(1, 41)]
    _, p_weak = paired(a, [0.0] * 40, boot.matrix(40))
    assert p_weak == ups["examples"].p_value
    assert ups["descriptions"].separated and not ups["self_check"].separated


def test_holm_family_can_reject_a_fix_that_would_pass_alone():
    """Planted defect: an unadjusted test (p <= 0.05 and an interval clear of 0) would call
    this fix separated; Holm across the backend's three fixes must not."""
    qs = questions(40)
    base = _config(40, "WWW")
    boot = Bootstrap(N, seed=11)
    weak = [
        r
        for i in range(1, 41)
        for r in trials(qid(i), "RRR" if i <= 4 else "WWW", fixes=("examples",))
    ]
    noise = [_config(40, "WWW", fixes=(fx,)) for fx in ("descriptions", "self_check")]
    ups = {u.fix: u for u in fix_uplifts(group_trials(base + weak + noise[0] + noise[1], qs), boot)}
    u = ups["examples"]
    assert 0.05 / 3 < u.p_value <= 0.05, "premise: passes unadjusted, fails Holm's first step"
    assert u.delta_points.lo > 0.0, "premise: the interval alone excludes zero"
    assert not u.separated
    (alone,) = fix_uplifts(group_trials(base + weak, qs), boot)
    assert alone.p_value == u.p_value and alone.separated  # a family of one: Holm == raw


def test_all_run_is_in_the_holm_family():
    """D-34: the `all` run joins the backend's Holm family. Here `examples` (p about 0.025)
    passes Holm over the three single fixes but not over four with a flat `all` run."""
    qs = questions(40)
    base = _config(40, "WWW")
    strong = _config(40, "RRR", fixes=("descriptions",))
    weak = [
        r
        for i in range(1, 41)
        for r in trials(qid(i), "RRR" if i <= 4 else "WWW", fixes=("examples",))
    ]
    flat = _config(40, "WWW", fixes=("self_check",))
    flat_all = _config(40, "WWW", fixes=("descriptions", "examples", "self_check"))
    boot = Bootstrap(N, seed=11)
    ups = {
        u.fix: u
        for u in fix_uplifts(group_trials(base + strong + weak + flat + flat_all, qs), boot)
    }
    assert list(ups) == ["descriptions", "examples", "self_check", "all"]
    singles = {k: u.p_value for k, u in ups.items() if k != "all"}
    assert holm(singles)["examples"], "premise: Holm over the three single fixes rejects"
    assert ups["examples"].delta_points.lo > 0.0
    assert not ups["examples"].separated
    without_all = {
        u.fix: u for u in fix_uplifts(group_trials(base + strong + weak + flat, qs), boot)
    }
    assert without_all["examples"].separated


def test_too_few_shared_questions_are_never_separated():
    qs = questions(1)
    records = trials(qid(1), "WWW") + trials(qid(1), "RWW", fixes=("descriptions",))
    (u,) = fix_uplifts(group_trials(records, qs), Bootstrap(N, seed=1))
    assert u.delta_points.point == pytest.approx(100 / 3)
    assert not u.separated


def test_minimum_shared_questions_is_configurable():
    qs = questions(10)
    records = _config(10, "WWW") + _config(10, "RRR", fixes=("descriptions",))
    groups = group_trials(records, qs)
    (default,) = fix_uplifts(groups, Bootstrap(N, seed=1))
    assert default.p_value < 0.05 and default.delta_points.lo > 0.0
    assert not default.separated  # 10 shared questions, below the default of 20
    (lowered,) = fix_uplifts(groups, Bootstrap(N, seed=1), min_questions=10)
    assert lowered.separated
    (raised,) = fix_uplifts(groups, Bootstrap(N, seed=1), min_questions=11)
    assert not raised.separated
    with pytest.raises(ValueError):
        fix_uplifts(groups, Bootstrap(N), min_questions=0)


def test_default_minimum_is_twenty():
    qs = questions(20)
    records = _config(20, "WWW") + _config(20, "RRR", fixes=("descriptions",))
    (u,) = fix_uplifts(group_trials(records, qs), Bootstrap(N, seed=1))
    assert u.separated
    qs19 = questions(19)
    records = _config(19, "WWW") + _config(19, "RRR", fixes=("descriptions",))
    (u,) = fix_uplifts(group_trials(records, qs19), Bootstrap(N, seed=1))
    assert not u.separated


def test_a_harmful_fix_can_be_separated_with_a_negative_delta():
    qs = questions(20)
    records = _config(20, "RRR") + _config(20, "WWW", fixes=("examples",))
    (u,) = fix_uplifts(group_trials(records, qs), Bootstrap(N, seed=1))
    assert u.delta_points.point == pytest.approx(-100.0)
    assert u.separated


def test_all_fixes_run_is_labelled_all_and_pairs_skip_two_fix_combos():
    qs = questions(10)
    records = (
        _config(10, "WWW")
        + _config(10, "RRR", fixes=("descriptions", "examples", "self_check"))
        + _config(10, "RRR", fixes=("descriptions", "examples"))
    )
    ups = fix_uplifts(group_trials(records, qs), Bootstrap(N))
    assert [u.fix for u in ups] == ["all"]


def test_no_baseline_means_no_uplift_and_cortex_never_has_one():
    qs = questions(10)
    records = _config(10, "RRR", fixes=("descriptions",)) + _config(
        10, "RRR", backend="cortex_analyst"
    )
    assert fix_uplifts(group_trials(records, qs), Bootstrap(N)) == ()


def test_uplifts_ordered_by_backend_then_fix():
    qs = questions(6)
    records = []
    for backend in ("gpt_mcp", "claude_mcp"):
        records += _config(6, "WRW", backend=backend)
        for fx in (("self_check",), ("descriptions",)):
            records += _config(6, "RRW", backend=backend, fixes=fx)
    ups = fix_uplifts(group_trials(records, qs), Bootstrap(N))
    assert [(u.backend, u.fix) for u in ups] == [
        ("claude_mcp", "descriptions"),
        ("claude_mcp", "self_check"),
        ("gpt_mcp", "descriptions"),
        ("gpt_mcp", "self_check"),
    ]


def test_small_uplifts_stay_in_the_holm_family():
    """A fix measured on too few shared questions is never separated, but it still counts in
    the backend's Holm family: here it turns a family of one into two, and the borderline
    `examples` fix fails Holm only because of it."""
    qs = questions(45)
    base = _config(45, "WWW")
    boot = Bootstrap(N, seed=11)
    weak = [
        r
        for i in range(1, 46)
        for r in trials(qid(i), "RRR" if i <= 4 else "WWW", fixes=("examples",))
    ]
    small = [r for i in range(1, 11) for r in trials(qid(i), "WWW", fixes=("self_check",))]
    (alone,) = fix_uplifts(group_trials(base + weak, qs), boot)
    assert 0.05 / 2 < alone.p_value <= 0.05 and alone.separated, "premise"
    ups = {u.fix: u for u in fix_uplifts(group_trials(base + weak + small, qs), boot)}
    assert not ups["self_check"].separated  # 10 shared questions < the minimum of 20
    assert not ups["examples"].separated  # the family has two members, so 0.05 / 2 applies
