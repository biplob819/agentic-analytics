"""DSH-06: licence credit, run stamps, gold mode, synthetic banner, meaning beyond colour."""

from __future__ import annotations

import re

from tests.report.helpers import html, section, summary, text
from tests.schemas import factories as f

CREDIT = "Contains HM Land Registry data © Crown copyright and database right 2021."


def _stamps(page: str) -> str:
    m = re.search(r'<header id="stamps"[^>]*>(.*?)</header>', page, re.S)
    assert m
    return text(m.group(1))


def test_licence_credit_comes_from_the_bundled_pack():
    page = html()
    footer = re.search(r'<footer id="licence"[^>]*>(.*?)</footer>', page, re.S)
    assert footer and CREDIT in text(footer.group(1))
    assert "Open Government Licence v3.0" in text(footer.group(1))
    assert CREDIT in _stamps(page)


def test_an_explicit_pack_or_string_wins_and_unknown_packs_warn():
    custom = f.pack(name="my_data", attribution="Data © Example Org, CC BY 4.0")
    s = summary(pack="my_data@1.0")
    assert "Data © Example Org, CC BY 4.0" in html(s, pack=custom)
    assert "Credit line from caller" in html(s, attribution="Credit line from caller")
    warn = _stamps(html(s))
    assert "Licence attribution unavailable" in warn and "my_data@1.0" in warn
    # a bundled pack whose version differs from the run's is not trusted for the credit
    assert "Licence attribution unavailable" in _stamps(html(summary(pack="uk_price_paid@9.9")))


def test_run_stamps():
    st = _stamps(html())
    assert "uk_price_paid@1.0" in st
    assert "snapshot 0123456789ab" in st
    assert "as of 2026-09-30" in st
    assert "gold check aaaaaaaaaaaa" in st
    assert "run r-0001" in st and "cross-engine gold" in st
    assert 'title="0123456789abcdef' in html()  # full sha on hover


def test_single_engine_runs_are_stamped_prominently():
    page = html(summary(gold_mode="single_engine"))
    m = re.search(r'<p class="stamp warn"[^>]*>(.*?)</p>', page, re.S)
    assert m and "single-engine gold" in text(m.group(1))
    assert "single-engine gold" in text(section(page, "categories"))


def test_synthetic_banner_only_for_the_example():
    assert "SYNTHETIC EXAMPLE" not in html()
    page = html(example=True)
    m = re.search(r'<p class="synthetic"[^>]*>(.*?)</p>', page, re.S)
    assert m and "SYNTHETIC EXAMPLE" in m.group(1)
    assert "<title>SYNTHETIC EXAMPLE" in page


def test_probe_verdicts_use_symbols_and_words():
    sec = section(html(), "probes")
    assert "✓ PASS" in text(sec) and "✗ FAIL" in text(sec)
    assert "1 probe failure" in text(sec)


def test_accessibility_and_themes():
    page = html()
    assert '<html lang="en">' in page
    assert '<a class="skip" href="#main">' in page and '<main id="main"' in page
    assert '<nav aria-label="Sections">' in page
    assert "prefers-color-scheme:dark" in page and ":focus-visible" in page
    for svg in re.findall(r"<svg[^>]*>", page):
        assert 'role="img"' in svg and "aria-labelledby" in svg
