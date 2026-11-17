"""OWNER D-62 caveat worded from the run's Snowflake region (R3.1 stamps it in run.json from
`run.yaml` or `SELECT CURRENT_REGION()`), never a fixed string."""

from __future__ import annotations

import pytest

from agentic_analytics.report.format import cortex_latency_caveat, snowflake_region_label
from tests.aggregate.helpers import rec
from tests.report.helpers import html, section, summary

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    ("region", "label"),
    [
        ("AWS_AP_SOUTHEAST_7", "AWS ap-southeast-7 (Thailand)"),
        ("aws_ap_southeast_7", "AWS ap-southeast-7 (Thailand)"),
        ("PUBLIC.AWS_US_WEST_2", "AWS us-west-2 (Oregon)"),
        ("AWS_EU_NORTH_1", "AWS eu-north-1"),
        ("AZURE_EASTUS2", "AZURE eastus2"),
        ("GCP_US_CENTRAL1", "GCP us-central1"),
        ("SOMETHING_ELSE", "SOMETHING_ELSE"),
    ],
)
def test_region_labels(region, label):
    assert snowflake_region_label(region) == label


def test_an_account_outside_the_us_crosses_to_the_us():
    text = cortex_latency_caveat("AWS_AP_SOUTHEAST_7")
    assert text.startswith("Snowflake account in AWS ap-southeast-7 (Thailand);")
    assert "crosses to the US" in text and "OWNER D-62" in text
    assert "crosses to the US" in cortex_latency_caveat("AZURE_WESTEUROPE")


def test_an_account_in_an_aws_us_region_has_no_cross_region_distance():
    text = cortex_latency_caveat("AWS_US_WEST_2")
    assert text.startswith("Snowflake account in AWS us-west-2 (Oregon);")
    assert "crosses" not in text and "no cross-region distance" in text


@pytest.mark.parametrize("region", [None, ""])
def test_an_unrecorded_region_says_so_and_names_no_place(region):
    text = cortex_latency_caveat(region)
    assert text.startswith("Snowflake account region not recorded for this run")
    assert "Thailand" not in text and "ap-southeast" not in text


def test_the_page_uses_the_region_it_is_given():
    records = [rec("q-001", "wrong", backend="cortex_analyst")]
    us = html(summary(), records, snowflake_region="AWS_US_WEST_2")
    th = html(summary(), records, snowflake_region="AWS_AP_SOUTHEAST_7")
    none = html(summary(), records)
    for page, text in ((us, "AWS us-west-2 (Oregon)"), (th, "AWS ap-southeast-7 (Thailand)"),
                       (none, "region not recorded")):  # fmt: skip
        caveat_once_in_cost = section(page, "cost").count('class="caveat"') == 1
        assert caveat_once_in_cost and text in section(page, "cost")
        assert text in section(page, "failures")
    assert "Thailand" not in us and "Thailand" not in none
