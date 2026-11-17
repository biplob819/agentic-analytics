"""`pack check` core (QST-08): a pack validated end to end with no accounts."""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path

import pytest

from agentic_analytics.questions import check_pack


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    src = Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))
    dst = tmp_path / "my_pack"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    return dst


def _rewrite(path: Path, index: int, **changes) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[index].update(changes)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_bundled_pack_is_clean_by_name_and_by_path(pack_copy: Path) -> None:
    by_name = check_pack("uk_price_paid")
    assert by_name.ok and by_name.to_dict() == {
        "pack": "uk_price_paid@1.0",
        "ok": True,
        "questions": 120,
        "probes": 20,
        "issues": [],
    }
    assert check_pack(pack_copy).ok


def test_lint_failures_are_reported(pack_copy: Path) -> None:
    path = pack_copy / "questions.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
    gold = dict(row["gold_sql"])
    gold["clickhouse"] = gold["clickhouse"].replace("quantileExactInclusive(0.5)", "median")
    _rewrite(path, 1, gold_sql=gold)
    report = check_pack(pack_copy)
    assert not report.ok
    assert report.issues == (
        "q-002 gold_sql [clickhouse]: approximate_function: median() is approximate or "
        "non-deterministic",
    )


def test_schema_errors_and_missing_files_are_reported(pack_copy: Path) -> None:
    _rewrite(pack_copy / "questions.jsonl", 4, top_n=3)
    (pack_copy / "probes.jsonl").unlink()
    report = check_pack(pack_copy)
    assert report.questions == 0 and report.probes == 0
    assert any(i.startswith("questions.jsonl: ") and ":5: q-005" in i for i in report.issues)
    assert any(
        i.startswith("probes.jsonl: ") and "no file 'probes.jsonl'" in i for i in report.issues
    )


def test_quota_shortfall_is_reported(pack_copy: Path) -> None:
    path = pack_copy / "questions.jsonl"
    rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(rows[:16] + rows[20:]) + "\n", encoding="utf-8")
    report = check_pack(pack_copy)
    assert report.issues == ("quotas: aggregation has 16 questions, below its quota of 17",)


def test_unplanted_marker_is_reported(pack_copy: Path) -> None:
    _rewrite(pack_copy / "probes.jsonl", 15, injection_marker="INJ-99")
    report = check_pack(pack_copy)
    assert report.issues == (
        "p-id01: marker 'INJ-99' is not planted in probes/clickhouse.sql",
        "p-id01: marker 'INJ-99' is not planted in probes/snowflake.sql",
    )


def test_semantic_yaml_is_parsed_when_present(pack_copy: Path) -> None:
    (pack_copy / "semantic.yaml").write_text("tables: [uk_price_paid]\n", encoding="utf-8")
    assert check_pack(pack_copy).ok
    (pack_copy / "semantic.yaml").write_text("- a\n- b\n", encoding="utf-8")
    assert check_pack(pack_copy).issues == ("semantic.yaml: top level must be a mapping",)
    (pack_copy / "semantic.yaml").write_text("a: [\n", encoding="utf-8")
    assert check_pack(pack_copy).issues[0].startswith("semantic.yaml: invalid YAML")


def test_bad_manifest_is_reported_not_raised(pack_copy: Path, tmp_path: Path) -> None:
    manifest = pack_copy / "pack.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("OGL-3.0", ""), "utf-8")
    report = check_pack(pack_copy)
    assert not report.ok and report.issues[0].startswith("pack.yaml: ")
    missing = check_pack(tmp_path / "nowhere")
    assert not missing.ok and "does not exist" in missing.issues[0]


# ---- CLI wiring: `agentic-analytics pack check [--pack PATH] [--json]` -------------------------


def test_cli_pack_check_reports_the_bundled_pack_clean(capsys) -> None:
    from agentic_analytics import cli

    assert cli.main(["pack", "check", "--json"]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out == check_pack("uk_price_paid").to_dict() and out["ok"] is True
    assert cli.main(["pack", "check"]) == cli.EXIT_OK
    assert "uk_price_paid@1.0: 120 questions, 20 probes, ok" in capsys.readouterr().out


def test_cli_pack_check_fails_with_exit_1_and_lists_issues(pack_copy: Path, capsys) -> None:
    from agentic_analytics import cli

    _rewrite(
        pack_copy / "questions.jsonl",
        0,
        gold_sql={
            "clickhouse": "SELECT median(price) AS m FROM uk.uk_price_paid",
            "snowflake": "SELECT MEDIAN(price) AS m FROM ANALYTICS.UK.UK_PRICE_PAID",
        },
    )
    assert cli.main(["pack", "check", "--pack", str(pack_copy), "--json"]) == cli.EXIT_FAILED
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and any("approximate_function" in i for i in out["issues"])


def test_cli_pack_without_check_is_a_usage_error(capsys) -> None:
    from agentic_analytics import cli

    assert cli.main(["pack", "--json"]) == cli.EXIT_ERROR
    assert "pack check" in capsys.readouterr().out + capsys.readouterr().err


def test_probe_gold_is_linted_by_the_pack_check(pack_copy: Path) -> None:
    probes = pack_copy / "probes.jsonl"
    rows = [json.loads(line) for line in probes.read_text(encoding="utf-8").splitlines()]
    i = next(n for n, r in enumerate(rows) if r["id"] == "p-iq02")
    gold = dict(rows[i]["gold_sql"])
    gold["clickhouse"] = gold["clickhouse"].replace(
        "quantileExactInclusive(0.5)(price)", "median(price)", 1
    )
    assert gold["clickhouse"] != rows[i]["gold_sql"]["clickhouse"]
    _rewrite(probes, i, gold_sql=gold)
    report = check_pack(pack_copy)
    assert not report.ok and any(
        "p-iq02" in i and "approximate_function" in i for i in report.issues
    ), report.issues


def test_missing_probes_sql_is_reported(pack_copy: Path) -> None:
    (pack_copy / "probes" / "snowflake.sql").unlink()
    report = check_pack(pack_copy)
    assert not report.ok
    assert any("no probes/snowflake.sql holds the planted rows" in i for i in report.issues)


def test_cli_pack_check_of_a_missing_pack_is_a_config_error(tmp_path: Path, capsys) -> None:
    from agentic_analytics import cli

    missing = tmp_path / "no_such_pack"
    assert cli.main(["pack", "check", "--pack", str(missing), "--json"]) == cli.EXIT_ERROR
    assert "cannot load pack" in capsys.readouterr().out


def test_cli_pack_check_text_lists_the_issues(pack_copy: Path, capsys) -> None:
    from agentic_analytics import cli

    (pack_copy / "probes" / "snowflake.sql").unlink()
    assert cli.main(["pack", "check", "--pack", str(pack_copy)]) == cli.EXIT_FAILED
    out = capsys.readouterr()
    assert "no probes/snowflake.sql holds the planted rows" in out.out + out.err
