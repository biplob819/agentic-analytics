"""SCR-02: result match via sql_match(allow_extra_columns=True) at the question's tolerance."""

from __future__ import annotations

import pytest

from agentic_analytics.schemas import ResultSet, Tolerance
from agentic_analytics.scoring import ScoringError, match_result, result_match
from tests.schemas.factories import ambiguous, execution, probe, question

GOLD = ResultSet(columns=("town", "median_price"), rows=(("LEEDS", 250000), ("YORK", 280000)))


def run(rows, columns=("town", "median_price"), **kw):
    rs = ResultSet(columns=columns, rows=rows)
    return execution(result=rs, rows_total=kw.pop("rows_total", len(rows)), **kw)


def test_same_rows_in_any_order_match():
    ex = run((("YORK", 280000), ("LEEDS", 250000)))
    assert result_match(ex, GOLD, question()) == (True, None)


def test_extra_helpful_columns_pass():
    ex = run(
        (("LEEDS", 250000, 812), ("YORK", 280000, 400)),
        columns=("town", "median_price", "sales"),
    )
    assert result_match(ex, GOLD, question()) == (True, None)


def test_extra_or_missing_rows_fail_with_reason():
    ok, why = result_match(run((("LEEDS", 250000),)), GOLD, question())
    assert ok is False and why is not None and why.startswith("row_count")
    extra = run((("LEEDS", 250000), ("YORK", 280000), ("BATH", 1)))
    assert result_match(extra, GOLD, question())[0] is False


def test_planted_value_defect_fails_at_default_tolerance():
    # EK L-1: a 0.4% error passes only when the question allows it
    ex = run((("LEEDS", 251000.0), ("YORK", 280000)))
    assert result_match(ex, GOLD, question())[0] is False
    loose = question(tolerance=Tolerance(rel=0.005))
    assert result_match(ex, GOLD, loose) == (True, None)


def test_absolute_tolerance_is_used():
    ex = run((("LEEDS", 250000.4), ("YORK", 280000)))
    assert result_match(ex, GOLD, question(tolerance=Tolerance(rel=0.0, abs=0.5)))[0] is True
    assert result_match(ex, GOLD, question(tolerance=Tolerance(rel=0.0, abs=0.1)))[0] is False


def test_order_matters_only_for_ordered_questions():
    swapped = run((("YORK", 280000), ("LEEDS", 250000)))
    ok, why = result_match(swapped, GOLD, question(ordered=True))
    assert ok is False and why is not None and why.startswith("row_mismatch")
    in_order = run((("LEEDS", 250000), ("YORK", 280000)))
    assert result_match(in_order, GOLD, question(ordered=True)) == (True, None)


def test_gold_order_flag_comes_from_the_question_not_the_stored_gold():
    stored = ResultSet(columns=GOLD.columns, rows=GOLD.rows, ordered=True)
    swapped = run((("YORK", 280000), ("LEEDS", 250000)))
    assert result_match(swapped, stored, question(ordered=False)) == (True, None)


@pytest.mark.parametrize("err", ["sql_error", "guard", "timeout", "transport"])
def test_failed_execution_is_incorrect(err):
    ex = execution(result=None, rows_total=None, error=err)
    assert result_match(ex, GOLD, question()) == (False, f"execution failed: {err}")


def test_missing_execution_is_incorrect():
    assert result_match(None, GOLD, question()) == (False, "no SQL to execute")


def test_truncated_execution_is_incorrect():
    ex = run((("LEEDS", 250000), ("YORK", 280000)), rows_total=20_000, truncated=True)
    ok, why = result_match(ex, GOLD, question())
    assert ok is False and why == "result truncated: 2 of 20000"


def test_empty_gold_matches_an_empty_result():
    empty = ResultSet(columns=("town",), rows=())
    ex = execution(result=empty, rows_total=0)
    assert result_match(ex, empty, question(expects_empty=True)) == (True, None)


def test_clarify_expected_questions_have_no_match():
    assert result_match(run((("LEEDS", 250000),)), GOLD, ambiguous()) == (None, None)
    assert result_match(None, None, ambiguous()) == (None, None)


def test_missing_gold_for_an_answer_question_is_a_wiring_error():
    with pytest.raises(ScoringError, match="q-001"):
        result_match(run((("LEEDS", 250000),)), None, question())


def test_probes_match_only_when_gold_is_given():
    p = probe()
    assert result_match(run(GOLD.rows), None, p) == (None, None)
    assert result_match(run(GOLD.rows), GOLD, p) == (True, None)


def test_match_result_reports_no_columns():
    ex = run((("x",), ("y",)), columns=("other",))
    ok, why = match_result(ex, GOLD, tolerance=Tolerance(), ordered=False)
    assert ok is False and why is not None and "column" in why


GOLD_MS = ResultSet(columns=("median_price", "sales"), rows=((250000, 812), (260000, 900)))


def test_renamed_and_reordered_columns_match_without_an_extra_column():
    # verifier HIGH: equal column count with other names made sql_match compare by position
    ex = run(((812, 250000), (900, 260000)), columns=("n", "med"))
    assert result_match(ex, GOLD_MS, question()) == (True, None)
    wrong = run(((812, 250000), (901, 260000)), columns=("n", "med"))
    assert result_match(wrong, GOLD_MS, question())[0] is False


def test_renamed_columns_within_tolerance_still_map():
    # float noise below the tolerance must not stop the columns being paired
    ex = run(((812, 250000.00004), (900, 259999.99996)), columns=("n", "med"))
    assert result_match(ex, GOLD_MS, question(tolerance=Tolerance(rel=1e-6)))[0] is True


def test_columns_matched_by_name_ignore_case_and_order():
    ex = run(((900, 260000), (812, 250000)), columns=("SALES", "Median_Price"))
    assert result_match(ex, GOLD_MS, question()) == (True, None)


def test_default_tolerance_is_tight_enough_for_counts():
    gold = ResultSet(columns=("sales",), rows=((29_000_000,),))
    ex = execution(result=ResultSet(columns=("sales",), rows=((29_000_025,),)), rows_total=1)
    assert result_match(ex, gold, question())[0] is False


def test_tolerance_applies_only_to_named_columns():
    gold = ResultSet(columns=("avg_price", "sales"), rows=((250000.0, 812),))
    q = question(tolerance=Tolerance(rel=0.005, columns=("AVG_PRICE",)))
    near = execution(result=ResultSet(columns=gold.columns, rows=((251000.0, 812),)), rows_total=1)
    assert result_match(near, gold, q) == (True, None)
    off = execution(result=ResultSet(columns=gold.columns, rows=((251000.0, 813),)), rows_total=1)
    assert result_match(off, gold, q)[0] is False
    # without columns the same tolerance covers the count too
    assert result_match(off, gold, question(tolerance=Tolerance(rel=0.005)))[0] is True


def test_exact_columns_accept_integral_floats():
    gold = ResultSet(columns=("avg_price", "sales"), rows=((250000.0, 812),))
    q = question(tolerance=Tolerance(rel=0.005, columns=("avg_price",)))
    ex = execution(result=ResultSet(columns=("a", "s"), rows=((250100.0, 812.0),)), rows_total=1)
    assert result_match(ex, gold, q) == (True, None)


def test_wide_tables_fall_back_to_sql_match():
    cols = tuple(f"c{i}" for i in range(9))
    gold = ResultSet(columns=("x",), rows=((7,),))
    ex = execution(result=ResultSet(columns=cols, rows=(tuple(range(9)),)), rows_total=1)
    assert result_match(ex, gold, question()) == (True, None)
    miss = execution(result=ResultSet(columns=cols, rows=((0,) * 9,)), rows_total=1)
    assert result_match(miss, gold, question())[0] is False


def test_no_compatible_columns_is_reported():
    gold = ResultSet(columns=("town",), rows=(("LEEDS",),))
    ex = execution(result=ResultSet(columns=("t",), rows=(("YORK",),)), rows_total=1)
    ok, why = result_match(ex, gold, question())
    assert ok is False and why is not None and why.startswith("no_matching_columns")
