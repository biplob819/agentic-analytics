"""REL-05: the results dashboard is deployed on GitHub Pages from the hand-run pages workflow,
from the committed published run, with the secret scan in the build job (OWNER D-107, D-111)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/measurements/pages-deploy-2026-11-18.json"


def record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_the_dashboard_was_deployed_by_the_pages_workflow() -> None:
    r = record()
    assert r["criterion"] == "REL-05" and r["workflow"] == "pages.yml"
    assert r["jobs"] == {"build": "success", "deploy": "success"}
    assert r["http_status"] == 200 and r["url"].startswith("https://biplob819.github.io/")
    assert str(r["workflow_run_id"]) in r["workflow_run_url"]


def test_the_deploy_is_built_from_the_committed_published_record() -> None:
    r = record()
    assert (ROOT / r["source_record"]).is_file()
    summary = json.loads((ROOT / r["source_record"]).read_text(encoding="utf-8"))
    assert summary["run_id"] == r["published_run"]
    steps = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"))
    build = " ".join(str(s.get("run", "")) for s in steps["jobs"]["build"]["steps"])
    assert "publish_results.py --from-record" in build and "secret_scan" in build


def test_the_published_page_carries_its_caveats_and_no_traces() -> None:
    c = record()["checks"]
    assert c["licence_credit_present"] and c["region_caveat_present"]
    assert c["per_answer_traces_absent"] and c["secret_scan_in_workflow"]
    assert c["no_keys_emails_or_local_paths"]
    assert c["headline_accuracies_present"] == ["76.0%", "97.0%", "85.0%"]
