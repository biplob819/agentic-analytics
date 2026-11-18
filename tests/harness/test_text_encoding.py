"""Every text file read or write names its encoding (L-32). Windows defaults to cp1252, so
an implicit encoding passes on macOS and Linux and breaks on Windows CI or for Windows users."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALLS = {"read_text", "write_text", "open"}


def offenders():
    found = []
    for base in ("src", "scripts", "tests"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name not in CALLS:
                    continue
                if name == "open":
                    mode = (
                        node.args[1]
                        if len(node.args) > 1
                        else next((k.value for k in node.keywords if k.arg == "mode"), None)
                    )
                    if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                        continue  # binary
                    if (
                        isinstance(f, ast.Attribute)
                        and f.attr == "open"
                        and not isinstance(f.value, ast.Name | ast.Call | ast.Attribute)
                    ):
                        continue
                if any(k.arg == "encoding" for k in node.keywords):
                    continue
                positional = {"read_text": 1, "write_text": 2, "open": 4}[name]
                if len(node.args) >= positional:
                    continue  # encoding passed positionally, e.g. read_text("utf-8")
                found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
    return found


def test_every_text_io_call_names_its_encoding():
    assert offenders() == []


def _text_write(node: ast.Call) -> bool:
    """`write_text(...)`, or `open` / `Path.open` / `os.fdopen` in a text write mode."""
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
    if name == "write_text":
        return True
    if name not in ("open", "fdopen"):
        return False
    module = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
    at = 0 if name == "open" and isinstance(f, ast.Attribute) and module not in ("os", "io") else 1
    mode = (
        node.args[at]
        if len(node.args) > at
        else next((k.value for k in node.keywords if k.arg == "mode"), None)
    )
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return False  # read mode by default
    return "b" not in mode.value and any(c in mode.value for c in "wax+")


def newline_offenders(bases: list[Path]) -> list[str]:
    """Text writes that leave `newline` to the OS: on Windows each \\n becomes \\r\\n, so a record,
    summary, SQL file, figure or .env written there differs from the same file written on
    Linux, and every byte-for-byte check fails on Windows only (the repository is LF)."""
    found = []
    for base in bases:
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and _text_write(node)
                    and not any(k.arg == "newline" for k in node.keywords)
                ):
                    found.append(f"{path.relative_to(base).as_posix()}:{node.lineno}")
    return found


def test_every_text_write_fixes_its_line_breaks():
    """B3.3 (Windows CI): the code writes \\n on every OS (tests may use the OS default)."""
    assert newline_offenders([ROOT / "src", ROOT / "scripts"]) == []


def test_newline_check_bites(tmp_path):
    (tmp_path / "ok.py").write_text(
        "from pathlib import Path\nimport os\n"
        "Path('a').write_text('x', encoding='utf-8', newline='\\n')\n"
        "Path('a').read_text(encoding='utf-8')\n"
        "open('a', encoding='utf-8')\n"
        "open('a', 'rb')\n"
        "Path('a').open('wb')\n"
        "os.fdopen(3, 'w', encoding='utf-8', newline='\\n')\n",
        encoding="utf-8",
    )
    planted = {
        "write_text.py": "from pathlib import Path\nPath('a').write_text('x', encoding='utf-8')\n",
        "open_w.py": "with open('a', 'w', encoding='utf-8') as f:\n    f.write('x')\n",
        "path_open_a.py": "from pathlib import Path\nPath('a').open('a', encoding='utf-8')\n",
        "fdopen.py": "import os\nos.fdopen(3, mode='w', encoding='utf-8')\n",
    }
    for name, code in planted.items():
        (tmp_path / name).write_text(code, encoding="utf-8")
    assert {o.split(":")[0] for o in newline_offenders([tmp_path])} == set(planted)
