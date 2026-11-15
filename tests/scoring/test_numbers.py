"""SCR-04 and SCR-06: answer-from-rows number check on hand-built fixtures (EK L-35)."""

from __future__ import annotations

import pytest

from agentic_analytics.schemas import ResultSet
from agentic_analytics.scoring.numbers import extract_numbers, unsupported_numbers

ROWS = ResultSet(
    columns=("district", "median_price", "sales", "yoy_pct", "share"),
    rows=(
        ("KENSINGTON AND CHELSEA", 1249000, 812, 12.5, 0.2),
        ("MANCHESTER", 190312, 5321, -3.2, 0.0345),
        ("LEEDS", 254312.5, 4100, 4.0, 0.125),
    ),
)
Q = "What was the median price by district in 2024?"

# (answer text, expected unsupported numbers); question Q, no top-N, rows ROWS
CASES: list[tuple[str, tuple[str, ...]]] = [
    # exact and separators
    ("The median in Manchester was £190,312.", ()),
    ("Manchester: 190312", ()),
    ("Manchester came in at £190312.00.", ()),
    ("Kensington and Chelsea: £1,249,000", ()),
    ("Leeds sold 4,100 homes and Manchester 5,321.", ()),
    ("Leeds sold 4100 homes.", ()),
    ("GBP 190,312 in Manchester", ()),
    ("**£190,312** in Manchester", ()),
    # displayed precision: half a unit of the last digit written
    ("roughly 190k in Manchester", ()),
    ("about £190k", ()),
    ("£190.3k", ()),
    ("Leeds was £254k.", ()),
    ("Leeds was £254.3k.", ()),
    ("Manchester recorded 5.3k sales", ()),
    ("Kensington topped £1.2m", ()),
    ("Kensington topped 1.25m", ()),
    ("Kensington: 1.249 million", ()),
    ("Kensington: 1.2 million", ()),
    ("c.£190k", ()),
    # within 0.5% relative
    ("about £190,000 in Manchester", ()),
    # near-misses that must be caught
    ("Leeds was £260k.", ("£260k",)),
    ("Kensington topped £1.3m", ("£1.3m",)),
    ("Manchester recorded 850 sales", ("850",)),
    ("prices rose 12.6%", ("12.6%",)),
    ("a total of £1.25bn changed hands", ("£1.25bn",)),
    ("about £500", ("£500",)),
    # percentages: the cell may hold the percent or the fraction
    ("Kensington rose 12.5%", ()),
    ("Kensington rose 12.5 percent", ()),
    ("Kensington took 20% of sales", ()),
    ("Manchester's share was 3.45%", ()),
    ("Manchester fell 3.2%", ()),
    ("Manchester: -3.2%", ()),
    ("Manchester: −3.2%", ()),
    ("a swing of 3.2 percentage points", ()),
    ("Leeds was up 4.0%", ()),
    ("share of 0.125", ()),
    # years, dates and ranges
    ("Between 2019-2024 prices rose.", ()),
    ("From 2019 to 2024 prices rose.", ()),
    ("As of 2024-01-01 the median was £190,312.", ()),
    ("Sold on 01/03/2024 for £190,312.", ()),
    ("In 2024/25 and 2024-03 prices held.", ()),
    ("Prices in 1890 were lower.", ("1890",)),
    ("By 2031 prices may double.", ("2031",)),
    ("Unlike the late 1990s, prices rose.", ()),  # the decade overlaps 1995-2026
    ("Unlike the 1980s, prices rose.", ("1980s",)),
    ("Prices in the 2000s rose.", ()),
    ("between £200k-£300k", ("£200k", "£300k")),
    ("between £1.2-1.3m", ("1.3m",)),
    ("growth of 4-12.5%", ()),
    # things that are not number claims
    ("The M1 and SW1A areas, plus LS6, were excluded.", ()),
    ("In Q3 2024 the median was £190,312.", ()),
    ("Leeds was the 3rd most expensive, 2nd by sales.", ()),
    ("1. KENSINGTON AND CHELSEA\n2. MANCHESTER\n3) LEEDS", ()),
    ("| 1 | KENSINGTON AND CHELSEA | £1,249,000 |\n| 2 | LEEDS | £254k |", ()),
    ("#1 is Kensington [1]", ()),
    ("Updated at 10:30 today.", ()),
    ("Prices hit a 10-year high.", ()),
    ("On 1st January 2024 Leeds was £254k.", ()),
    ("```sql\nSELECT * FROM t LIMIT 50\n```\nManchester: £190k", ()),
    ("", ()),
    ("No sales were recorded for that district.", ()),
    # the row count of the result is allowed; other small counts are not
    ("All 3 districts are listed.", ()),
    ("Only 2 districts rose.", ("2",)),
    # number words
    ("half a million homes", ("half a million",)),
    ("three million pounds", ("three million",)),
    ("around a million in Kensington", ()),
    ("two hundred and fifty-four thousand in Leeds", ()),
    ("a quarter of a million in Leeds", ()),  # 250,000 +/- 5,000 covers 254,312.5
    ("a quarter of a billion", ("a quarter of a billion",)),
    ("four thousand one hundred homes in Leeds", ()),
    ("four thousand two hundred homes", ("four thousand two hundred",)),
    # dates in words are not claims
    ("On 31 December 2024 Leeds was £254k.", ()),
    ("On 1 Jan 2024 Leeds was £254k.", ()),
    ("On January 31, 2024 Leeds was £254k.", ()),
    ("On 31/12/24 Leeds was £254k.", ()),
    ("In Q3 '24 Leeds was £254k.", ()),
    ("In Q3 ’24 Leeds was £254k.", ()),
    # larger numbers stay claims however they are glued or placed
    ("| 999 |", ("999",)),
    ("| 4100 |", ()),
    ("Leeds sold £260,000-plus homes", ("£260,000",)),
    ("Leeds sold 260,000-odd homes", ("260,000",)),
    ("Leeds was c.999k", ("999k",)),  # without c. blanking the 999 would be missed
    ("Leeds sold 999homes", ("999",)),
    ("999. That is the median.", ("999",)),
    # locale forms are parsed, then checked
    ("Leeds: 254.312,5", ()),
    ("Leeds: 9.999,5", ("9.999,5",)),
    ("Kensington: 12,49,000", ()),
    ("Kensington: 12,34,567", ("12,34,567",)),
    ("Kensington: 1.249.000", ()),
    # percentages compare by column name
    ("0.2% of sales went to Kensington", ("0.2%",)),
    # multiples and duplicates
    ("Kensington is 10x Manchester", ("10x",)),
    ("£260k, and again £260k", ("£260k",)),
]


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[c[0][:40] for c in CASES])
def test_number_fixture(text, expected):
    assert unsupported_numbers(text, ROWS, Q) == expected


def test_there_are_at_least_forty_fixtures():
    assert len(CASES) >= 40


def test_top_n_is_allowed_only_on_top_n_questions():
    text = "The top 10 districts by median price are listed."
    assert unsupported_numbers(text, ROWS, "Which districts are most expensive?") == ("10",)
    assert unsupported_numbers(text, ROWS, "Most expensive districts?", top_n=10) == ()


def test_numbers_in_the_question_are_allowed():
    q = "Which districts had more than 500 sales and a median over £1m?"
    text = "Districts with more than 500 sales and a median over £1,000,000: Kensington."
    assert unsupported_numbers(text, ROWS, q) == ()


def test_year_range_is_configurable():
    assert unsupported_numbers("In 1996 prices fell.", ROWS, Q) == ()
    assert unsupported_numbers("In 1996 prices fell.", ROWS, Q, year_range=(2000, 2026)) == (
        "1996",
    )


def test_a_year_with_a_separator_or_currency_is_not_a_year():
    assert unsupported_numbers("There were 2,020 sales.", ROWS, Q) == ("2,020",)
    assert unsupported_numbers("It cost £2020.", ROWS, Q) == ("£2020",)


def test_without_executed_rows_only_the_allowances_remain():
    assert unsupported_numbers("Manchester: £190,312 in 2024.", None, Q) == ("£190,312",)
    assert unsupported_numbers("There were 0 sales.", None, Q) == ()


def test_empty_result_allows_zero():
    empty = ResultSet(columns=("sales",), rows=())
    assert unsupported_numbers("There were 0 sales.", empty, Q) == ()


def test_boolean_cells_never_support_numbers():
    flags = ResultSet(columns=("flag", "n"), rows=((True, 7), (True, 7)))
    assert unsupported_numbers("1 flag", flags, Q) == ("1",)


def test_numeric_string_cells_count():
    rs = ResultSet(columns=("median",), rows=(("254312.50",), ("190,312",)))
    assert unsupported_numbers("£254k and £190k", rs, Q) == ()


def test_planted_hallucination_is_caught_among_supported_numbers():
    # EK L-1: the check must fail on a planted defect, not only pass on clean input
    clean = "Kensington £1.2m, Manchester £190k, Leeds £254k; 812 sales in Kensington."
    assert unsupported_numbers(clean, ROWS, Q) == ()
    planted = clean.replace("812", "912")
    assert unsupported_numbers(planted, ROWS, Q) == ("912",)


def test_percent_matches_fractions_only_in_ratio_columns():
    unnamed = ResultSet(columns=("district", "v"), rows=(("LEEDS", 0.125),))
    assert unsupported_numbers("Leeds rose 12.5%", unnamed, Q) == ("12.5%",)
    rate = ResultSet(columns=("district", "growth_rate"), rows=(("LEEDS", 0.125),))
    assert unsupported_numbers("Leeds rose 12.5%", rate, Q) == ()
    assert unsupported_numbers("Leeds rose 0.125%", rate, Q) == ("0.125%",)
    pct = ResultSet(columns=("district", "pct_change"), rows=(("LEEDS", 0.5),))
    assert unsupported_numbers("Leeds rose 0.5%", pct, Q) == ()  # percent units
    assert unsupported_numbers("Leeds rose 50%", pct, Q) == ()  # or a fraction


def test_row_count_allowance_is_for_plain_integers_only():
    rs = ResultSet(columns=("v",), rows=((1000,), (2000,), (3000,)))
    assert unsupported_numbers("All 3 rows.", rs, Q) == ()
    assert unsupported_numbers("£3 each, 3% up, 3.0 on average, -3 net", rs, Q) == (
        "£3",
        "3%",
        "3.0",
        "-3",
    )


def test_extract_reports_values_and_flags():
    nums = extract_numbers("£254k, 12.5%, 2024 and -3.2")
    assert [n.text for n in nums] == ["£254k", "12.5%", "2024", "-3.2"]
    assert nums[0].value == 254000 and nums[0].candidates[0][1] == 500
    assert nums[1].candidates == ((12.5, 0.05, "pct"),)
    assert nums[2].year_like and nums[2].plain_int
    assert not nums[3].plain_int and nums[3].value == 3.2


def test_large_results_are_checked_quickly():
    big = ResultSet(columns=("v",), rows=tuple((i * 1.5,) for i in range(10_000)))
    text = " ".join(f"{i * 1.5:.1f}" for i in range(0, 10_000, 50)) + " 99999999"
    assert unsupported_numbers(text, big, Q) == ("99999999",)
