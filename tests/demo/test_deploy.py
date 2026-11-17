"""D-57 (D4.2; DEM-13): the demo as its own Streamlit Community Cloud app. The repo-root entry
`demo/app.py` puts `src/` on the path and calls the page's `main`; `demo/requirements.txt` pins
the core dependencies and the `demo` extra to uv.lock (planted drift is caught); the Snowflake
read-only key is read from its PEM text (`SNOWFLAKE_RO_PRIVATE_KEY`) when the `_PATH` variable is
absent, everywhere the path is read, and the text never reaches a repr, a message, a chained
cause or a log line. Keys are generated per test run; nothing connects."""

from __future__ import annotations

import ast
import functools
import logging
import re
import runpy
import sys
import tomllib
from pathlib import Path

import pytest

from agentic_analytics import tracing as T
from agentic_analytics.backends.cortex import CortexConfigError, KeyPairJwt
from agentic_analytics.demo import page as P
from agentic_analytics.run import runner as R
from agentic_analytics.warehouse import doctor as D
from agentic_analytics.warehouse import snowflake as S
from agentic_analytics.warehouse._common import WarehouseConfigError
from tests.demo.helpers import FakeSt
from tests.demo.test_page import clean  # noqa: F401
from tests.run import world as w

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "demo" / "app.py"
REQUIREMENTS = ROOT / "demo" / "requirements.txt"
PLANTED = "PLANTEDkeyTEXT0123456789abcdefPLANTED"


@functools.cache
def pem_key(seed: int = 0) -> str:
    """A throwaway RSA private key as PKCS#8 PEM text (generated here, never committed)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


_PEM_HEAD = "-----BEGIN " + "PRIVATE KEY-----"  # split so the repo's secret scan stays quiet
BAD_PEM = f"{_PEM_HEAD}\n{PLANTED}\n{_PEM_HEAD.replace('BEGIN', 'END')}\n"


# ---- the entry point ---------------------------------------------------------------------------


def test_the_root_entry_puts_src_on_the_path_and_calls_the_page_main():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    code = ast.unparse(tree)
    assert "ROOT = Path(__file__).resolve().parent.parent" in code
    assert "sys.path.insert(0, str(ROOT / 'src'))" in code
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert any(n.module == "agentic_analytics.demo.page" and [a.name for a in n.names] == ["main"]
               for n in imports)  # fmt: skip
    last = tree.body[-1]
    assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call)
    assert isinstance(last.value.func, ast.Name) and last.value.func.id == "main"
    # the insert comes before the package import
    insert_line = next(n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                       and ast.unparse(n.func) == "sys.path.insert")  # fmt: skip
    page_import = next(n.lineno for n in imports if n.module == "agentic_analytics.demo.page")
    assert insert_line < page_import


def test_the_root_entry_renders_the_page_from_a_checkout(clean, monkeypatch):  # noqa: F811
    st = FakeSt()
    monkeypatch.setitem(sys.modules, "streamlit", st)  # `main()` imports streamlit itself
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(ROOT / "src")])
    runpy.run_path(str(APP), run_name="__main__")
    assert str(ROOT / "src") == sys.path[0]
    assert st.named("set_page_config") and len(st.texts("subheader")) == 2
    assert "saved answer from the benchmark run" in st.texts("info")[0]


def test_the_streamlit_config_hides_error_details_and_holds_no_secret():
    """Streamlit Community Cloud reads `.streamlit/config.toml` from the repo root: an uncaught
    exception shows no message or traceback (D-96), and the file carries settings only."""
    path = ROOT / ".streamlit" / "config.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    assert config["client"]["showErrorDetails"] == "none"  # Streamlit 1.67's spelling of false
    assert set(config) == {"client"}
    assert ".streamlit/secrets.toml" in (ROOT / ".gitignore").read_text(encoding="utf-8")


# ---- demo/requirements.txt in step with pyproject.toml and uv.lock -----------------------------


def _name(req: str) -> str:
    return re.split(r"[\[=<>@ ;]", req.strip(), maxsplit=1)[0].strip().lower()


def locked_demo_set(lock: dict) -> set[tuple[str, str]]:
    """(name, version) of every package the core dependencies plus the `demo` extra resolve to
    in uv.lock, on any platform (markers kept as uv export keeps them: a superset, never less)."""
    by_name: dict[str, list[dict]] = {}
    for pkg in lock["package"]:
        by_name.setdefault(pkg["name"].lower(), []).append(pkg)
    (root,) = [p for p in lock["package"] if p.get("source", {}).get("editable") == "."]
    todo = [*root.get("dependencies", []), *root.get("optional-dependencies", {})["demo"]]
    seen: set[tuple[str, str]] = set()
    while todo:
        dep = todo.pop()
        for pkg in by_name[dep["name"].lower()]:
            if "version" in dep and pkg["version"] != dep["version"]:
                continue
            key = (pkg["name"].lower(), pkg["version"])
            extras = dep.get("extra", [])
            for extra in extras:
                todo += pkg.get("optional-dependencies", {}).get(extra, [])
            if key in seen:
                continue
            seen.add(key)
            todo += pkg.get("dependencies", [])
    return seen


def requirement_problems(req_text: str, pyproject: dict, lock: dict) -> list[str]:
    """Why `req_text` is not exactly the locked resolution of the core dependencies + the `demo`
    extra (D-95): every package pinned `==` to its uv.lock version, nothing more, nothing less,
    and the evalkit-harness git pin exactly as pyproject.toml has it (empty means in step)."""
    project = pyproject["project"]
    wanted = {_name(r): r for r in [*project["dependencies"],
                                    *project["optional-dependencies"]["demo"]]}  # fmt: skip
    locked = locked_demo_set(lock)
    versions: dict[str, set[str]] = {}
    for name, version in locked:
        versions.setdefault(name, set()).add(version)
    lines = [ln.split(";", 1)[0].strip() for ln in req_text.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]  # fmt: skip
    problems = []
    got: set[tuple[str, str]] = set()
    names: set[str] = set()
    for ln in lines:
        name = _name(ln)
        names.add(name)
        spec = wanted.get(name, "")
        if "@" in spec:  # a direct reference (the evalkit-harness git pin, D-24)
            if ln != spec:
                problems.append(f"{name} must be listed exactly as in pyproject.toml")
            entry = (name, next(iter(versions.get(name, {""}))))
        else:
            m = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", ln)
            if m is None:
                problems.append(f"{name} is not pinned with ==")
                continue
            entry = (name, m.group(2))
            if name not in versions:
                problems.append(f"{name} is not in the locked core + demo set")
                continue
            if m.group(2) not in versions[name]:
                problems.append(f"{ln} but uv.lock has {', '.join(sorted(versions[name]))}")
                continue
        if entry in got:
            problems.append(f"a package is listed twice ({name})")
        got.add(entry)
    for name in sorted(set(wanted) - names):
        problems.append(f"{name} missing")
    for name, version in sorted(locked - got):
        if name in names and name in wanted and "@" in wanted[name]:
            continue
        problems.append(f"{name}=={version} missing")
    return problems


def _project() -> tuple[dict, dict]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return pyproject, lock


def test_demo_requirements_pin_the_core_and_demo_extra_to_uv_lock():
    pyproject, lock = _project()
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert requirement_problems(text, pyproject, lock) == []
    listed = {_name(ln) for ln in text.splitlines() if ln and not ln.startswith("#")}
    assert {"streamlit", "psycopg", "psycopg-binary", "anthropic", "openai", "httpx2",
            "snowflake-connector-python", "clickhouse-connect", "mcp", "evalkit-harness",
            "pandas", "numpy", "narwhals"} <= listed  # fmt: skip
    assert len(listed) > 50  # the whole resolved set, not only the direct dependencies
    todo = [ln for ln in text.splitlines() if "TODO" in ln]
    assert len(todo) == 1 and "PyPI" in todo[0]  # only the switch to the PyPI release


def test_demo_requirements_are_what_uv_export_prints():
    """The same set as `uv export` from the lock, line for line (when uv is on the PATH)."""
    import shutil
    import subprocess

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH; test_demo_requirements_pin_the_core... checks the lock")
    out = subprocess.run(
        [uv, "export", "--no-hashes", "--no-annotate", "--no-header", "--extra", "demo",
         "--no-dev", "--frozen", "--no-emit-project", "--offline"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    text = REQUIREMENTS.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines == [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda t: t.replace("streamlit==1.67.0\n", ""), "streamlit missing"),
        (lambda t: t.replace("narwhals==", "#narwhals=="), "narwhals=="),  # a transitive one
        (lambda t: t.replace("sqlglot==30.31.0", "sqlglot==30.30.0"), "sqlglot==30.30.0 but"),
        (lambda t: t.replace("pyjwt==2.15.1", "pyjwt>=2.9"), "pyjwt is not pinned with =="),
        (lambda t: t.replace("psycopg-binary==3.3.9", "#"), "psycopg-binary==3.3.9 missing"),
        (lambda t: t.replace("6530fd119", "6530fd118"), "evalkit-harness must be listed exactly"),
        (lambda t: t + "rich==13.9.4\n", "rich is not in the locked core + demo set"),
        (lambda t: t + "mcp==2.3.0\n", "a package is listed twice"),
    ],
)
def test_the_requirements_check_catches_planted_drift(edit, problem):
    pyproject, lock = _project()
    text = REQUIREMENTS.read_text(encoding="utf-8")
    planted = edit(text)
    assert planted != text
    assert any(p.startswith(problem) for p in requirement_problems(planted, pyproject, lock))


def test_a_new_core_dependency_must_reach_the_requirements():
    pyproject, lock = _project()
    pyproject["project"]["dependencies"] = [*pyproject["project"]["dependencies"], "rich>=13"]
    text = REQUIREMENTS.read_text(encoding="utf-8")
    assert "rich missing" in requirement_problems(text, pyproject, lock)


# ---- the Snowflake key from its PEM text (D-57) ------------------------------------------------


def _sf_env(**extra: str) -> dict[str, str]:
    return {"SNOWFLAKE_ACCOUNT": "xy12345.eu-west-1", "SNOWFLAKE_RO_USER": "ro_user",
            "SNOWFLAKE_WAREHOUSE": "wh", **extra}  # fmt: skip


def _der(text: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(text.encode(), password=None)
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())  # fmt: skip


def test_the_executor_and_cortex_read_the_key_text_when_the_path_is_absent(tmp_path):
    text = pem_key()
    _, sf = D.executors_from_env(_sf_env(SNOWFLAKE_RO_PRIVATE_KEY=text))
    assert sf is not None and sf.connect_kwargs()["private_key"] == _der(text)
    one_line = text.strip().replace("\n", "\\n")  # pasted on one line with \n escapes
    _, sf1 = D.executors_from_env(_sf_env(SNOWFLAKE_RO_PRIVATE_KEY=one_line))
    assert sf1 is not None and sf1.connect_kwargs()["private_key"] == _der(text)
    # the path wins when both are set (local runs keep the file)
    other = pem_key(1)  # a second, different key
    keyfile = tmp_path / "ro.p8"
    keyfile.write_text(other, encoding="utf-8")
    _, sf2 = D.executors_from_env(
        _sf_env(SNOWFLAKE_RO_PRIVATE_KEY=text, SNOWFLAKE_RO_PRIVATE_KEY_PATH=str(keyfile))
    )
    assert sf2 is not None and sf2.connect_kwargs()["private_key"] == _der(other)
    # neither set: no executor; Cortex's JWT from text matches the one from the file
    assert D.executors_from_env(_sf_env())[1] is None
    keyfile.write_text(text, encoding="utf-8")
    from_text = KeyPairJwt.from_key_text("xy12345", "ro_user", text)
    from_file = KeyPairJwt.from_key_file("xy12345", "ro_user", keyfile)
    assert from_text.issuer == from_file.issuer and from_text.token()


def test_services_from_env_builds_cortex_from_the_key_text(tmp_path):
    cfg = w.config(backends=("cortex_analyst",))
    env = {"SNOWFLAKE_ACCOUNT": "xy12345", "SNOWFLAKE_RO_USER": "ro",
           "SNOWFLAKE_RO_PRIVATE_KEY": pem_key()}  # fmt: skip
    services = R.services_from_env(cfg, w.BUNDLED, env)
    assert services.cortex is not None and services.models == {}
    with pytest.raises(R.RunConfigError, match=r"SNOWFLAKE_RO_PRIVATE_KEY_PATH \(or "):
        R.services_from_env(cfg, w.BUNDLED, {"SNOWFLAKE_ACCOUNT": "a", "SNOWFLAKE_RO_USER": "u"})


@pytest.mark.safety
def test_the_key_text_never_reaches_a_repr_a_message_a_cause_or_a_log(caplog):
    caplog.set_level(logging.DEBUG)
    text = pem_key()
    body = text.splitlines()[1]  # a line of the real key's base64
    _, sf = D.executors_from_env(_sf_env(SNOWFLAKE_RO_PRIVATE_KEY=text))
    assert sf is not None and body not in repr(sf) and body not in repr(S.ro_key_from_env(
        _sf_env(SNOWFLAKE_RO_PRIVATE_KEY=text)))  # fmt: skip
    failures: list[BaseException] = []
    _, bad = D.executors_from_env(_sf_env(SNOWFLAKE_RO_PRIVATE_KEY=BAD_PEM))
    assert bad is not None
    with pytest.raises(WarehouseConfigError) as e1:
        bad.connect_kwargs()
    failures.append(e1.value)
    with pytest.raises(CortexConfigError) as e2:
        KeyPairJwt.from_key_text("xy12345", "ro", BAD_PEM)
    failures.append(e2.value)
    with pytest.raises(CortexConfigError) as e3:
        R.services_from_env(w.config(backends=("cortex_analyst",)), w.BUNDLED, {
            "SNOWFLAKE_ACCOUNT": "xy", "SNOWFLAKE_RO_USER": "ro",
            "SNOWFLAKE_RO_PRIVATE_KEY": BAD_PEM})  # fmt: skip
    failures.append(e3.value)
    for exc in failures:
        assert "SNOWFLAKE_RO_PRIVATE_KEY" in str(exc)
        assert exc.__cause__ is None and exc.__suppress_context__ is True
        assert PLANTED not in str(exc) and PLANTED not in repr(exc)
    env = _sf_env(SNOWFLAKE_RO_PRIVATE_KEY=BAD_PEM)
    report = D.run_doctor(env, snowflake=bad)  # the key fails to load before any connection
    assert any(c.status == "fail" for c in report.checks) and PLANTED not in repr(report)
    assert dict(report.env)["SNOWFLAKE_RO_PRIVATE_KEY"] is True  # presence only
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert PLANTED not in logged and body not in logged
    # the redactor and the env inventories know the variable; the page's secrets carry it
    assert "SNOWFLAKE_RO_PRIVATE_KEY" in T.SECRET_ENV_NAMES
    red = T.Redactor.from_environ({"SNOWFLAKE_RO_PRIVATE_KEY": text})
    assert body not in red.text(f"oops {text}")
    assert "SNOWFLAKE_RO_PRIVATE_KEY" in D.ENV_VARS and "SNOWFLAKE_RO_PRIVATE_KEY" in P.SECRETS
    assert "SNOWFLAKE_RO_PRIVATE_KEY_PATH" not in P.SECRETS  # the host has no key file
    for name in ("DEMO_LIVE_BACKENDS", "DEMO_CONTACT_URL"):  # settings, not secrets
        assert name in D.ENV_VARS and name in P.SECRETS and name not in T.SECRET_ENV_NAMES
