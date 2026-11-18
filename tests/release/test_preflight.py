"""B3.3: scripts/release_preflight.py refuses to build while a direct reference is pinned
(PyPI rejects it; OWNER D-47) and refuses a wheel without the dataset pack (OSS-01, OSS-07)."""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import release_preflight as rp

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
DIRECT = re.compile(r'^\s*"[^"]*git\+[^"]*",\n', re.M)
ALLOW = "allow-direct-references = true"


def _cleaned(tmp_path: Path) -> Path:
    """The real pyproject with any direct reference swapped for a PyPI range (what D-47 did;
    since D-97 and D-109 the real pyproject has none, so this copy equals it)."""
    text = PYPROJECT.read_text(encoding="utf-8")
    text = DIRECT.sub('    "evalkit-harness>=0.1,<0.2",\n', text)
    text = text.replace(ALLOW, "")
    out = tmp_path / "pyproject.toml"
    out.write_text(text, encoding="utf-8")
    return out


def test_a_cleaned_pyproject_passes(tmp_path):
    clean = _cleaned(tmp_path)
    assert rp.pyproject_problems(clean) == []
    assert rp.main(["pyproject", str(clean)]) == 0


@pytest.mark.parametrize(
    ("planted", "why"),
    [
        ('    "evalkit-harness @ git+https://github.com/o/r@abc",\n', "direct reference"),
        ('    "evalkit-harness @ https://example.org/e-0.1.whl",\n', "direct reference"),
    ],
)
def test_a_direct_dependency_fails(tmp_path, planted, why):
    clean = _cleaned(tmp_path).read_text(encoding="utf-8")
    bad = tmp_path / "bad.toml"
    bad.write_text(clean.replace("dependencies = [\n", "dependencies = [\n" + planted, 1), "utf-8")
    assert any(why in p for p in rp.pyproject_problems(bad))
    assert rp.main(["pyproject", str(bad)]) == 1


def test_allow_direct_references_fails(tmp_path):
    clean = _cleaned(tmp_path).read_text(encoding="utf-8")
    bad = tmp_path / "bad.toml"
    assert "[build-system]\n" in clean
    bad.write_text(
        clean.replace("[build-system]\n", f"[tool.hatch.metadata]\n{ALLOW}\n\n[build-system]\n"),
        "utf-8",
    )
    assert rp.pyproject_problems(bad) == [
        "[tool.hatch.metadata] allow-direct-references = true; remove it"
    ]


def test_an_optional_extra_with_a_direct_reference_fails(tmp_path):
    clean = _cleaned(tmp_path).read_text(encoding="utf-8")
    bad = tmp_path / "bad.toml"
    line = 'planted = ["x @ git+https://e.org/x"]\n'
    table = "[project.optional-dependencies]\n"
    if table in clean:  # merge into the existing table (the `demo` extra, D-57)
        text = clean.replace(table, table + line, 1)
    else:
        text = clean.replace("\n[project.urls]", f"\n{table}{line}\n[project.urls]", 1)
    assert line in text
    bad.write_text(text, "utf-8")
    assert "dependency 'x' is a direct reference; PyPI rejects it" in rp.pyproject_problems(bad)


def test_the_current_pyproject_is_publishable():
    """OSS-01, OWNER D-47 / D-97 / D-109: EvalKit comes from PyPI and nothing allows a direct
    reference, so the release workflow's preflight passes on the committed pyproject."""
    assert rp.pyproject_problems(PYPROJECT) == []
    assert rp.main(["pyproject", str(PYPROJECT)]) == 0
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "git+" not in text and ALLOW not in text


def test_every_required_wheel_file_exists_in_the_source_tree():
    src = [p.relative_to(ROOT / "src").as_posix() for p in (ROOT / "src").rglob("*")]
    for pattern in rp.WHEEL_REQUIRED:
        assert fnmatch.filter(src, pattern), pattern


def _fake_wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, "x")
    return path


def test_wheel_check_names_each_missing_file(tmp_path):
    full = [p.replace("*", "t1") for p in rp.WHEEL_REQUIRED]
    ok = _fake_wheel(tmp_path / "ok-0.1-py3-none-any.whl", full)
    assert rp.wheel_problems(ok) == []
    gone = f"{rp.PACK}/fixes/FROZEN.json"
    bad_dir = tmp_path / "dist"
    bad_dir.mkdir()
    _fake_wheel(bad_dir / "bad-0.1-py3-none-any.whl", [n for n in full if n != gone])
    assert rp.wheel_problems(bad_dir) == [f"bad-0.1-py3-none-any.whl is missing {gone}"]
    assert rp.main(["wheel", str(bad_dir)]) == 1
    no_traces = [n for n in full if "/traces/" not in n]
    assert rp.wheel_problems(_fake_wheel(tmp_path / "nt.whl", no_traces))
    assert rp.wheel_problems(tmp_path / "empty-dir-does-not-exist") != []


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")


@pytest.mark.build
@needs_uv
def test_a_real_build_carries_the_pack_and_leaves_planning_out(tmp_path):
    out = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--quiet", "--out-dir", str(out)], cwd=ROOT, check=True, timeout=300
    )
    assert rp.wheel_problems(out) == []
    assert rp.main(["wheel", str(out)]) == 0
    import tarfile

    (sdist,) = out.glob("*.tar.gz")
    with tarfile.TarFile.gzopen(sdist) as tf:
        names = [n.split("/", 1)[1] for n in tf.getnames() if "/" in n]
    for kept_out in (
        "specs/",
        "Agentic Analytics PRD.md",
        "Agentic Analytics Tech Spec.md",
        "portfolio.yml",
        "docs/assets/walkthrough.gif",
        "tests/",
        "docs/decisions.md",
        "docs/graph/",
        "docs/lessons.md",
    ):
        assert not [n for n in names if n.startswith(kept_out)], kept_out
    assert "src/agentic_analytics/packs/uk_price_paid/pack.yaml" in names


def test_pyproject_metadata_describes_the_tool():
    import tomllib

    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert "text-to-SQL" in project["description"] and "dataset packs" in project["description"]
    assert project["readme"] == {"file": "README.md", "content-type": "text/markdown"}
    classifiers = set(project["classifiers"])
    for c in (
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Operating System :: OS Independent",
        "Topic :: Database",
        "Topic :: Scientific/Engineering",
    ):
        assert c in classifiers, c
    # PEP 639: `license = "MIT"` replaces license classifiers; PyPI refuses both together
    assert not [c for c in classifiers if c.startswith("License ::")]
    assert project["keywords"]
    base = "https://github.com/biplob819/agentic-analytics"
    urls = project["urls"]
    for key in ("Homepage", "Repository", "Documentation", "Issues", "Changelog"):
        assert urls[key].startswith(base), key
