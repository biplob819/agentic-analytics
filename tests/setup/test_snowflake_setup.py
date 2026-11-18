"""SET-06: `setup snowflake` on a fake connector; keys in a temporary folder only."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agentic_analytics.setup import SecretBox, SetupError, read_env_file
from agentic_analytics.setup import snowflake as sfs
from agentic_analytics.setup.snowflake import (
    ADMIN_USER,
    RO_USER,
    SnowflakeOptions,
    bootstrap_sql,
    clean_account,
    ensure_keys,
    setup_snowflake,
)
from agentic_analytics.warehouse.roles import role_sql_problems
from tests.file_modes import mode_problems
from tests.setup.fakes import SF_PASSCODE, SF_PASSWORD, FakeSnowflake


class Prompts:
    def __init__(self, *answers: str, secrets: tuple[str, ...] = (SF_PASSWORD, SF_PASSCODE)):
        self.answers, self.secrets = list(answers), list(secrets)
        self.asked: list[str] = []
        self.asked_hidden: list[str] = []

    def prompt(self, q: str) -> str:
        self.asked.append(q)
        return self.answers.pop(0)

    def secret(self, q: str) -> str:
        self.asked_hidden.append(q)
        return self.secrets.pop(0)


def _run(
    tmp_path: Path,
    fake: FakeSnowflake | None = None,
    prompts: Prompts | None = None,
    *,
    run: bool = True,
    account: str | None = None,
) -> tuple[sfs.SnowflakeResult, list[str], FakeSnowflake, Prompts]:
    fake = fake or FakeSnowflake()
    prompts = prompts or Prompts("myorg-ab12345", "owner_admin")
    lines: list[str] = []
    result = setup_snowflake(
        SnowflakeOptions(key_dir=tmp_path / "keys", run=run, account=account),
        env_path=tmp_path / ".env",
        environ={},
        connect=fake,
        prompt=prompts.prompt,
        secret_prompt=prompts.secret,
        say=lines.append,
    )
    return result, lines, fake, prompts


def _private_bodies(key_dir: Path) -> list[str]:
    """Every base64 line of every private key: none may appear in any output."""
    out = []
    for p in key_dir.glob("*.p8"):
        out += [ln for ln in p.read_text(encoding="utf-8").splitlines() if "-----" not in ln]
    return out


def test_print_mode_generates_keys_and_prints_sql_with_public_keys(tmp_path: Path) -> None:
    result, _lines, fake, _ = _run(tmp_path, run=False)
    assert not result.ran and fake.connects == [] and not (tmp_path / ".env").exists()
    keys = {k.user: k for k in result.keys}
    assert set(keys) == {ADMIN_USER, RO_USER} and all(k.created for k in keys.values())
    for k in keys.values():
        der = base64.b64decode(k.public_b64)
        assert len(der) == 294  # RSA-2048 SubjectPublicKeyInfo
        assert f"RSA_PUBLIC_KEY = '{k.public_b64}'" in result.sql
        assert (k.private_path.parent / f"{k.private_path.stem}.pub.txt").read_text(
            encoding="utf-8"
        ) == k.public_b64
        pem = k.private_path.read_text(encoding="utf-8")
        assert pem.startswith("-----BEGIN " + "PRIVATE KEY-----")  # PKCS8, unencrypted
    assert result.sql_path.read_text(encoding="utf-8") == result.sql
    assert result.sql.splitlines()[3] == "USE ROLE ACCOUNTADMIN;"
    assert result.sql.rstrip().endswith("AS SNOWFLAKE_ACCOUNT;")
    assert "CREATE ROLE IF NOT EXISTS ANALYTICS_RO;" in result.sql
    assert "GRANT ROLE ANALYTICS_RO TO USER AA_RO_USER;" in result.sql
    assert result.env_lines["SNOWFLAKE_RO_PRIVATE_KEY_PATH"] == str(keys[RO_USER].private_path)
    assert "SNOWFLAKE_ACCOUNT" not in result.env_lines


def test_key_files_and_folder_are_private(tmp_path: Path) -> None:
    keys = ensure_keys(tmp_path / "keys")
    assert mode_problems(tmp_path / "keys", 0o700) == []
    for k in keys:
        assert mode_problems(k.private_path, 0o600) == []


def test_existing_keys_are_never_overwritten(tmp_path: Path) -> None:
    first = ensure_keys(tmp_path / "keys")
    before = {k.private_path: k.private_path.read_bytes() for k in first}
    second = ensure_keys(tmp_path / "keys")
    assert [k.created for k in second] == [False, False]
    assert {k.private_path: k.private_path.read_bytes() for k in second} == before
    assert [k.public_b64 for k in second] == [k.public_b64 for k in first]
    # a missing public half is rewritten from the private key, never the reverse
    pub = tmp_path / "keys" / "ro_key.pub.txt"
    pub.unlink()
    third = ensure_keys(tmp_path / "keys")
    assert pub.read_text(encoding="utf-8") == third[1].public_b64 == first[1].public_b64


def test_writer_refuses_to_replace_a_file(tmp_path: Path) -> None:
    target = tmp_path / "admin_key.p8"
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sfs._write_new(target, b"new")
    assert target.read_text(encoding="utf-8") == "existing"


def test_broken_or_orphaned_keys_are_refused(tmp_path: Path) -> None:
    d = tmp_path / "keys"
    d.mkdir()
    (d / "admin_key.p8").write_text("not a key", encoding="utf-8")
    with pytest.raises(SetupError, match="not an unencrypted PEM private key"):
        ensure_keys(d)
    (d / "admin_key.p8").unlink()
    (d / "admin_key.pub.txt").write_text("AAAA", encoding="utf-8")
    with pytest.raises(SetupError, match=r"without admin_key\.p8"):
        ensure_keys(d)
    assert not (d / "admin_key.p8").exists()


def test_mismatched_public_key_is_refused(tmp_path: Path) -> None:
    ensure_keys(tmp_path / "keys")
    (tmp_path / "keys" / "ro_key.pub.txt").write_text("QUJD", encoding="utf-8")
    with pytest.raises(SetupError, match="does not match"):
        ensure_keys(tmp_path / "keys")


def test_bootstrap_sql_role_part_is_read_only(tmp_path: Path) -> None:
    keys = ensure_keys(tmp_path / "keys")
    sql = bootstrap_sql(keys)
    role_part = sql[
        sql.index("CREATE ROLE IF NOT EXISTS") : sql.index(
            "GRANT USAGE ON WAREHOUSE ANALYTICS_XS TO ROLE SYSADMIN"
        )
    ]
    assert role_sql_problems(role_part, "snowflake") == []
    assert "SYSADMIN" not in role_part
    for stmt in (
        "CREATE DATABASE IF NOT EXISTS ANALYTICS;",
        "CREATE SCHEMA IF NOT EXISTS ANALYTICS.UK;",
    ):
        assert sql.index("USE ROLE SYSADMIN;") < sql.index(stmt) < sql.index("CREATE ROLE")


def test_bootstrap_sql_refuses_a_widened_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = ensure_keys(tmp_path / "keys")
    real = sfs.pack_role_sql
    monkeypatch.setattr(
        sfs,
        "pack_role_sql",
        lambda *a, **k: real(*a, **k).replace(
            "GRANT SELECT ON ALL TABLES", "GRANT INSERT ON ALL TABLES"
        ),
    )
    with pytest.raises(SetupError, match="not read-only"):
        bootstrap_sql(keys)


def test_run_uses_mfa_authenticator_then_key_pair_logins(tmp_path: Path) -> None:
    result, _lines, fake, prompts = _run(tmp_path)
    admin_login, *key_logins = fake.connects
    assert admin_login == {
        "account": "myorg-ab12345",
        "user": "owner_admin",
        "password": SF_PASSWORD,
        "role": "ACCOUNTADMIN",
        "authenticator": "username_password_mfa",
        "login_timeout": 120,
        "passcode": SF_PASSCODE,
    }
    assert fake.scripts == [result.sql]
    assert [(c["user"], c["authenticator"], c["account"]) for c in key_logins] == [
        (ADMIN_USER, "SNOWFLAKE_JWT", "MYORG-AB12345"),
        (RO_USER, "SNOWFLAKE_JWT", "MYORG-AB12345"),
    ]
    assert key_logins[0]["private_key_file"] == str(result.keys[0].private_path)
    assert "password" not in key_logins[0]
    assert result.logins == {ADMIN_USER: "ok", RO_USER: "ok"}
    env = read_env_file(tmp_path / ".env")
    assert env == {
        "SNOWFLAKE_ADMIN_USER": ADMIN_USER,
        "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH": str(result.keys[0].private_path),
        "SNOWFLAKE_RO_USER": RO_USER,
        "SNOWFLAKE_RO_PRIVATE_KEY_PATH": str(result.keys[1].private_path),
        "SNOWFLAKE_WAREHOUSE": "ANALYTICS_XS",
        "SNOWFLAKE_ACCOUNT": "MYORG-AB12345",
    }
    assert mode_problems(tmp_path / ".env", 0o600) == []
    assert len(prompts.asked_hidden) == 2 and "Password" in prompts.asked_hidden[0]
    assert "Duo push" in prompts.asked_hidden[1]


def test_enter_means_duo_push_no_passcode(tmp_path: Path) -> None:
    prompts = Prompts("owner_admin", secrets=(SF_PASSWORD, ""))
    _result, _lines, fake, _ = _run(
        tmp_path, prompts=prompts, account="https://myorg-ab12345.snowflakecomputing.com/"
    )
    assert "passcode" not in fake.connects[0]
    assert fake.connects[0]["account"] == "myorg-ab12345"
    assert len(prompts.asked) == 1  # the account came from --account


def test_existing_env_lines_are_kept(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "# mine\nANTHROPIC_API_KEY=keep\nSNOWFLAKE_ACCOUNT=old\n", encoding="utf-8"
    )
    prompts = Prompts("owner_admin")  # the account is read from .env, no prompt for it
    _run(tmp_path, prompts=prompts)
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert text.startswith("# mine\nANTHROPIC_API_KEY=keep\nSNOWFLAKE_ACCOUNT=MYORG-AB12345\n")
    assert prompts.asked == ["Snowflake username (a user with ACCOUNTADMIN): "]


@pytest.mark.safety
def test_password_mfa_and_private_keys_never_printed(tmp_path: Path) -> None:
    result, lines, _fake, _ = _run(tmp_path)
    out = "\n".join(lines) + str(result.to_dict())
    assert SF_PASSWORD not in out and SF_PASSCODE not in out
    for body in _private_bodies(tmp_path / "keys"):
        assert body not in out and body not in result.sql
    assert "PRIVATE KEY" not in out
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert SF_PASSWORD not in env_text and SF_PASSCODE not in env_text


@pytest.mark.safety
def test_login_failure_is_scrubbed(tmp_path: Path) -> None:
    fake = FakeSnowflake(fail_login=RuntimeError(f"bad password {SF_PASSWORD} / {SF_PASSCODE}"))
    with pytest.raises(SetupError) as err:
        _run(tmp_path, fake)
    msg = str(err.value)
    assert SF_PASSWORD not in msg and SF_PASSCODE not in msg and "login failed" in msg
    assert not (tmp_path / ".env").exists()


def test_key_login_failure_stops_with_the_user_named(tmp_path: Path) -> None:
    fake = FakeSnowflake(fail_key_login=RuntimeError("JWT token is invalid"))
    with pytest.raises(SetupError, match="key-pair login failed for AA_ADMIN_USER"):
        _run(tmp_path, fake)
    assert read_env_file(tmp_path / ".env")["SNOWFLAKE_ACCOUNT"] == "MYORG-AB12345"


def test_empty_password_is_refused_before_connecting(tmp_path: Path) -> None:
    fake = FakeSnowflake()
    with pytest.raises(SetupError, match="no password"):
        _run(tmp_path, fake, Prompts("myorg-ab12345", "u", secrets=("", "")))
    assert fake.connects == []


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("myorg-ab12345", "myorg-ab12345"),
        ("https://MYORG-AB12345.snowflakecomputing.com/console", "MYORG-AB12345"),
        (" xy12345.eu-west-1 ", "xy12345.eu-west-1"),
    ],
)
def test_clean_account(raw: str, want: str) -> None:
    assert clean_account(raw) == want


@pytest.mark.parametrize("raw", ["", "a'b", "x y", "acct;DROP"])
def test_clean_account_refuses_junk(raw: str) -> None:
    with pytest.raises(SetupError):
        clean_account(raw)


def test_secret_box_never_hides_key_paths() -> None:
    box = SecretBox.from_environ({"SNOWFLAKE_RO_PRIVATE_KEY_PATH": "/home/u/keys/ro_key.p8"})
    assert box.scrub("/home/u/keys/ro_key.p8") == "/home/u/keys/ro_key.p8"
