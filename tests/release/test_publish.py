"""B3.2: scripts/publish_results.py renders a finished run into site/, copies its summary and
stamp into docs/results/, draws the figures and regenerates the README's generated blocks
(REL-01, REL-02, REL-04). Tests publish into a scratch copy."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import publish_results
from agentic_analytics.report.readme import END, RESULTS_END, RESULTS_START, START

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copyfile(ROOT / "README.md", root / "README.md")
    return root


_SNAPSHOTS = {"example-agent-a": "claude-t1", "example-agent-b": "gpt-t1", "example-cortex": "c-t1"}


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _unsynthesise(run: Path) -> None:
    """Make the example look like a real run: fresh fingerprint, real snapshot names and
    hashes. Without this a renamed example is (rightly) refused as SYNTHETIC."""
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    swaps = {"a" * 64: _digest("record")}
    for key in ("config_hash", "snapshot_sha256", "gold_check_id"):
        swaps[summary[key]] = _digest(key)
    for name in ("summary.json", "records.jsonl"):
        text = (run / name).read_text(encoding="utf-8")
        for old, new in swaps.items():
            text = text.replace(old, new)
        for old, new in _SNAPSHOTS.items():
            text = text.replace(f'"{old}"', f'"{new}"')
        (run / name).write_text(text, encoding="utf-8")


def _run_dir(tmp_path: Path, *, real: bool = True, **update) -> Path:
    run = publish_results._example_run(tmp_path / "runs")
    if real:
        _unsynthesise(run)
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    summary.update(update)
    target = tmp_path / "runs" / summary["run_id"]
    if target != run:
        run.rename(target)
    (target / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return target


def test_publish_example_into_a_scratch_repo(repo, capsys):
    assert publish_results.main(["--example", "--root", str(repo)]) == 0
    assert "README headline updated" in capsys.readouterr().out
    site = repo / "site"
    html = (site / "index.html").read_text(encoding="utf-8")
    assert "SYNTHETIC EXAMPLE" in html
    assert (site / "summary.vl.json").is_file() and (site / "summary.json").is_file()
    assert sorted(p.name for p in (site / "traces").glob("*.json"))
    for name in ("headline.svg", "categories.svg", "uplift.svg"):
        assert "SYNTHETIC EXAMPLE" in (repo / "docs" / "assets" / name).read_text(encoding="utf-8")
    readme = (repo / "README.md").read_text(encoding="utf-8")
    inside = readme.split(START, 1)[1].split(END, 1)[0]
    assert "No results yet" not in inside and "In the bundled SYNTHETIC example" in inside
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and "site" not in p.parts}
    assert publish_results.main(["--example", "--root", str(repo)]) == 0
    assert "README headline unchanged" in capsys.readouterr().out
    after = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file() and "site" not in p.parts}
    assert after == before


def test_publish_a_finished_run(repo, tmp_path, capsys):
    run = _run_dir(tmp_path, run_id="r-20261201-test", gold_mode="single_engine")
    (repo / "site" / "traces").mkdir(parents=True)
    (repo / "site" / "traces" / "stale.json").write_text("{}", encoding="utf-8")
    assert publish_results.main([str(run), "--root", str(repo)]) == 0
    html = (repo / "site" / "index.html").read_text(encoding="utf-8")
    assert "SYNTHETIC EXAMPLE" not in html and "single-engine gold" in html
    assert not (repo / "site" / "traces" / "stale.json").exists()
    readme = (repo / "README.md").read_text(encoding="utf-8")
    inside = readme.split(START, 1)[1].split(END, 1)[0]
    assert "SYNTHETIC" not in inside and "(single-engine gold)" in inside
    assert "Open Government Licence v3.0." in (repo / "docs/assets/headline.svg").read_text(
        encoding="utf-8"
    )


def test_publish_removes_a_stale_uplift_figure(repo, tmp_path):
    (repo / "docs" / "assets").mkdir(parents=True)
    (repo / "docs" / "assets" / "uplift.svg").write_text("<svg/>", encoding="utf-8")
    run = _run_dir(tmp_path, run_id="r-no-fixes", uplifts=[])
    assert publish_results.main([str(run), "--root", str(repo)]) == 0
    assert not (repo / "docs" / "assets" / "uplift.svg").exists()


def test_publish_refuses_the_example_and_unfinished_runs(repo, tmp_path, capsys):
    example = _run_dir(tmp_path, real=False)
    assert publish_results.main([str(example), "--root", str(repo)]) == 2
    assert "SYNTHETIC example" in capsys.readouterr().err
    unfinished = _run_dir(tmp_path, run_id="r-unfinished", finished_at=None)
    assert publish_results.main([str(unfinished), "--root", str(repo)]) == 2
    assert "has not finished" in capsys.readouterr().err
    assert publish_results.main([str(tmp_path / "missing"), "--root", str(repo)]) == 2
    assert not (repo / "site").exists()
    assert (repo / "README.md").read_bytes() == (ROOT / "README.md").read_bytes()


def test_example_never_targets_this_repository(tmp_path, monkeypatch):
    """Run against a copy posing as the repository, so a regressed guard writes there, not
    into the real tree; nothing is written into the copy either."""
    fake = tmp_path / "fake-repo"
    fake.mkdir()
    shutil.copyfile(ROOT / "README.md", fake / "README.md")
    monkeypatch.setattr(publish_results, "ROOT", fake)
    before = sorted(p.name for p in fake.rglob("*"))
    with pytest.raises(SystemExit) as exc:
        publish_results.main(["--example"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        publish_results.main(["--example", "--root", str(fake)])
    with pytest.raises(SystemExit):
        publish_results.main([])
    assert sorted(p.name for p in fake.rglob("*")) == before
    assert (fake / "README.md").read_bytes() == (ROOT / "README.md").read_bytes()


def _renamed_example(tmp_path: Path, name: str) -> Path:
    """The example copied under a new run id, everything else untouched."""
    return _run_dir(tmp_path / name, real=False, run_id=f"r-20261201-{name}")


def _fix_one(run: Path, *, summary_fp: bool, snapshots: bool, hashes: bool) -> None:
    """Clear all but the chosen synthetic giveaways, to test each on its own."""
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    swaps: dict[str, str] = {}
    if not hashes:
        swaps["a" * 64] = _digest("record")
    if not summary_fp:
        for key in ("config_hash", "snapshot_sha256", "gold_check_id"):
            swaps[summary[key]] = _digest(key)
    for name in ("summary.json", "records.jsonl"):
        text = (run / name).read_text(encoding="utf-8")
        for old, new in swaps.items():
            text = text.replace(old, new)
        if not snapshots:
            for old, new in _SNAPSHOTS.items():
                text = text.replace(f'"{old}"', f'"{new}"')
        (run / name).write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "giveaway",
    [
        {"summary_fp": True, "snapshots": False, "hashes": False},
        {"summary_fp": False, "snapshots": True, "hashes": False},
        {"summary_fp": False, "snapshots": False, "hashes": True},
    ],
    ids=["example-fingerprint", "example-model-snapshot", "placeholder-hashes"],
)
def test_publish_refuses_a_renamed_example(repo, tmp_path, capsys, giveaway):
    """Planted defect: the old check compared only run_id, so a renamed copy was published."""
    run = _renamed_example(tmp_path, "renamed")
    _fix_one(run, **giveaway)
    assert publish_results.main([str(run), "--root", str(repo)]) == 2
    assert "SYNTHETIC example" in capsys.readouterr().err
    assert not (repo / "site").exists()
    assert (repo / "README.md").read_bytes() == (ROOT / "README.md").read_bytes()


def test_a_renamed_example_is_still_stamped_synthetic(tmp_path):
    from agentic_analytics.report.render import looks_synthetic, render_report
    from agentic_analytics.run.runner import load_run
    from agentic_analytics.schemas import RunSummary

    run = _renamed_example(tmp_path, "stamp")
    summary = RunSummary.model_validate_json((run / "summary.json").read_text("utf-8"))
    _, records, questions = load_run(run)
    assert looks_synthetic(summary, records)
    html = render_report(summary, records, tmp_path / "out", questions=questions)
    assert "SYNTHETIC EXAMPLE" in html.read_text(encoding="utf-8")
    real = _run_dir(tmp_path / "real", run_id="r-real")
    summary = RunSummary.model_validate_json((real / "summary.json").read_text("utf-8"))
    assert not looks_synthetic(summary, load_run(real)[1])


def test_publish_refuses_a_key_in_a_trace(repo, tmp_path, capsys):
    run = _run_dir(tmp_path, run_id="r-20261201-leak")
    trace = sorted((run / "traces").glob("*.json"))[0]
    planted = "sk-ant-" + "a1B2c3D4" * 4
    trace.write_text(json.dumps({"note": planted}), encoding="utf-8")
    assert publish_results.main([str(run), "--root", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "secret scan" in err and trace.name in err and planted not in err
    assert not (repo / "site").exists()


def test_secret_scan_walks_a_directory(tmp_path, capsys):
    """pages.yml runs `secret_scan.py site`: a directory argument must be scanned, not skipped."""
    import secret_scan

    site = tmp_path / "site"
    (site / "traces").mkdir(parents=True)
    (site / "index.html").write_text("<p>ok</p>", encoding="utf-8")
    assert secret_scan.main([str(site)]) == 0
    (site / "traces" / "t.json").write_text('{"k": "sk-ant-' + "Zz9" * 8 + '"}', encoding="utf-8")
    assert secret_scan.main([str(site)]) == 1
    assert "t.json" in capsys.readouterr().out


def test_publish_writes_the_record_the_region_and_the_models(repo, tmp_path):
    """The committed record (docs/results/) is the run's own summary and stamp, and the D-62
    region and the agents' models reach the dashboard and the README results block."""
    run = _run_dir(tmp_path, run_id="r-20261201-stamp")
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    manifest["snowflake_region"] = {"region": "AWS_AP_SOUTHEAST_7", "source": "run.yaml"}
    manifest["config"] = {"models": {"claude_mcp": "m-claude", "gpt_mcp": "m-gpt"}}
    (run / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert publish_results.main([str(run), "--root", str(repo)]) == 0
    for name in ("summary.json", "run.json"):
        assert (repo / "docs/results" / name).read_bytes() == (run / name).read_bytes()
    assert "Thailand" in (repo / "site/index.html").read_text(encoding="utf-8")
    readme = (repo / "README.md").read_text(encoding="utf-8")
    block = readme.split(RESULTS_START, 1)[1].split(RESULTS_END, 1)[0]
    assert "Thailand" in block and "`m-claude`" in block and "r-20261201-stamp" in block
