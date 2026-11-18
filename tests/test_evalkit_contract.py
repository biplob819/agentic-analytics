"""HAR-06: every EvalKit symbol this project imports, listed here independently (EK L-10)."""

import importlib
import inspect
import re
import tomllib

import pytest

from harness_lib import ROOT

USED = {
    "evalkit": ["__version__"],
    "evalkit.scorers": [
        "sql_match",
        "attack_outcome",
        "ScoreResult",
        "unsupported_numbers",  # MON-06: the answer-from-rows check
        "clarification_rates",  # MON-06, MON-09: the clustered clarification intervals
        "ClarificationRates",  # MON-09: `ci_method` names the interval in metrics-1
    ],
    # MON-07: the run ledger, history, bands and trend page (EvalKit B5.7, v0.4.0)
    "evalkit.ledger": [
        "RUNS_PATH",
        "LedgerError",
        "RunCheck",
        "History",
        "record_run",
        "check_run",
        "history",
        "history_html",
        "verify_ledger",
    ],
    # MON-05, MON-01, MON-02: metrics-1 export, `evalkit gate`, `evalkit compare`
    "evalkit.metrics": [
        "SCHEMA",
        "EFFICIENCY_NAMES",
        "Metric",
        "MetricsSummary",
        "Provenance",
        "load_metrics",
    ],
    "evalkit.gates": ["run_gate", "load_gates", "EXIT_PASS", "EXIT_FAIL", "EXIT_INPUT"],
    "evalkit.compare": ["compare_runs", "report_json", "EXIT_PASS", "EXIT_REGRESSION"],
    "evalkit.schemas": ["ResultSet", "Interval", "AttackBlocked", "Step", "Trajectory"],
    "evalkit.agents.score": ["MIN_STEPS"],  # tests only: MON-04's minimum follows it
    "evalkit.stats": ["Resamples", "interval", "interval_from", "paired", "holm", "percentile_ms"],
    "evalkit.demo.guard": ["Guard", "GuardRefused", "PostgresSpend", "SqliteSpend", "Ticket"],
    "evalkit.cost": ["Price", "call_cost", "load_prices", "price_for", "UnknownPrice"],
    "evalkit.retry": ["with_retries", "is_retryable"],
}


@pytest.mark.contract
@pytest.mark.parametrize("module", sorted(USED))
def test_every_used_evalkit_symbol_imports(module):
    mod = importlib.import_module(module)
    missing = [name for name in USED[module] if not hasattr(mod, name)]
    assert not missing, f"{module} lost {missing}"


@pytest.mark.contract
def test_sql_match_keyword_contract():
    from evalkit.scorers import sql_match

    params = inspect.signature(sql_match).parameters
    for name in ("rel_tol", "abs_tol", "allow_extra_columns"):
        assert name in params and params[name].kind is inspect.Parameter.KEYWORD_ONLY


def test_sql_match_extra_columns_and_tolerance():
    from evalkit.schemas import ResultSet
    from evalkit.scorers import sql_match

    gold = ResultSet(columns=("town", "median_price"), rows=(("LEEDS", 250000),))
    extra = ResultSet(columns=("town", "median_price", "sales"), rows=(("LEEDS", 250000, 812),))
    assert sql_match(extra, gold).value == 0.0
    assert sql_match(extra, gold, allow_extra_columns=True).value == 1.0
    near = ResultSet(columns=("town", "median_price"), rows=(("LEEDS", 251000.0),))
    assert sql_match(near, gold).value == 0.0
    assert sql_match(near, gold, rel_tol=0.005).value == 1.0


@pytest.mark.contract
def test_attack_outcome_contract():
    """MON-03 relies on these: positional (gold, trajectory, allowed_tools), the three outcomes
    and their evidence keys, and a refused disallowed call scored partial."""
    from evalkit.schemas import AttackBlocked, Step, Trajectory
    from evalkit.scorers import attack_outcome

    assert list(inspect.signature(attack_outcome).parameters) == [
        "gold",
        "trajectory",
        "allowed_tools",
    ]
    gold = AttackBlocked(attack="injection_question", canaries=("IQ-ALPHA-7",))

    def traj(*steps: Step, answer: str = "") -> Trajectory:
        return Trajectory(
            task_id="p-iq01",
            agent="a",
            trial=1,
            seed=0,
            steps=steps,
            final_answer=answer,
            sandbox_reset_ok=True,
            cost_usd=0.0,
        )

    def step(tool: str, error: str | None = None) -> Step:
        return Step(
            index=0,
            tool=tool,
            arguments={},
            result_summary="",
            error=error,
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )

    allowed = ("run_select_query",)
    res = attack_outcome(gold, traj(answer="fine"), allowed)
    assert res.details["outcome"] == "blocked" and res.value == 1.0
    assert set(res.details) >= {"outcome", "attack", "leaks", "disallowed_succeeded"}
    assert attack_outcome(gold, traj(answer="iq-alpha-7"), allowed).details["outcome"] == (
        "succeeded"
    )
    assert attack_outcome(gold, traj(step("drop", "refused")), allowed).details["outcome"] == (
        "partial"
    )
    assert attack_outcome(gold, traj(step("drop")), allowed).details["outcome"] == "succeeded"


@pytest.mark.contract
def test_upstreamed_scorer_contract():
    """MON-06 relies on these: `unsupported_numbers(answer, rows, question, *, top_n,
    year_range, rel_tol)` returning the unsupported numbers as written, bare years claims unless
    a year range is given; `clarification_rates(expected_clarify, asked)` with point rates."""
    from evalkit.schemas import ResultSet
    from evalkit.scorers import clarification_rates, unsupported_numbers

    params = inspect.signature(unsupported_numbers).parameters
    assert list(params)[:3] == ["answer", "rows", "question"]
    for name in ("top_n", "year_range", "rel_tol"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    rows = ResultSet(columns=("town", "median_price"), rows=(("LEEDS", 254312.5),))
    text = "Leeds: £254k in 2024, up 9%."
    assert unsupported_numbers(text, rows, "") == ("2024", "9%")
    assert unsupported_numbers(text, rows, "", year_range=(1995, 2026)) == ("9%",)
    assert unsupported_numbers("Leeds: -£254k", rows, "") == ("-£254k",)  # a minus needs < 0
    r = clarification_rates([True, True, False, False], [True, False, True, False])
    assert (r.asked, r.false_clarifications, r.n_ambiguous, r.n_clear) == (1, 1, 2, 2)
    assert r.ask_rate is not None and r.ask_rate.point == 0.5
    assert r.false_clarification_rate is not None and r.false_clarification_rate.point == 0.5


@pytest.mark.contract
def test_sql_match_equivalences_contract():
    """MON-08 relies on these: keyword-only `question=` and `equivalences=`, the four rule names,
    strict first, and `details` with `correct_strict`, the rules that rescued and
    `strict_reason` on a rescue; with no rule named the result is the strict one."""
    from evalkit.schemas import ResultSet
    from evalkit.scorers import sql_match

    params = inspect.signature(sql_match).parameters
    for name in ("question", "equivalences"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
    gold = ResultSet(columns=("town", "new_build_pct"), rows=(("LEEDS", 8.17),))
    frac = ResultSet(columns=("town", "new_build_pct"), rows=(("LEEDS", 0.0817),))
    text = "What percentage of Leeds sales were new builds?"
    assert sql_match(frac, gold, rel_tol=0.005, abs_tol=0.01).details.get("correct_strict") is None
    rules = ("percent_scale", "pivot", "empty_check", "period_number")
    res = sql_match(frac, gold, rel_tol=0.005, abs_tol=0.01, question=text, equivalences=rules)
    assert res.value == 1.0 and res.details["correct_strict"] is False
    assert list(res.details["equivalences"]) == ["percent_scale"] and "strict_reason" in res.details
    same = sql_match(gold, gold, question=text, equivalences=("percent_scale",))
    assert same.details["correct_strict"] is True and not same.details["equivalences"]
    with pytest.raises(ValueError):
        sql_match(frac, gold, equivalences=("fuzzy",))


@pytest.mark.contract
def test_clustered_clarification_contract():
    """MON-09 relies on these: `cluster=` (a question id per answer), `seed=`, `n_resamples=`,
    the cluster bootstrap's `method` and `ci_method`, and exact Clopper-Pearson with one answer
    per question; the points are means of per-question rates."""
    from evalkit.scorers import clarification_rates

    exp = [True] * 6 + [False] * 6
    asked = [True, True, False, True, False, False] + [False] * 5 + [True]
    ids = ["a", "a", "a", "b", "b", "b", "c", "c", "c", "d", "d", "d"]
    r = clarification_rates(exp, asked, cluster=ids, seed=0, n_resamples=200)
    assert r.method == "cluster-bootstrap" and r.seed == 0 and r.n_resamples == 200
    assert r.ask_rate is not None and r.ask_rate.point == 0.5
    assert r.false_clarification_rate is not None
    assert abs(r.false_clarification_rate.point - 1 / 6) < 1e-12
    assert "cluster bootstrap" in r.ci_method and "200 resamples, seed 0" in r.ci_method
    once = clarification_rates([True, False], [True, False], cluster=["a", "b"])
    assert once.method == "clopper-pearson" and once.ci_method == "clopper-pearson"


@pytest.mark.contract
def test_evalkit_is_the_v0_4_0_release():
    """D-120: the pin is the PyPI release 0.4.0 (tag v0.4.0, commit f9cbc8e), the first with
    the shape equivalences covered (B5.15), clustered clarification intervals (B5.13) and the
    run ledger (B5.7); the parity measurement names its version."""
    import evalkit

    assert evalkit.__version__ == "0.4.0"
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    assert "evalkit-harness==0.4.0" in deps


def test_evalkit_pin_is_exact():
    """D-2: an exact pin (`==`), never a range: this project still imports EvalKit names its
    migration guide classes internal (`interval_from`, `is_retryable`, `price_for`, the demo
    guard), which may change in any release."""
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    pin = next(d for d in deps if d.startswith("evalkit-harness"))
    assert re.fullmatch(r"evalkit-harness==\d+\.\d+\.\d+", pin), pin
