"""LOD-07: pack discovery, manifest validation and file access (OWNER D-29, Tech Spec §2.7)."""

from __future__ import annotations

import os
import shutil
from importlib import resources
from pathlib import Path

import pytest

from agentic_analytics.packs import (
    Pack,
    PackError,
    bundled_packs,
    load_pack,
    pack_file,
    read_pack_text,
)

BUNDLED_FILES = (
    "pack.yaml",
    "parity.yaml",
    "load/clickhouse.sql",
    "load/snowflake.sql",
    "probes/clickhouse.sql",
    "probes/snowflake.sql",
)


def bundled_dir() -> Path:
    return Path(str(resources.files("agentic_analytics.packs") / "uk_price_paid"))


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "my_pack"
    shutil.copytree(bundled_dir(), dst, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    return dst


def test_bundled_pack_loads_by_name_through_importlib_resources() -> None:
    pack = load_pack("uk_price_paid")
    assert isinstance(pack, Pack) and pack.bundled
    assert pack.ref == "uk_price_paid@1.0"
    assert pack.manifest.schema_version == "1.0"
    assert "uk_price_paid" in bundled_packs()


def test_every_pack_data_file_is_package_data_readable_via_importlib_resources() -> None:
    root = resources.files("agentic_analytics.packs").joinpath("uk_price_paid")
    for rel in BUNDLED_FILES:
        f = root.joinpath(*rel.split("/"))
        assert f.is_file(), rel
        assert f.read_text(encoding="utf-8").strip(), rel


def test_pack_yaml_text_is_readable_via_importlib_resources() -> None:
    text = resources.files("agentic_analytics.packs").joinpath("uk_price_paid", "pack.yaml")
    assert 'schema_version: "1.0"' in text.read_text(encoding="utf-8")


def test_directory_pack_loads_from_path_and_string(pack_copy: Path) -> None:
    by_path = load_pack(pack_copy)
    assert not by_path.bundled and by_path.root == pack_copy.resolve()
    by_str = load_pack(str(pack_copy))
    assert by_str.manifest == by_path.manifest == load_pack("uk_price_paid").manifest


def test_relative_string_with_dot_is_a_path(pack_copy: Path, monkeypatch) -> None:
    monkeypatch.chdir(pack_copy.parent)
    assert load_pack("./my_pack").name == "uk_price_paid"


def test_bare_name_never_falls_back_to_a_directory(pack_copy: Path, monkeypatch) -> None:
    monkeypatch.chdir(pack_copy.parent)
    with pytest.raises(PackError, match=r"no bundled pack named 'my_pack'.*\./my_pack"):
        load_pack("my_pack")


def test_missing_directory_and_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="does not exist"):
        load_pack(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(PackError, match=r"no pack\.yaml"):
        load_pack(tmp_path / "empty")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("name: [unclosed", "invalid YAML"),
        ("- a\n- b\n", "top level must be a mapping"),
        ("schema_version: '1.0'\nname: x\nname: y\n", "duplicate key"),
        ("schema_version: '2.0'\n", "not compatible"),
    ],
)
def test_bad_manifests_are_refused_with_the_file_named(
    pack_copy: Path, text: str, match: str
) -> None:
    (pack_copy / "pack.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(PackError, match=match) as info:
        load_pack(pack_copy)
    assert "pack.yaml" in str(info.value)


def test_manifest_must_validate_as_dataset_pack(pack_copy: Path) -> None:
    p = pack_copy / "pack.yaml"
    p.write_text(
        p.read_text(encoding="utf-8").replace("engines: [clickhouse, snowflake]", "engines: [x]"),
        encoding="utf-8",
    )
    with pytest.raises(PackError, match="engines"):
        load_pack(pack_copy)


def test_pack_file_resolves_inside_the_pack() -> None:
    pack = load_pack("uk_price_paid")
    assert pack_file(pack, "load/clickhouse.sql").is_file()
    assert "CREATE DATABASE {database};" in read_pack_text(pack, "load/clickhouse.sql")


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "/etc/passwd",
        "../pack.yaml",
        "load/../pack.yaml",
        "./pack.yaml",
        "load//x.sql",
        "load\\clickhouse.sql",
        "a\x00b",
    ],
)
def test_pack_file_refuses_paths_that_leave_or_are_malformed(rel: str) -> None:
    with pytest.raises(PackError):
        pack_file(load_pack("uk_price_paid"), rel)


def test_pack_file_missing_file_is_named() -> None:
    with pytest.raises(PackError, match=r"no file 'load/duckdb\.sql'"):
        pack_file(load_pack("uk_price_paid"), "load/duckdb.sql")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_pack_file_refuses_a_symlink_out_of_a_directory_pack(
    pack_copy: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.sql"
    secret.write_text("DROP TABLE x;", encoding="utf-8")
    (pack_copy / "load" / "evil.sql").symlink_to(secret)
    with pytest.raises(PackError, match="outside the pack"):
        pack_file(load_pack(pack_copy), "load/evil.sql")


def test_bundled_pack_name_must_match_its_directory(monkeypatch) -> None:
    import agentic_analytics.packs as packs

    real = packs._parse_manifest

    def renamed(text: str, where: str):
        return real(text.replace("name: uk_price_paid", "name: other_pack"), where)

    monkeypatch.setattr(packs, "_parse_manifest", renamed)
    with pytest.raises(PackError, match="names itself 'other_pack'"):
        load_pack("uk_price_paid")
