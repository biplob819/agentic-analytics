"""FIX-05: fix content frozen and hashed before the baseline; gate 8 checks it (OWNER D-13)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agentic_analytics.fixes import (
    FIX_FILES,
    FROZEN_FILE,
    FixError,
    current_fix_hash,
    fix_blocks,
    fix_hash,
    fix_hash_matches_frozen,
    freeze_fixes,
    frozen_fix_hash,
    frozen_problems,
    load_frozen,
)
from agentic_analytics.fixes import examples as examples_mod
from agentic_analytics.fixes.freeze import INITIAL_REASON, fix_contents, main
from agentic_analytics.packs import load_pack
from tests.gold_check.fakes import BUNDLED, copy_bundled

T0 = datetime(2026, 11, 15, 12, tzinfo=UTC)


@pytest.fixture
def pack_dir(tmp_path):
    root = copy_bundled(tmp_path / "pack")
    (root / FROZEN_FILE).unlink(missing_ok=True)
    return root


@pytest.mark.gold
def test_bundled_fixes_match_the_frozen_record():
    """Gate 8: the shipped fix files and descriptions block are exactly what was frozen."""
    assert frozen_problems(BUNDLED) == []
    assert fix_hash_matches_frozen(BUNDLED)
    record = load_frozen(BUNDLED)
    assert record is not None and record.fix_hash == fix_hash(BUNDLED)
    assert record.fix_hash == current_fix_hash(BUNDLED) == frozen_fix_hash(BUNDLED)
    assert set(record.files) == set(FIX_FILES)
    assert set(record.blocks) == {"descriptions", "examples", "self_check"}
    assert record.reason.strip()


def test_hash_covers_the_rendered_blocks_and_the_loaded_rules(pack_dir):
    pack = load_pack(pack_dir)
    contents = fix_contents(pack)
    assert contents["blocks"] == fix_blocks(pack)
    assert isinstance(contents["rules"], dict) and contents["rules"]["max_change_pct"] == 500


def test_changed_examples_header_changes_the_hash_and_fails_the_check(pack_dir, monkeypatch):
    """BLOCKER: a renderer change with no fix-file change still changes fix_hash."""
    pack = load_pack(pack_dir)
    freeze_fixes(pack, now=lambda: T0)
    base = fix_hash(pack)
    monkeypatch.setattr(examples_mod, "BLOCK_INTRO", examples_mod.BLOCK_INTRO + " Be careful.")
    assert fix_hash(pack) != base
    assert frozen_problems(pack) == ["the rendered examples block changed after the freeze"]
    assert main(["--pack", str(pack_dir), "--check"]) == 1


def test_hash_covers_exact_file_text_and_the_descriptions_block(pack_dir):
    pack = load_pack(pack_dir)
    contents = fix_contents(pack)
    assert set(contents["files"]) == set(FIX_FILES)  # type: ignore[arg-type]
    base = fix_hash(pack)
    for rel in (*FIX_FILES, "semantic.yaml"):
        p = pack_dir / rel
        original = p.read_text(encoding="utf-8")
        p.write_text(original + "\n# a trailing comment\n", encoding="utf-8")
        changed = fix_hash(load_pack(pack_dir))
        p.write_text(original, encoding="utf-8")
        if rel == "semantic.yaml":  # a YAML comment does not change the generated block
            assert changed == base
        else:
            assert changed != base, rel
    sem = pack_dir / "semantic.yaml"
    text = sem.read_text(encoding="utf-8")
    sem.write_text(
        text.replace("synonyms: [city, post town]", "synonyms: [city]"), encoding="utf-8"
    )
    assert fix_hash(load_pack(pack_dir)) != base


def test_freeze_writes_the_record_and_refuses_to_overwrite(pack_dir):
    pack = load_pack(pack_dir)
    path = freeze_fixes(pack, now=lambda: T0)
    assert path == pack_dir / FROZEN_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "fix_freeze" and data["fix_hash"] == fix_hash(pack)
    assert data["frozen_at"] == "2026-11-15T12:00:00Z" or data["frozen_at"].startswith(
        "2026-11-15T12"
    )
    assert data["reason"] == INITIAL_REASON
    assert fix_hash_matches_frozen(pack)
    with pytest.raises(FixError, match="already frozen"):
        freeze_fixes(pack)
    with pytest.raises(FixError, match="needs a reason"):
        freeze_fixes(pack, force=True)
    freeze_fixes(pack, force=True, reason="D-52: examples replaced", now=lambda: T0)
    assert json.loads(path.read_text(encoding="utf-8"))["reason"] == "D-52: examples replaced"


def test_changed_fix_file_after_freeze_fails_the_gate(pack_dir):
    pack = load_pack(pack_dir)
    freeze_fixes(pack, now=lambda: T0)
    ex = pack_dir / "fixes" / "examples.yaml"
    ex.write_text(ex.read_text(encoding="utf-8").replace("SALFORD", "BOLTON"), encoding="utf-8")
    assert not fix_hash_matches_frozen(pack)
    assert frozen_problems(pack) == [
        "fixes/examples.yaml changed after the freeze",
        "the rendered examples block changed after the freeze",
    ]
    assert current_fix_hash(pack) != frozen_fix_hash(pack)


def test_changed_threshold_or_semantic_text_after_freeze_fails_the_gate(pack_dir):
    pack = load_pack(pack_dir)
    freeze_fixes(pack, now=lambda: T0)
    rules = pack_dir / "fixes" / "self_check.yaml"
    rules.write_text(
        rules.read_text(encoding="utf-8").replace("max_change_pct: 500", "max_change_pct: 400"),
        encoding="utf-8",
    )
    sem = pack_dir / "semantic.yaml"
    sem.write_text(
        sem.read_text(encoding="utf-8").replace("synonyms: [city, post town]", "synonyms: [city]"),
        encoding="utf-8",
    )
    assert frozen_problems(pack) == [
        "fixes/self_check.yaml changed after the freeze",
        "the rendered descriptions block changed after the freeze",
        "the rendered self_check block changed after the freeze",
        "the self-check rules as loaded changed after the freeze",
    ]


def test_missing_or_corrupt_record(pack_dir):
    pack = load_pack(pack_dir)
    assert load_frozen(pack) is None
    assert "freeze the fixes before the baseline" in frozen_problems(pack)[0]
    (pack_dir / FROZEN_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(FixError, match=r"FROZEN\.json"):
        frozen_problems(pack)


def test_freeze_refuses_malformed_fix_files(pack_dir):
    (pack_dir / "fixes" / "examples.yaml").write_text("examples: [\n", encoding="utf-8")
    with pytest.raises(FixError, match="invalid YAML"):
        freeze_fixes(load_pack(pack_dir))
    assert not (pack_dir / FROZEN_FILE).exists()


def test_module_entry_point_freezes_and_checks(pack_dir, capsys):
    assert main(["--pack", str(pack_dir), "--check"]) == 1
    assert main(["--pack", str(pack_dir)]) == 0
    assert main(["--pack", str(pack_dir)]) == 2  # already frozen
    capsys.readouterr()
    assert main(["--pack", str(pack_dir), "--force"]) == 2  # --force needs --reason
    assert "--reason" in capsys.readouterr().err
    assert main(["--pack", str(pack_dir), "--force", "--reason", "D-52"]) == 0
    assert main(["--pack", str(pack_dir), "--check"]) == 0
    assert "matches the frozen record" in capsys.readouterr().out


def test_force_without_a_reason_is_refused_on_an_unfrozen_pack_too(pack_dir, capsys):
    assert main(["--pack", str(pack_dir), "--force"]) == 2
    assert "--reason" in capsys.readouterr().err
    assert not (pack_dir / FROZEN_FILE).exists()


def test_a_record_for_another_pack_or_with_another_fix_hash_fails_the_gate(pack_dir):
    assert main(["--pack", str(pack_dir)]) == 0
    record = pack_dir / FROZEN_FILE
    original = record.read_text(encoding="utf-8")
    data = json.loads(original)
    data["fix_hash"] = "0" * 64
    record.write_text(json.dumps(data), encoding="utf-8")
    assert any(
        "fix_hash" in p and "000000000000" in p for p in frozen_problems(load_pack(pack_dir))
    )
    data = json.loads(original)
    data["pack"] = "other_pack@1.0"
    record.write_text(json.dumps(data), encoding="utf-8")
    assert any("other_pack@1.0" in p for p in frozen_problems(load_pack(pack_dir)))
