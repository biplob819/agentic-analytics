"""B3.2: the README's generated blocks and the write-up figures come from a summary (REL-01,
REL-03, REL-04): the generators are checked on the bundled SYNTHETIC example, and the
repository's README and figures must regenerate byte for byte from the committed published run
(docs/results/), whose numbers must equal the committed measurement records."""

from __future__ import annotations

import json
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agentic_analytics.report import load_example
from agentic_analytics.report.figures import (
    categories_svg,
    headline_svg,
    uplift_svg,
    writeup_figures,
)
from agentic_analytics.report.readme import (
    CHART_URL,
    END,
    RESULTS_END,
    RESULTS_START,
    START,
    ReadmeError,
    headline_block,
    headline_sentences,
    render_readme,
    results_block,
    splice,
    update_readme,
)
from agentic_analytics.schemas import RunSummary

ROOT = Path(__file__).resolve().parents[2]
CREDIT = (
    "Contains HM Land Registry data © Crown copyright and database right 2021. "
    "This data is licensed under the Open Government Licence v3.0."
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


@pytest.fixture(scope="module")
def summary() -> RunSummary:
    return load_example()[0]


def _variant(summary: RunSummary, **update) -> RunSummary:
    return RunSummary.model_validate({**summary.model_dump(mode="json"), **update})


def _prose(block: str) -> str:
    lines = [ln for ln in block.splitlines() if ln and not ln.startswith(("<!--", "<p "))]
    assert len(lines) == 1, lines
    return lines[0]


def _sentences(text: str) -> list[str]:
    return SENTENCE_END.split(text)


def decimals_problems(text: str) -> list[str]:
    """Numbers with a decimal point that are not printed to exactly one decimal."""
    return [
        m.group(0) for m in re.finditer(r"\d+\.\d+", text) if len(m.group(0).split(".")[1]) != 1
    ]


# REL-01


def test_headline_block_is_one_chart_and_three_sentences(summary):
    block = headline_block(summary)
    assert block.startswith(START + "\n") and block.endswith("\n" + END)
    assert block.count("<img ") == 1 and f'src="{CHART_URL}"' in block
    prose = _prose(block)
    assert _sentences(prose) == headline_sentences(summary)
    assert len(headline_sentences(summary)) == 3
    assert all(s.endswith(".") for s in headline_sentences(summary))


def test_headline_figures_print_to_one_decimal(summary):
    prose = _prose(headline_block(summary))
    assert decimals_problems(prose) == []
    assert decimals_problems("62.5% and 0.638") == ["0.638"]  # the check itself bites
    assert "63.9% (95% CI 52.2–75.0%) for Claude (MCP)" in prose
    assert "74.4% (95% CI 63.9–84.4%) for Cortex Analyst" in prose
    assert "descriptions on Claude (MCP), +17.2 pts (95% CI +7.8 to +27.2)" in prose


def test_headline_states_how_it_was_measured(summary):
    s1, _, s3 = headline_sentences(summary)
    assert s1.startswith("In the bundled SYNTHETIC example (generated data, not a measurement)")
    assert "without an LLM judge" in s1
    assert "3 times" in s1 and "60 questions" in s1
    assert "uk_price_paid@1.0" in s1 and "as of 2026-09-30" in s1
    assert "(cross-engine gold)" in s1
    assert "conservative" in s3 and "about 99% coverage" in s3
    assert "cost figures are estimates" in s3


def test_headline_variants(summary):
    real = _variant(summary, run_id="r-real", gold_mode="single_engine", cost_status="reconciled")
    s1, _, s3 = headline_sentences(real)
    assert "SYNTHETIC" not in s1 and s1.startswith("Claude (MCP), GPT (MCP) and Cortex Analyst")
    assert "(single-engine gold)" in s1
    assert "estimates" not in s3
    one_trial = _variant(
        summary,
        backends=[{**b.model_dump(mode="json"), "trials": 1} for b in summary.backends],
    )
    assert "pack once" in headline_sentences(one_trial)[0]


def test_headline_fix_clause(summary):
    ups = [u.model_dump(mode="json") for u in summary.uplifts]
    none = _variant(summary, uplifts=[])
    assert "no fix runs are in this summary." in headline_sentences(none)[1]
    noise = _variant(summary, uplifts=[{**u, "separated": False} for u in ups])
    assert "no fix moved accuracy above noise" in headline_sentences(noise)[1]
    hurts = _variant(
        summary, uplifts=[u for u in ups if u["separated"] and u["delta_points"]["point"] < 0]
    )
    s2 = headline_sentences(hurts)[1]
    assert "no fix raised accuracy above noise" in s2 and "−21.7 pts" in s2
    for s in (none, noise, hurts):
        prose = _prose(headline_block(s))
        assert len(_sentences(prose)) == 3 and decimals_problems(prose) == []


def test_update_readme_replaces_between_markers_idempotently(summary, tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"# T\n\nintro\n\n{START}\nplaceholder\n{END}\n\nrest\n", encoding="utf-8")
    assert update_readme(readme, summary) is True
    first = readme.read_text(encoding="utf-8")
    assert "placeholder" not in first and first.startswith("# T\n\nintro\n\n" + START)
    assert first.endswith(END + "\n\nrest\n")
    assert update_readme(readme, summary) is False
    assert readme.read_text(encoding="utf-8") == first
    assert first.count(START) == first.count(END) == 1


@pytest.mark.parametrize(
    "text",
    ["no markers", f"{START} only", f"{END} {START}", f"{START}{START}{END}", f"{START}{END}{END}"],
)
def test_splice_refuses_bad_markers(text):
    with pytest.raises(ReadmeError):
        splice(text, "x")


def _with_strict(summary: RunSummary, drop: float = 0.1) -> RunSummary:
    """The example with an OWNER D-81 strict score below every headline accuracy."""

    def lower(iv: dict) -> dict:
        return {k: max(0.0, v - drop) for k, v in iv.items()}

    backends = []
    for b in summary.backends:
        d = b.model_dump(mode="json")
        d["accuracy_strict"] = lower(d["accuracy"])
        d["by_category"] = [
            {**c, "accuracy_strict": lower(c["accuracy"])} for c in d["by_category"]
        ]
        backends.append(d)
    return _variant(summary, backends=backends)


def test_strict_score_sits_beside_every_headline_accuracy(summary):
    """OWNER D-81: wherever accuracy is reported, the strict score is beside it."""
    strict = _with_strict(summary)
    s2 = headline_sentences(strict)[1]
    assert "63.9% (95% CI 52.2–75.0%; strict 53.9%) for Claude (MCP)" in s2
    assert s2.count("; strict ") == len([b for b in strict.backends if not b.fixes])
    assert "pre-registered" in headline_sentences(strict)[2]
    assert "strict" not in headline_sentences(summary)[1]  # a pre-D-81 summary has none
    table = results_block(strict)
    assert "| Strict |" in table and "| 53.9% |" in table
    assert "| Strict |" not in results_block(summary)
    svg = headline_svg(strict)
    assert 'class="h0"' in svg and "strict 53.9%" in svg
    assert 'class="h0"' not in headline_svg(summary)
    assert "ring: strict score" in categories_svg(strict)
    up = uplift_svg(strict)
    assert up is not None and "hollow" not in up and "headline score" in up


def test_all_fixes_is_never_a_readme_figure(summary):
    """Tech Spec §7: the `all` run is recorded but never a headline figure."""
    claude = next(b for b in summary.backends if b.backend == "claude_mcp" and not b.fixes)
    all_cfg = {
        **claude.model_dump(mode="json"),
        "fixes": ["descriptions", "examples", "self_check"],
        "accuracy": {"point": 0.991, "lo": 0.98, "hi": 1.0},
    }
    best_all = _variant(
        summary,
        backends=[*(b.model_dump(mode="json") for b in summary.backends), all_cfg],
        uplifts=[
            *(u.model_dump(mode="json") for u in summary.uplifts),
            {
                "backend": "claude_mcp",
                "fix": "all",
                "delta_points": {"point": 60.0, "lo": 50.0, "hi": 70.0},
                "p_value": 0.0001,
                "separated": True,
            },
        ],
    )
    assert "all fixes" not in headline_sentences(best_all)[1]
    table = results_block(best_all)
    assert "+60.0 pts" not in table and "+ all fixes |" not in table and "99.1%" not in table
    assert "not a headline figure" in table
    for svg in writeup_figures(best_all).values():
        assert "+ all fixes" not in svg and "+60.0 pts" not in svg and "99.1%" not in svg


def test_results_block_states_its_caveats(summary):
    block = results_block(
        summary, models={"claude_mcp": "m-claude", "gpt_mcp": "m-gpt"}, snowflake_region=None
    )
    assert block.startswith(RESULTS_START + "\n") and block.endswith("\n" + RESULTS_END)
    assert "Claude (MCP) is `m-claude` and GPT (MCP) is `m-gpt`" in block
    assert "(estimated)" in block and "costs are estimates" in block
    assert "¹ Snowflake account region not recorded" in block
    thai = results_block(summary, snowflake_region="AWS_AP_SOUTHEAST_7")
    assert "Thailand" in thai and "Cortex latency includes that distance" in thai
    assert decimals_problems(block.replace("¹", "")) == [] or all(
        d.startswith("0.") for d in decimals_problems(block)
    )  # only money, which prints its own significant digits


def test_render_readme_refuses_a_half_present_results_pair(summary):
    text = f"{START}\n{END}\n{RESULTS_START}\n"
    with pytest.raises(ReadmeError):
        render_readme(text, summary)
    only_headline = render_readme(f"{START}\n{END}\n", summary)
    assert RESULTS_START not in only_headline


# REL-04 and the README itself: generated from the committed published run

RECORD = ROOT / "docs" / "results"
MEASURE = ROOT / "docs" / "measurements"


@pytest.fixture(scope="module")
def published() -> tuple[RunSummary, dict]:
    summary = RunSummary.model_validate_json((RECORD / "summary.json").read_text("utf-8"))
    return summary, json.loads((RECORD / "run.json").read_text(encoding="utf-8"))


def test_repo_readme_regenerates_from_the_committed_summary(published):
    """Every number in the README comes from docs/results/summary.json (no hand edits)."""
    from agentic_analytics.run.runner import stamped_region

    summary, manifest = published
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for marker in (START, END, RESULTS_START, RESULTS_END):
        assert text.count(marker) == 1, marker
    again = render_readme(
        text,
        summary,
        models=manifest["config"]["models"],
        snowflake_region=stamped_region(manifest),
    )
    assert again == text


def test_repo_figures_regenerate_from_the_committed_summary(published):
    from agentic_analytics.packs import load_pack

    summary, _ = published
    credit = load_pack("uk_price_paid").manifest.attribution
    figs = writeup_figures(summary, credit)
    for name, svg in figs.items():
        assert (ROOT / "docs" / "assets" / name).read_text(encoding="utf-8") == svg, name


def _near(a: dict, b: dict) -> bool:
    return all(abs(a[k] - b[k]) < 5e-5 for k in ("point", "lo", "hi"))


def test_committed_summary_equals_the_measurement_records(published):
    """The published summary (v0.1.1) is the fix-run rescore on EvalKit 0.4.0 with the
    baseline's metered credits: its figures equal baseline-2026-11-17.json and
    fixruns-evalkit-0.4-2026-11-18.json to the records' rounding (OWNER D-118, D-119)."""
    summary, manifest = published
    base = json.loads((MEASURE / "baseline-2026-11-17.json").read_text(encoding="utf-8"))
    fix = json.loads((MEASURE / "fixruns-evalkit-0.4-2026-11-18.json").read_text(encoding="utf-8"))
    assert summary.run_id == fix["rescored_run_id"] and summary.cost_status == "reconciled"
    assert summary.gold_check_id == base["gold_check_id"] == fix["gold_check_id"]
    assert summary.as_of.isoformat() == base["as_of"]
    assert manifest["fix_hash"] == base["fix_hash"] == fix["fix_hash"]
    assert manifest["rescored_offline"]["evalkit"] == "0.4.0"
    assert dict(summary.total_credits) == base["reconciled"]["total_credits"]
    assert len(fix["per_config"]) == len(summary.backends)
    for b in summary.backends:
        key = f"{b.backend}/{'+'.join(b.fixes) or 'baseline'}"
        rec = fix["per_config"][key]
        assert b.accuracy_strict is not None
        assert _near(b.accuracy.model_dump(), rec["accuracy"]), key
        assert _near(b.accuracy_strict.model_dump(), rec["accuracy_strict"]), key
        assert _near(b.clarification_rate.model_dump(), rec["clarification_rate"]), key
        assert b.latency_p95_ms == rec["latency_p95_ms"], key
        assert dict(b.equivalence_passes) == rec["equivalence_passes"], key
        assert abs((b.cost_per_correct_usd or 0) - rec["cost_per_correct_usd"]) < 5e-7, key
        if not b.fixes:
            brec = base["per_backend"][b.backend]
            assert _near(b.accuracy.model_dump(), brec["accuracy"]), key
            assert _near(b.accuracy_strict.model_dump(), brec["accuracy_strict"]), key
            assert abs((b.cost_per_correct_usd or 0) - brec["cost_per_correct_usd"]) < 5e-7, key
    ups = {(u["backend"], u["fix"]): u for u in fix["uplifts"]}
    assert len(ups) == len(summary.uplifts)
    for u in summary.uplifts:
        rec = ups[(u.backend, u.fix)]
        assert _near(u.delta_points.model_dump(), rec["delta_points"])
        assert u.separated == rec["separated"]


def test_v0_1_1_moves_only_what_owner_d118_and_d119_name():
    """Against v0.1.0's record (fixruns-2026-11-17.json): the baseline headline and every
    accuracy but GPT + descriptions are unchanged; GPT + descriptions loses one trial of q-020
    (OWNER D-118); clarification points never move, two intervals widen (D-119)."""
    old = json.loads((MEASURE / "fixruns-2026-11-17.json").read_text(encoding="utf-8"))
    new = json.loads((MEASURE / "fixruns-evalkit-0.4-2026-11-18.json").read_text(encoding="utf-8"))
    assert set(old["per_config"]) == set(new["per_config"])
    moved = {
        k
        for k in new["per_config"]
        if new["per_config"][k]["accuracy"] != old["per_config"][k]["accuracy"]
    }
    assert moved == {"gpt_mcp/descriptions"}
    for k, rec in new["per_config"].items():
        was = old["per_config"][k]
        assert rec["accuracy_strict"] == was["accuracy_strict"], k
        assert rec["clarification_rate"]["point"] == was["clarification_rate"]["point"], k
        assert rec["false_clarification_rate"] == was["false_clarification_rate"], k
        if k.endswith("/baseline"):
            assert rec["accuracy"] == was["accuracy"], k  # the headline does not move
    g, w = new["per_config"]["gpt_mcp/descriptions"], old["per_config"]["gpt_mcp/descriptions"]
    assert abs(w["accuracy"]["point"] - g["accuracy"]["point"] - 1 / 300) < 1e-4
    assert w["equivalence_passes"]["empty_check"] - g["equivalence_passes"]["empty_check"] == 1
    ups = {(u["backend"], u["fix"]): u for u in new["uplifts"]}
    was_ups = {(u["backend"], u["fix"]): u for u in old["uplifts"]}
    assert {k for k in ups if ups[k] != was_ups[k]} == {("gpt_mcp", "descriptions")}
    assert ups[("gpt_mcp", "descriptions")]["delta_points"]["point"] == -5.0
    widened = {
        k
        for k in new["per_config"]
        if new["per_config"][k]["clarification_rate"] != old["per_config"][k]["clarification_rate"]
    }
    assert widened == {"claude_mcp/baseline", "gpt_mcp/descriptions"}
    (c,) = new["changed_verdicts"]
    assert (c["question_id"], c["backend"], c["fixes"], c["trial"]) == (
        "q-020",
        "gpt_mcp",
        "descriptions",
        1,
    )


def test_readme_headline_shows_the_recorded_baseline(published):
    """The case study's headline, read from the README: tolerant beside strict for each."""
    prose = _prose((ROOT / "README.md").read_text("utf-8").split(START, 1)[1].split(END, 1)[0])
    for want in (
        "76.0% (95% CI 67.7–84.0%; strict 76.0%) for Claude (MCP)",
        "97.0% (95% CI 93.0–100.0%; strict 92.0%) for GPT (MCP)",
        "85.0% (95% CI 78.0–92.0%; strict 62.0%) for Cortex Analyst",
        "examples on Claude (MCP), +15.7 pts",
    ):
        assert want in prose, want
    assert decimals_problems(prose) == []


REPO_LINK = re.compile(
    r"https://github\.com/biplob819/agentic-analytics/(?:blob|tree)/main/([^)\s\"#]+)"
)
RAW_PREFIX = "https://raw.githubusercontent.com/biplob819/agentic-analytics/main/"
IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]*)")


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag, re.I)
    return m.group(1) if m else None


def readme_images(readme: str) -> list[tuple[str | None, str | None]]:
    """(repository path or None, alt text or None) for every <img> tag and Markdown image; a
    source that is neither this repository's raw URL nor a relative path gives None."""
    out = []
    srcs = [(_attr(t, "src"), _attr(t, "alt")) for t in IMG_TAG.findall(readme)]
    srcs += [(src, alt) for alt, src in MD_IMAGE.findall(readme)]
    for src, alt in srcs:
        if src and src.startswith(RAW_PREFIX):
            path = src.removeprefix(RAW_PREFIX)
        elif src and not re.match(r"^[a-z]+:", src) and not src.startswith("/"):
            path = src
        else:
            path = None
        out.append((path, alt))
    return out


def image_problems(readme: str) -> list[str]:
    """Why an image would ship broken: unparsed, outside docs/assets, missing, too large, a PNG
    carrying text chunks, or alt text missing or too short to describe it."""
    images = readme_images(readme)
    problems = []
    if len(images) != len(re.findall(r"<img\b", readme, re.I)) + readme.count("!["):
        problems.append("an image tag was not parsed")
    for path, alt in images:
        if path is None or not path.startswith("docs/assets/"):
            problems.append(f"{path}: not a docs/assets file of this repository")
            continue
        f = ROOT / path
        if not f.is_file():
            problems.append(f"{path}: missing")
            continue
        if alt is None or len(alt.strip()) < 40:
            problems.append(f"{path}: alt text missing or too short")
        if f.suffix == ".png":
            data = f.read_bytes()
            if len(data) > PNG_MAX_BYTES:
                problems.append(f"{path}: {len(data)} bytes")
            if {"tEXt", "iTXt", "zTXt", "eXIf"} & set(_png_chunks(data)):
                problems.append(f"{path}: PNG text chunks")
    return problems


def test_readme_says_the_public_dashboard_has_no_traces():
    """OWNER D-111: the Pages dashboard is built from the committed record alone."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "no per-answer records or traces" in text
    assert "builds the dashboard from the committed published-run record" in text


def test_readme_links_resolve_and_name_every_document():
    """REL-03 and OWNER D-60: the decision log, data card and case study (the "how I built it"
    note) are linked, with the other key documents, the B3.3 guides and community files
    (OSS-04, OSS-05, OSS-06); every repository link resolves."""
    from urllib.parse import unquote

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = [unquote(t) for t in REPO_LINK.findall(text)]
    for t in targets:
        assert (ROOT / t).exists(), t
    for need in (
        "docs/decisions.md",
        "DATA_CARD.md",
        "docs/case-study.md",
        "docs/lessons.md",
        "Agentic Analytics PRD.md",
        "Agentic Analytics Tech Spec.md",
        "docs/results/summary.json",
        "docs/measurements/baseline-2026-11-17.json",
        "docs/measurements/fixruns-2026-11-17.json",
        "docs/measurements/fixruns-evalkit-0.4-2026-11-18.json",
        "LICENSE",
        # B3.3: the guides (OSS-04) and the community and privacy files (OSS-05, OSS-06)
        "docs/quickstart.md",
        "docs/reproduce-the-uk-benchmark.md",
        "docs/writing-a-dataset-pack.md",
        "docs/adding-questions.md",
        "docs/cost.md",
        "docs/versioning.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "PRIVACY.md",
        "CODE_OF_CONDUCT.md",
        "docs/walkthrough.md",
    ):
        assert need in targets, need
    for url in (
        "https://github.com/biplob819/evalkit",
        "https://pypi.org/project/evalkit-harness/",
        "https://www.gov.uk/government/statistical-data-sets/price-paid-data-downloads",
        "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    ):
        assert url in text, url


PNG_MAX_BYTES = 200_000


def _png_chunks(data: bytes) -> list[str]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    out, i = [], 8
    while i < len(data):
        n = struct.unpack(">I", data[i : i + 4])[0]
        out.append(data[i + 4 : i + 8].decode("ascii"))
        i += 12 + n
    return out


def test_readme_images_exist_have_alt_text_and_stay_small():
    """OWNER D-60 screenshots: each image is in docs/assets, has alt text, is small, and a PNG
    carries pixels only (no text chunks that could hold a path or a name)."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert image_problems(text) == []
    names = {Path(p).name for p, _ in readme_images(text) if p}
    assert {
        "headline.svg",
        "uplift.svg",
        "categories.svg",
        "dashboard.png",
        "demo.png",
        "demo-passcode.png",
        "cli.png",
        "cli-doctor.png",
        "walkthrough.gif",  # embedded after the headline block, linked to its shot list
    } <= names
    gif = "docs/assets/walkthrough.gif)](https://github.com/biplob819/agentic-analytics/blob/main/docs/walkthrough.md)"
    assert gif in text and text.index(END) < text.index(gif)  # outside the generated block


PASSCODE_ALT = re.compile(r'(docs/assets/demo-passcode\.png") alt="[^"]*"')


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: PASSCODE_ALT.sub(r"\1", t),  # alt dropped
        lambda t: PASSCODE_ALT.sub(r'\1 alt=""', t),  # alt emptied
        lambda t: t + f"\n![]({RAW_PREFIX}docs/assets/nothere.png)\n",
        lambda t: t + "\n![A long enough description of a missing picture](docs/assets/no.png)\n",
        lambda t: (
            t + '\n<img alt="A long enough description of an external picture here" '
            'src="https://example.org/x.png">\n'
        ),
        lambda t: t + "\n<img src=docs/assets/demo.png>\n",  # unquoted: not parsed
    ],
    ids=["alt-dropped", "alt-empty", "md-missing", "md-missing-alt-ok", "external", "unparsed"],
)
def test_image_check_bites(mutate):
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert PASSCODE_ALT.search(text)
    assert image_problems(mutate(text))


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def broken_anchors(readme: str) -> list[str]:
    slugs = {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", readme, re.M)}
    return [a for a in re.findall(r"\]\(#([^)]+)\)", readme) if a not in slugs]


def test_readme_in_page_anchors_resolve():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.findall(r"\]\(#", text)
    assert broken_anchors(text) == []
    assert broken_anchors(text + "\n[x](#no-such-section)\n") == ["no-such-section"]


LOCAL = re.compile(r"/Users/|/private/|/home/[a-z]|[A-Za-z]:\\\\|[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b")


def test_published_files_hold_no_local_path_or_email():
    files = [ROOT / "README.md", *(ROOT / "docs" / "results").iterdir()]
    files += [f for f in (ROOT / "docs" / "assets").iterdir() if f.suffix == ".svg"]
    for f in files:
        assert not LOCAL.findall(f.read_text(encoding="utf-8")), f.name
    assert LOCAL.findall("see /Users/someone/x and a@b.co")  # the check bites


_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|"
    r"December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)"
)
SCHEDULE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)days?\b|"
    rf"\b{_MONTH}\.? \d{{1,2}}\b|\b\d{{1,2}}(?:st|nd|rd|th)? {_MONTH}\b|"
    rf"\b(?:in|by|on|from|until|till|since|before|after|during|early|late|mid|end of) {_MONTH}\b|"
    r"\b(?:tomorrow|tonight|today|yesterday|next (?:week|month|quarter|year)|this (?:week|month)|"
    r"by the end of|every (?:day|night|week|month|quarter|year|morning)|"
    r"(?:bi)?(?:weekly|monthly|daily|nightly|quarterly|annually|yearly))\b",
    re.I,
)


def dated_prose(readme: str) -> list[str]:
    """OWNER D-20: dates or schedule words in the README's own prose (generated blocks and link
    targets, such as a dated record's file name, are not prose)."""
    outside = re.sub(re.escape(START) + r".*?" + re.escape(END), "", readme, flags=re.S)
    outside = re.sub(
        re.escape(RESULTS_START) + r".*?" + re.escape(RESULTS_END), "", outside, flags=re.S
    )
    outside = re.sub(r"\]\([^)]*\)|(?:src|href)=\"[^\"]*\"", "", outside)
    return [m.group(0) for m in SCHEDULE.finditer(outside)]


def test_readme_has_no_dates_or_schedule():
    assert dated_prose((ROOT / "README.md").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "planted",
    [
        "Released on 2026-12-01.",
        "Ships next week.",
        "Live by Friday.",
        "Due 3 December.",
        "Lands in December.",
        "Runs on Mondays.",
        "Runs every week.",
        "A weekly smoke run.",
        "Updated monthly.",
        "Checked daily.",
        "Due Dec 3.",
    ],
)
def test_dated_prose_check_bites(planted):
    assert dated_prose(f"# T\n{planted}\n")
    assert dated_prose("[rec](docs/x-2026-11-17.json)") == []


ALLOWED_PERCENTS = {"95%", "99%"}  # the interval level and its simulated coverage (D-42)


def hand_typed_numbers(readme: str) -> list[str]:
    """Result-like numbers outside the generated block: percentages other than the method's
    95% and 99%, signed points, decimal accuracies and costs per correct answer."""
    outside = re.sub(re.escape(START) + r".*?" + re.escape(END), "", readme, flags=re.S)
    outside = re.sub(
        re.escape(RESULTS_START) + r".*?" + re.escape(RESULTS_END), "", outside, flags=re.S
    )
    found = [m for m in re.findall(r"\d+(?:\.\d+)?\s?%", outside) if m not in ALLOWED_PERCENTS]
    found += re.findall(r"[+−-]\d+(?:\.\d+)?\s?(?:pts|points|pp)\b", outside)
    found += re.findall(r"\b0\.\d{2,}\b", outside)
    found += re.findall(r"\$\d+\.\d{2,}", outside)
    found += re.findall(r"\b\d+(?:\.\d+)?\s?(?:percent|per cent|pct)\b", outside, re.I)
    found += re.findall(r"\b\d+(?:\.\d+)?\s?(?:percentage points?|points?|pp)\b", outside, re.I)
    found += re.findall(r"\b\d+ (?:of|out of|in) (?:the )?\d+\b", outside, re.I)
    found += re.findall(r"\b[Oo]f (?:the )?\d+\b[^.]{0,60}?\b\d+ (?:were|was|are|is)\b", outside)
    return found


def test_readme_has_no_hand_typed_result_numbers():
    assert hand_typed_numbers((ROOT / "README.md").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "planted",
    [
        "Claude scores 63.9% here.",
        "descriptions add +17.2 pts",
        "accuracy 0.64",
        "$0.063",
        "Claude scores 74 percent.",
        "GPT is 92 per cent right.",
        "It answered 74 of 100.",
        "Right on 74 out of 100 questions.",
        "Of 30 failures, 20 were missed clarifications.",
        "Examples add 15.7 points.",
    ],
)
def test_hand_typed_number_check_bites(planted):
    readme = f"# T\n{START}\n63.9% inside is fine\n{END}\n{planted}\n"
    assert hand_typed_numbers(readme)
    assert hand_typed_numbers(f"# T\n{START}\n{planted}\n{END}\n") == []


def test_figures_are_standalone_svg_with_caveats(summary):
    figs = writeup_figures(summary, CREDIT)
    assert set(figs) == {"headline.svg", "categories.svg", "uplift.svg"}
    for name, svg in figs.items():
        root = ET.fromstring(svg)
        ns = "{http://www.w3.org/2000/svg}"
        assert root.tag == f"{ns}svg", name
        assert root.find(f"{ns}title") is not None and root.find(f"{ns}desc") is not None
        assert "prefers-color-scheme:dark" in svg
        for stamp in (
            "SYNTHETIC EXAMPLE",
            "Cross-engine gold",
            "no LLM judge",
            "conservative (about 99% coverage",
            "Cost pending",
            "Open Government Licence v3.0.",
        ):
            assert stamp in svg, (name, stamp)
        assert "http" not in svg.replace("http://www.w3.org/2000/svg", "")  # loads nothing
    assert "Cortex Analyst baseline: 74.4% (95% CI 63.9–84.4%)" in figs["headline.svg"]
    assert "▲ above noise, helps" in figs["uplift.svg"]
    assert "▼ above noise, hurts" in figs["uplift.svg"]


def test_figures_follow_the_summary_and_are_deterministic(summary):
    assert headline_svg(summary, CREDIT) == headline_svg(summary, CREDIT)
    real = _variant(summary, run_id="r-real", gold_mode="single_engine", cost_status="reconciled")
    svg = categories_svg(real)
    assert "SYNTHETIC" not in svg and "Single-engine gold" in svg and "Cost pending" not in svg
    assert uplift_svg(_variant(summary, uplifts=[])) is None
    assert set(writeup_figures(_variant(summary, uplifts=[]))) == {"headline.svg", "categories.svg"}
    changed = _variant(
        summary,
        backends=[
            {**b.model_dump(mode="json"), "accuracy": {"point": 0.5, "lo": 0.4, "hi": 0.6}}
            for b in summary.backends
        ],
    )
    assert "50.0% (95% CI 40.0–60.0%)" in headline_svg(changed)


def test_a_summary_of_only_the_all_fixes_run_names_no_figure(summary):
    """§7 at the edge: with no baseline and no single-fix configuration, nothing is shown as a
    headline figure, and the sentences say so."""
    claude = next(b for b in summary.backends if b.backend == "claude_mcp" and not b.fixes)
    only_all = _variant(
        summary,
        backends=[
            {
                **claude.model_dump(mode="json"),
                "fixes": ["descriptions", "examples", "self_check"],
                "accuracy": {"point": 0.991, "lo": 0.98, "hi": 1.0},
            }
        ],
        uplifts=[],
    )
    s1, s2, _ = headline_sentences(only_all)
    assert "No baseline or single-fix configuration is in this summary" in s2
    assert "99.1%" not in s1 + s2 and "all fixes" not in s2
    assert "No baseline or single-fix configuration" in results_block(only_all)
    for svg in writeup_figures(only_all).values():
        assert "99.1%" not in svg and "+ all fixes" not in svg
