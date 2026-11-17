"""D-57 (D4.2; DEM-14): `scripts/streamlit_secrets.py` turns `.env` into the hosted demo's
Streamlit secrets TOML, on stdout only. Planted fake values; the key is generated here."""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest

import streamlit_secrets as SS
from agentic_analytics.demo import page as P
from tests.demo.test_deploy import pem_key

ADMIN = {
    "CLICKHOUSE_ADMIN_USER": "PLANTEDadminUSER",
    "CLICKHOUSE_ADMIN_PASSWORD": "PLANTEDadminPASSWORD0123",
    "SNOWFLAKE_ADMIN_USER": "PLANTEDsfADMIN",
    "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH": "/keys/PLANTEDadmin.p8",
    "CLICKHOUSE_CLOUD_API_KEY": "PLANTEDcloudKEY0123",
    "CLICKHOUSE_CLOUD_API_SECRET": "PLANTEDcloudSECRET0123",
}
COMMENT = "PLANTEDownerCOMMENT"


class Out(io.StringIO):
    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def runtime_values() -> dict[str, str]:
    """A fake value for every runtime secret, some needing TOML escapes."""
    values = {n: f"PLANTED-{n.lower()}-value" for n in P.SECRETS if n != SS.KEY_TEXT}
    values["DEMO_PASSCODE"] = 'open "sesame" \\ with\ttab'
    values["DEMO_LIVE_BACKENDS"] = "claude_mcp,cortex_analyst"
    values["DEMO_CONTACT_URL"] = "https://github.com/biplob819/agentic-analytics/issues"
    return values


def write_env(where, values: dict[str, str], key_file: str | None = "keys/ro.p8") -> None:
    lines = [f"# {COMMENT}: owner notes stay local", ""]
    for n, v in values.items():
        quoted = f"'{v}'" if any(c in v for c in ' "\\\t') else v
        lines.append(f"{n}={quoted}")
    for n, v in ADMIN.items():
        lines.append(f"{n}={v}")
    lines.append("OPENAI_API_KEY=")  # blank: left out
    if key_file:
        lines.append(f"SNOWFLAKE_RO_PRIVATE_KEY_PATH={key_file}")
    (where / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(where, *, tty: bool = False) -> tuple[int, str, str]:
    out, err = Out(tty), Out()
    code = SS.main(["--env", str(where / ".env")], stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.safety
def test_the_toml_holds_exactly_the_demo_secrets_and_round_trips(tmp_path):
    values = runtime_values()
    key = pem_key()
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "ro.p8").write_text(key, encoding="utf-8")
    write_env(tmp_path, values)
    code, out, err = run(tmp_path)
    assert code == 0
    data = tomllib.loads(out)
    want = {n for n in P.SECRETS if n != "SNOWFLAKE_RO_PRIVATE_KEY_PATH"}
    assert set(data) == want  # exactly the page's list (the key as text, no path)
    assert data["SNOWFLAKE_RO_PRIVATE_KEY"] == key  # the file's text, byte for byte
    for n, v in values.items():
        assert data[n] == v, n
    for v in [*ADMIN.values(), COMMENT, "ro.p8", "SNOWFLAKE_RO_PRIVATE_KEY_PATH"]:
        assert v not in out, v
    assert not any(SS.FORBIDDEN.match(n) for n in data)
    # stderr names variables only, never a value
    assert "wrote" in err and key.splitlines()[1] not in err
    assert not any(v in err for v in values.values() if len(v) > 8 and v not in P.SECRETS)


@pytest.mark.safety
def test_a_terminal_gets_a_refusal_and_no_secret(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "ro.p8").write_text(pem_key(), encoding="utf-8")
    write_env(tmp_path, runtime_values())
    code, out, err = run(tmp_path, tty=True)
    assert code == 2 and out == ""
    assert "pbcopy" in err and "PLANTED" not in err and "PRIVATE KEY" not in err


@pytest.mark.safety
def test_admin_names_are_refused_even_if_the_page_list_ever_held_one():
    with pytest.raises(SS.SecretsError, match="admin credentials never go to the demo host"):
        SS.wanted_names(("DEMO_PASSCODE", "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH"))
    for name in ADMIN:
        with pytest.raises(SS.SecretsError):
            SS.wanted_names((name,))
    assert SS.wanted_names(P.SECRETS)  # the real list passes
    toml = SS.build_toml({**ADMIN, "DEMO_PASSCODE": "x"}, base=Path("."), names=P.SECRETS)
    assert tomllib.loads(toml) == {"DEMO_PASSCODE": "x"}


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        'a "quoted" word',
        "back\\slash",
        "tab\there",
        "ctl\x01\x1f\x7fchars",
        "ünïcödé €",
        'three """ quotes',
        "trailing\\",
        "line\r\nbreak",
    ],
)
def test_values_are_toml_escaped(value):
    assert tomllib.loads(SS.toml_line("V", value)) == {"V": value}
    assert tomllib.loads(SS.toml_line("V", value + "\n", multiline=True)) == {"V": value + "\n"}


def test_a_missing_key_file_names_the_variable_not_the_path_or_text(tmp_path):
    write_env(tmp_path, runtime_values(), key_file="keys/PLANTEDmissing.p8")
    code, out, err = run(tmp_path)
    assert code == 1 and out == ""
    assert (
        "SNOWFLAKE_RO_PRIVATE_KEY_PATH (FileNotFoundError)" in err and "PLANTEDmissing" not in err
    )
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "PLANTEDmissing.p8").write_text("PLANTEDnotAkey", encoding="utf-8")
    code, out, err = run(tmp_path)
    assert (
        code == 1 and out == "" and "not a PEM private key" in err and "PLANTEDnotAkey" not in err
    )


def test_no_env_file_is_an_error(tmp_path):
    code, out, err = run(tmp_path)
    assert code == 1 and out == "" and "no .env" in err


@pytest.mark.safety
def test_stderr_names_come_from_the_toml_never_from_key_file_lines(tmp_path):
    """Verifier round: a key file line holding " = " never reaches stderr (the names were once
    read back from the output text line by line)."""
    write_env(tmp_path, runtime_values())
    (tmp_path / "keys").mkdir()
    text = "PLANTEDkeyLINE = PLANTEDrest\n" + pem_key()
    (tmp_path / "keys" / "ro.p8").write_text(text, encoding="utf-8")
    code, out, err = run(tmp_path)
    assert code == 0 and tomllib.loads(out)[SS.KEY_TEXT] == text
    assert "PLANTED" not in err
    assert err.endswith(f"wrote {len(P.SECRETS)} variables: {', '.join(P.SECRETS)}\n")
