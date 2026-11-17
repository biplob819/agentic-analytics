"""QST-04 and QST-05 records: the dual-authorship report and the owner review sitting.

Both are live records in `docs/measurements/`; these tests hold them to the shipped bank so a
question added, dropped or reworded later cannot leave them silently stale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentic_analytics.questions import load_probes, load_questions

ROOT = Path(__file__).resolve().parents[2]
DUAL = ROOT / "docs/measurements/dual-authorship-2026-11-16.json"
REVIEW = ROOT / "docs/measurements/question-review.json"
DECISIONS = ROOT / "docs/decisions.md"
VERDICTS = {"ok", "change", "drop"}


@pytest.fixture(scope="module")
def bank():
    return {q.id: q for q in load_questions("uk_price_paid")}


@pytest.fixture(scope="module")
def probes():
    return {p.id: p for p in load_probes("uk_price_paid")}


@pytest.fixture(scope="module")
def review() -> dict:
    return json.loads(REVIEW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dual() -> dict:
    return json.loads(DUAL.read_text(encoding="utf-8"))


def test_dual_authorship_covers_every_answer_question_once(bank, dual) -> None:
    answer = {qid for qid, q in bank.items() if q.expected == "answer"}
    ids = [r["id"] for r in dual["results"]]
    assert len(ids) == len(set(ids)) == dual["answer_questions"] == len(answer)
    assert set(ids) == answer
    agree = [r for r in dual["results"] if r["agree"]]
    assert dual["agree"] == len(agree) and dual["agree"] + dual["disagree"] == len(ids)
    assert all(r["gpt_sql"].strip() for r in dual["results"])  # a real second version each


def test_every_disagreement_is_triaged_and_noted_for_the_owner(dual, review) -> None:
    disagree = {r["id"] for r in dual["results"] if not r["agree"]}
    triage = dual["triage_summary"]
    triaged = [qid for bucket in triage.values() for qid in bucket]
    assert len(triaged) == len(set(triaged))  # each disagreement in exactly one bucket
    # every disagreement is triaged; a bucket may also hold an agreeing question with a note
    assert disagree <= set(triaged)
    assert dual["disagree_on_substance"] == len(triage["gold_wrong"]) + len(
        triage["second_author_wrong"]
    )
    notes = {e["id"]: e for e in review["entries"]}
    for qid in disagree:
        assert "QST-04" in json.dumps(notes[qid]), qid


def test_owner_review_covers_every_item_with_one_verdict(bank, probes, review) -> None:
    entries = {e["id"]: e for e in review["entries"]}
    assert len(entries) == len(review["entries"])
    assert set(entries) == set(bank) | set(probes)
    assert all(e["owner_verdict"] in VERDICTS for e in entries.values())
    summary = review["owner_review"]
    assert summary["items"] == len(entries)
    assert summary["ok"] == sum(e["owner_verdict"] == "ok" for e in entries.values())
    assert summary["change"] == sorted(
        i for i, e in entries.items() if e["owner_verdict"] == "change"
    )
    assert summary["drop"] == sorted(i for i, e in entries.items() if e["owner_verdict"] == "drop")


def test_owner_rulings_are_applied_and_logged(bank, review) -> None:
    assert all(q.owner_reviewed for q in bank.values())
    decisions = DECISIONS.read_text(encoding="utf-8")
    ref = review["owner_review"]["decision"]
    for d in re.findall(r"OWNER D-\d+", ref):
        assert f"| {d} |" in decisions, d
    entries = {e["id"]: e for e in review["entries"]}
    for qid, e in entries.items():
        if e["owner_verdict"] == "drop":
            assert qid not in bank, qid
        if e["owner_verdict"] == "change":
            assert e["owner_note"] and e["text_after_review"] == bank[qid].text, qid
            assert e["text"] != bank[qid].text, qid
            assert qid in decisions, qid
        if qid in bank:  # the record names the shipped text, before or after review
            assert bank[qid].text in (e["text"], e.get("text_after_review")), qid


def test_review_record_shows_the_shipped_gold(bank, probes, review) -> None:
    """The owner reviewed the gold the bank ships: same SQL, tolerance and shape fields."""
    for e in review["entries"]:
        item = bank.get(e["id"]) or probes[e["id"]]
        assert e["gold_sql"] == item.gold_sql, e["id"]
        if e["id"] not in bank:
            continue
        q = bank[e["id"]]
        assert e["top_n"] == q.top_n and e["ordered"] == q.ordered, e["id"]
        assert e["expects_empty"] == q.expects_empty, e["id"]
        assert e["tolerance"] == q.tolerance.model_dump(mode="json"), e["id"]
        shipped = [dict(i) for i in q.interpretations]
        assert [i["sql"] for i in e.get("interpretations", [])] == shipped, e["id"]


def test_dual_authorship_spend_is_in_the_ledger(dual) -> None:
    ledger = ROOT / "docs/measurements/spend-ledger.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    hits = [r for r in rows if r["run_id"] == "q2.1-qst04-dual-2026-11-16"]
    assert len(hits) == 1 and abs(hits[0]["spent_usd"] - dual["spend_usd"]) < 1e-9
