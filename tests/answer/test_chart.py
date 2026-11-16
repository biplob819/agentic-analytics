"""ANS-01: one deterministic chart builder for every backend (D-6)."""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from agentic_analytics.answer import MAX_BARS, VEGA_LITE_SCHEMA, chart_for
from agentic_analytics.schemas import ResultSet
from tests.schemas import factories as f


def rs(columns, rows) -> ResultSet:
    return ResultSet(columns=tuple(columns), rows=tuple(tuple(r) for r in rows))


def kind(spec) -> str:
    return spec["usermeta"]["chart"]


# -- KPI --------------------------------------------------------------------------------------
def test_single_value_is_a_kpi_text_mark():
    spec = chart_for(rs(["median_price"], [[250000]]), "Median price in Leeds?")
    assert spec is not None
    assert spec["$schema"] == VEGA_LITE_SCHEMA == "https://vega.github.io/schema/vega-lite/v5.json"
    assert kind(spec) == "kpi"
    assert spec["mark"]["type"] == "text"
    assert spec["data"] == {"values": [{"value": 250000}]}
    assert spec["encoding"]["text"]["field"] == "value"
    assert spec["encoding"]["text"]["type"] == "quantitative"
    assert spec["title"] == "Median price in Leeds?"


def test_single_text_value_is_a_nominal_kpi():
    spec = chart_for(rs(["district"], [["KENSINGTON AND CHELSEA"]]))
    assert spec is not None and kind(spec) == "kpi"
    assert spec["encoding"]["text"]["type"] == "nominal"
    assert spec["title"] == "district"


def test_single_null_value_has_no_chart():
    assert chart_for(rs(["median_price"], [[None]])) is None


def test_one_row_of_two_measures_has_no_chart():
    assert chart_for(f.result()) is None  # (median_price, sales): the table says it better


def test_one_row_of_a_period_and_a_measure_has_no_chart():
    """A2.3 live: GPT answered q-001 with `SELECT toYear(date) AS year, count() ... GROUP BY
    year` for one year; the old builder drew a one-point line."""
    assert chart_for(rs(["year", "property_sales"], [[2023, 861782]])) is None
    assert chart_for(rs(["month", "sales"], [["2023-01-01", 5]])) is None
    assert chart_for(rs(["year", "sales"], [[2023, 5], [2024, 6]])) is not None  # two rows draw


def test_one_row_of_a_label_and_a_measure_has_no_chart():
    assert chart_for(rs(["town", "sales"], [["LEEDS", 9785]])) is None  # not a one-bar chart
    assert chart_for(rs(["sales", "town"], [[9785, "LEEDS"]])) is None


# -- line -------------------------------------------------------------------------------------
def test_iso_dates_and_a_measure_give_a_line_sorted_by_period():
    spec = chart_for(
        rs(["month", "sales"], [["2024-03-01", 30], ["2024-01-01", 10], ["2024-02-01", 20]])
    )
    assert spec is not None and kind(spec) == "line"
    assert spec["mark"]["type"] == "line"
    assert spec["data"]["values"] == [
        {"period": "2024-01-01", "value": 10},
        {"period": "2024-02-01", "value": 20},
        {"period": "2024-03-01", "value": 30},
    ]
    x, y = spec["encoding"]["x"], spec["encoding"]["y"]
    assert (x["field"], x["type"], x["title"]) == ("period", "temporal", "month")
    assert x["scale"] == {"type": "utc"} and x["timeUnit"] == "utcyearmonthdate"
    assert (y["field"], y["type"], y["title"]) == ("value", "quantitative", "sales")
    assert spec["title"] == "sales by month"


def test_year_month_strings_are_a_period():
    spec = chart_for(rs(["ym", "avg_price"], [["2024-02", 2.5], ["2024-01", 1.5]]))
    assert spec is not None and kind(spec) == "line"
    assert [v["period"] for v in spec["data"]["values"]] == ["2024-01", "2024-02"]


def test_year_month_strings_use_a_utc_month_unit():
    spec = chart_for(rs(["ym", "n"], [["2024-01", 1], ["2024-02", 2]]))
    assert spec is not None
    assert spec["encoding"]["x"]["timeUnit"] == "utcyearmonth"
    assert spec["encoding"]["x"]["scale"] == {"type": "utc"}


def test_mixed_iso_formats_are_not_a_period():
    spec = chart_for(rs(["d", "n"], [["2024-01", 1], ["2024-02-01", 2]]))
    assert spec is not None and kind(spec) == "bar"


def test_repeated_periods_have_no_chart():
    assert chart_for(rs(["month", "n"], [["2024-01-01", 1], ["2024-01-01", 2]])) is None
    assert chart_for(rs(["year", "n"], [[2020, 1], [2020, 2], [2021, 3]])) is None


def test_repeated_labels_have_no_chart():
    assert chart_for(rs(["town", "n"], [["LEEDS", 1], ["YORK", 2], ["LEEDS", 3]])) is None


def test_truncated_results_have_no_chart():
    bars = rs(["town", "n"], [["A", 1], ["B", 2]])
    assert chart_for(bars, rows_total=2) is not None
    assert chart_for(bars, rows_total=3) is None
    assert chart_for(rs(["month", "n"], [["2024-01", 1], ["2024-02", 2]]), rows_total=10) is None
    assert chart_for(rs(["v"], [[5]]), rows_total=2) is None


def test_rows_total_below_the_rows_held_is_an_error():
    import pytest

    with pytest.raises(ValueError, match="smaller"):
        chart_for(rs(["town", "n"], [["A", 1], ["B", 2]]), rows_total=1)
    with pytest.raises(ValueError, match="integer"):
        chart_for(rs(["town", "n"], [["A", 1]]), rows_total=True)


def test_bar_cut_note_uses_the_full_untruncated_total():
    rows = [[f"t{i:02d}", i] for i in range(30)]
    spec = chart_for(rs(["town", "n"], rows), rows_total=30)
    assert spec is not None and spec["title"].endswith("(top 25 of 30)")
    assert spec["usermeta"]["rows_total"] == 30


def test_boolean_single_cell_has_no_chart():
    assert chart_for(rs(["flag"], [[True]])) is None


def test_integers_beyond_2_pow_53_have_no_chart():
    big = 2**53 + 1
    assert chart_for(rs(["n"], [[big]])) is None
    assert chart_for(rs(["town", "n"], [["A", big], ["B", 1]])) is None
    assert kind(chart_for(rs(["n"], [[2**53]]))) == "kpi"
    # and below -2^53 (A2.3 mutation testing: the lower bound could be dropped unnoticed)
    assert chart_for(rs(["n"], [[-big]])) is None
    assert chart_for(rs(["town", "n"], [["A", -big], ["B", 1]])) is None
    assert kind(chart_for(rs(["n"], [[-(2**53)]]))) == "kpi"


def test_invalid_calendar_dates_are_not_a_period():
    # 2024-13 is not a month, so the column is text and the result a bar chart
    spec = chart_for(rs(["ym", "n"], [["2024-13", 1], ["2024-01", 2]]))
    assert spec is not None and kind(spec) == "bar"


def test_year_named_integer_column_is_ordinal_period_even_out_of_range():
    spec = chart_for(rs(["sale_year", "n"], [[1985, 5], [1984, 4]]))
    assert spec is not None and kind(spec) == "line"
    assert spec["encoding"]["x"]["type"] == "ordinal"
    assert [v["period"] for v in spec["data"]["values"]] == [1984, 1985]


def test_camel_case_year_name_counts_but_yearly_does_not():
    assert kind(chart_for(rs(["toYear(date)", "n"], [[1980, 1], [1981, 2]]))) == "line"
    assert chart_for(rs(["yearly", "n"], [[1980, 1], [1981, 2]])) is None


def test_unnamed_four_digit_ints_in_range_are_a_period():
    spec = chart_for(rs(["y", "median"], [[2001, 150000], [2000, 140000]]))
    assert spec is not None and kind(spec) == "line"
    assert spec["encoding"]["x"]["title"] == "y"


def test_range_bounds_are_inclusive_1990_to_2100():
    assert kind(chart_for(rs(["a", "n"], [[1990, 1.5], [1991, 2.5]]))) == "line"
    assert kind(chart_for(rs(["a", "n"], [[2099, 1.5], [2100, 2.5]]))) == "line"
    assert chart_for(rs(["a", "n"], [[1989, 1.5], [1990, 2.5]])) is None
    assert chart_for(rs(["a", "n"], [[2100, 1.5], [2101, 2.5]])) is None


def test_unnamed_in_range_ints_must_be_consecutive():
    assert chart_for(rs(["a", "n"], [[2001, 1.5], [2005, 2.5]])) is None  # a gap
    assert chart_for(rs(["a", "n"], [[2001, 1.5]])) is None  # one value is not a series
    assert kind(chart_for(rs(["a", "n"], [[2003, 1.5], [2001, 2.5], [2002, 3.5]]))) == "line"


def test_id_and_measure_names_never_read_as_unnamed_years():
    assert chart_for(rs(["store_id", "n"], [[2001, 1.5], [2002, 2.5]])) is None
    assert chart_for(rs(["price", "n"], [[2001, 1.5], [2002, 2.5]])) is None
    assert chart_for(rs(["postcodeCode", "n"], [[2001, 1.5], [2002, 2.5]])) is None


def test_yr_token_is_a_year_but_years_is_not():
    spec = chart_for(rs(["sale_yr", "n"], [[1985, 5], [1999, 4]]))
    assert spec is not None and kind(spec) == "line" and spec["encoding"]["x"]["title"] == "sale_yr"
    assert chart_for(rs(["years", "n"], [[1985, 5], [1999, 4]])) is None


def test_named_year_beats_range_and_leftmost_wins_a_tie():
    named = chart_for(rs(["sales", "year"], [[2000, 2020], [1995, 2021]]))
    assert named is not None and named["encoding"]["x"]["title"] == "year"
    tie = chart_for(rs(["a", "b"], [[2000, 2020], [2001, 2021]]))
    assert tie is not None and tie["encoding"]["x"]["title"] == "a"


def test_float_years_are_not_a_period():
    assert chart_for(rs(["year", "n"], [[2020.0, 1], [2021.0, 2]])) is None


# -- bar --------------------------------------------------------------------------------------
def test_category_and_measure_give_bars_sorted_descending_ties_by_label():
    spec = chart_for(
        rs(["district", "median"], [["B", 10], ["A", 30], ["C", 30], ["D", 20]]), "Top districts"
    )
    assert spec is not None and kind(spec) == "bar"
    assert spec["mark"]["type"] == "bar"
    assert spec["data"]["values"] == [
        {"label": "A", "value": 30},
        {"label": "C", "value": 30},
        {"label": "D", "value": 20},
        {"label": "B", "value": 10},
    ]
    assert spec["encoding"]["y"]["field"] == "label"
    assert spec["encoding"]["y"]["sort"] is None  # data order is the order
    assert spec["encoding"]["y"]["title"] == "district"
    assert spec["encoding"]["x"]["title"] == "median"
    assert spec["title"] == "Top districts"
    # ties by label whatever the row order (A2.3 mutation testing: a stable sort on the value
    # alone passed the case above, whose tied rows were already in label order)
    tied = chart_for(rs(["district", "median"], [["C", 30], ["B", 10], ["A", 30]]))
    assert tied is not None and [v["label"] for v in tied["data"]["values"]] == ["A", "C", "B"]


def test_measure_column_may_come_first():
    spec = chart_for(rs(["n", "town"], [[3, "LEEDS"], [5, "YORK"]]))
    assert spec is not None and [v["label"] for v in spec["data"]["values"]] == ["YORK", "LEEDS"]


def test_bars_cut_at_25_with_a_note_in_the_title():
    rows = [[f"town-{i:02d}", i] for i in range(40)]
    spec = chart_for(rs(["town", "n"], rows), "Sales by town")
    assert MAX_BARS == 25
    assert spec is not None
    values = spec["data"]["values"]
    assert len(values) == 25
    assert values[0] == {"label": "town-39", "value": 39}
    assert values[-1] == {"label": "town-15", "value": 15}
    assert spec["title"] == "Sales by town (top 25 of 40)"
    assert spec["usermeta"]["rows_shown"] == 25 and spec["usermeta"]["rows_total"] == 40


def test_exactly_25_bars_are_not_cut():
    spec = chart_for(rs(["town", "n"], [[f"t{i:02d}", i] for i in range(25)]))
    assert spec is not None and len(spec["data"]["values"]) == 25
    assert "top" not in spec["title"]


def test_dotted_column_names_never_become_vega_paths():
    spec = chart_for(rs(["t.town", "avg[price]"], [["A", 1.5], ["B", 2.5]]))
    assert spec is not None
    assert spec["encoding"]["y"]["field"] == "label" and spec["encoding"]["x"]["field"] == "value"


# -- no chart ---------------------------------------------------------------------------------
def test_empty_result_has_no_chart():
    assert chart_for(rs(["town", "n"], [])) is None


def test_null_in_either_column_has_no_chart():
    assert chart_for(rs(["town", "n"], [["A", 1], [None, 2]])) is None
    assert chart_for(rs(["town", "n"], [["A", 1], ["B", None]])) is None


def test_two_measures_or_extra_columns_have_no_chart():
    assert chart_for(rs(["town", "n", "avg"], [["A", 1, 2.0]])) is None
    assert chart_for(rs(["a", "b"], [[1, 2], [3, 4]])) is None


def test_booleans_are_not_measures():
    assert chart_for(rs(["town", "flag"], [["A", True], ["B", False]])) is None


def test_two_text_columns_have_no_chart():
    assert chart_for(rs(["town", "county"], [["A", "X"], ["B", "Y"]])) is None


def test_nested_values_have_no_chart():
    assert chart_for(rs(["town", "n"], [["A", [1, 2]]])) is None


def test_blank_question_falls_back_to_derived_title():
    spec = chart_for(rs(["town", "n"], [["A", 1], ["B", 2]]), "   ")
    assert spec is not None and spec["title"] == "n by town"


def test_long_question_titles_are_cut_before_the_bar_note():
    # A2.3 verifier: removing the cut passed every test
    rows = [[f"t{i:02d}", i] for i in range(MAX_BARS + 1)]
    spec = chart_for(rs(["town", "n"], rows), "q" * 300)
    assert spec is not None
    assert spec["title"] == "q" * 200 + f" (top {MAX_BARS} of {MAX_BARS + 1})"


# -- properties -------------------------------------------------------------------------------
SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**15), max_value=10**15),
    st.integers(min_value=1980, max_value=2110),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
    st.dates().map(lambda d: d.isoformat()),
    st.dates().map(lambda d: d.isoformat()[:7]),
)
NAMES = st.sampled_from(["year", "town", "n", "month", "a.b", "toYear(date)", "price", "yr"])


@st.composite
def result_sets(draw) -> ResultSet:
    ncols = draw(st.integers(min_value=1, max_value=3))
    cols = draw(st.lists(NAMES, min_size=ncols, max_size=ncols))
    nrows = draw(st.integers(min_value=0, max_value=40))
    # each column draws from one family most of the time, so every chart branch is exercised
    per_col = [
        draw(
            st.sampled_from(
                [
                    SCALARS,
                    st.integers(min_value=1985, max_value=2105),
                    st.floats(allow_nan=False, allow_infinity=False, width=32),
                    st.sampled_from(["A", "B", "C", "D", "E"]),
                    st.dates().map(lambda d: d.isoformat()),
                ]
            )
        )
        for _ in cols
    ]
    rows = [[draw(s) for s in per_col] for _ in range(nrows)]
    return rs(cols, rows)


@settings(max_examples=400, deadline=None)
@given(result_sets(), st.one_of(st.none(), st.text(max_size=20)), st.integers(0, 3))
def test_property_never_raises_json_round_trips_and_is_deterministic(result, question, extra):
    total = len(result.rows) + extra
    spec = chart_for(result, question, rows_total=total)
    assert spec == chart_for(result, question, rows_total=total)
    if extra:
        assert spec is None  # truncated
    spec = chart_for(result, question)
    assert spec == chart_for(result, question)
    if spec is None:
        return
    assert len(result.rows) == 1 if kind(spec) == "kpi" else len(result.rows) >= 2
    assert json.loads(json.dumps(spec, allow_nan=False)) == spec
    # it is a valid AnalyticsAnswer.chart (JSON values only, deep-frozen)
    answer = f.answer(chart=spec)
    assert json.loads(json.dumps(answer.model_dump(mode="json")["chart"])) == spec
    # never invents rows: every value shown is a cell of the executed rows
    cells = {json.dumps(c) for row in result.rows for c in row}
    for item in spec["data"]["values"]:
        for value in item.values():
            assert json.dumps(value) in cells
