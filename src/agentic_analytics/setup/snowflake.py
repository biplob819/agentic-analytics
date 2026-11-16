"""`agentic-analytics setup snowflake` (N1.7: SET-06; OWNER D-55).

1. Key pairs. Two unencrypted PKCS8 RSA-2048 private keys, `admin_key.p8` and `ro_key.p8`, in the
   key folder (default `~/.snowflake/agentic-analytics/`, mode 700), each written with mode 600
   by an exclusive create, so an existing key is never overwritten: it is read and reused, and
   its public half (`*.pub.txt`) is written only if missing.
2. The ACCOUNTADMIN bootstrap SQL, with the public keys only: the two key-pair service users
   (`AA_ADMIN_USER` with SYSADMIN and SECURITYADMIN, `AA_RO_USER`), the pack's database and
   schemas owned by SYSADMIN (so the admin user can load them), then the read-only role from
   `warehouse.roles.pack_role_sql` (checked by `role_sql_problems` first), USAGE on its
   warehouse for SYSADMIN, and a last row naming the account identifier. `CREATE ... IF NOT
   EXISTS` plus `ALTER USER ... SET RSA_PUBLIC_KEY` make it safe to run again.
3. By default the SQL is printed (and saved next to the keys) to paste into Snowsight. With
   `--run` it asks for the account, an ACCOUNTADMIN username, the password and an MFA code
   (hidden; Enter approves a Duo push instead), runs the SQL over `username_password_mfa`, writes
   SNOWFLAKE_ACCOUNT, the users, the key paths and the warehouse into `.env`, and checks that both
   users can log in with their key pair (`SNOWFLAKE_JWT`).

The password and MFA code are used for that one connection and never stored or printed; no
private key is ever printed.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_analytics.setup._common import SecretBox, SetupError
from agentic_analytics.setup.envfile import read_env_file, update_env_file
from agentic_analytics.warehouse.roles import SF_WAREHOUSE, pack_role_sql, role_sql_problems

DEFAULT_KEY_DIR = Path("~/.snowflake/agentic-analytics")
ADMIN_USER = "AA_ADMIN_USER"
RO_USER = "AA_RO_USER"
KEY_FILES = {ADMIN_USER: "admin_key", RO_USER: "ro_key"}
SQL_FILE = "bootstrap.sql"
KEY_BITS = 2048
KEY_MODE = 0o600
DIR_MODE = 0o700
_B64 = re.compile(r"^[A-Za-z0-9+/=]+$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,200}$")
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,255}$")
_create = os.open  # a byte-level create with an explicit mode: there is no text to encode

SNOWSIGHT_STEPS = (
    "Paste this SQL into Snowsight as ACCOUNTADMIN (Projects > Worksheets > + SQL Worksheet, then "
    "Run All). It holds public keys only."
)

Connect = Callable[..., Any]
Prompt = Callable[[str], str]


@dataclass(frozen=True)
class KeyPair:
    user: str
    private_path: Path
    public_b64: str
    created: bool


@dataclass(frozen=True)
class SnowflakeOptions:
    key_dir: Path = DEFAULT_KEY_DIR
    run: bool = False
    account: str | None = None
    pack: str = "uk_price_paid"


@dataclass
class SnowflakeResult:
    key_dir: Path
    keys: list[KeyPair]
    sql: str
    sql_path: Path
    ran: bool = False
    account: str | None = None
    env_lines: dict[str, str] = field(default_factory=dict)
    env_written: list[str] = field(default_factory=list)
    logins: dict[str, str] = field(default_factory=dict)

    def to_dict(self, *, include_sql: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": True,
            "engine": "snowflake",
            "key_dir": str(self.key_dir),
            "keys": {
                k.user: {"key_file": str(k.private_path), "created": k.created} for k in self.keys
            },
            "sql_path": str(self.sql_path),
            "ran": self.ran,
            "account": self.account,
            "env": dict(self.env_lines),
            "env_written": sorted(set(self.env_written)),
            "logins": dict(self.logins),
        }
        if include_sql:
            out["sql"] = self.sql
        return out


# ---- keys ------------------------------------------------------------------------------------


def _public_b64(private_key: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode("ascii")


def _write_new(path: Path, data: bytes) -> None:
    """Create `path` with mode 600; refuse if it exists (never overwrite a key)."""
    fd = _create(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_MODE)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, KEY_MODE)


def ensure_key_pair(key_dir: Path, user: str) -> KeyPair:
    """Reuse `<stem>.p8` if it exists, else generate it; write `<stem>.pub.txt` if missing."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    stem = KEY_FILES[user]
    private, public = key_dir / f"{stem}.p8", key_dir / f"{stem}.pub.txt"
    created = False
    if private.exists():
        try:
            key = serialization.load_pem_private_key(private.read_bytes(), password=None)
        except (ValueError, TypeError) as exc:
            raise SetupError(
                f"{private} exists but is not an unencrypted PEM private key "
                f"({type(exc).__name__}); move it away or use another --key-dir"
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise SetupError(f"{private} is not an RSA key; move it away or use another --key-dir")
    else:
        if public.exists():
            raise SetupError(
                f"{public} exists without {private.name}; move it away (a new key would not "
                "match it) or use another --key-dir"
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        _write_new(private, pem)
        created = True
    pub = _public_b64(key)
    if public.exists():
        on_disk = "".join(public.read_text(encoding="utf-8").split())
        on_disk = re.sub(r"-----(BEGIN|END) PUBLIC KEY-----", "", on_disk)
        if on_disk != pub:
            raise SetupError(f"{public} does not match {private.name}; move one of them away")
    else:
        _write_new(public, pub.encode("ascii"))
    return KeyPair(user=user, private_path=private, public_b64=pub, created=created)


def ensure_keys(key_dir: Path) -> list[KeyPair]:
    key_dir = key_dir.expanduser()
    key_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    return [ensure_key_pair(key_dir, user) for user in (ADMIN_USER, RO_USER)]


# ---- SQL -------------------------------------------------------------------------------------


def bootstrap_sql(keys: list[KeyPair], *, pack_name: str = "uk_price_paid") -> str:
    """The ACCOUNTADMIN bootstrap SQL for these key pairs (public keys only)."""
    from agentic_analytics.packs import PackError, load_pack

    by_user = {k.user: k.public_b64 for k in keys}
    for user, pub in by_user.items():
        if not _B64.match(pub):
            raise SetupError(f"the public key for {user} is not base64")
    try:
        pack = load_pack(pack_name).manifest
    except PackError as exc:
        raise SetupError(str(exc)) from exc
    role_sql = pack_role_sql(pack, "snowflake", user=RO_USER)
    problems = role_sql_problems(role_sql, "snowflake")
    if problems:  # never hand out role SQL that is not read-only
        raise SetupError("the role SQL is not read-only: " + "; ".join(problems))
    db, _, schema = pack.databases["snowflake"].partition(".")
    admin_pub, ro_pub = by_user[ADMIN_USER], by_user[RO_USER]
    lines = [
        "-- Agentic Analytics Snowflake bootstrap (OWNER D-55). Run once as ACCOUNTADMIN: in",
        "-- Snowsight (Projects > Worksheets > + SQL Worksheet, then Run All), or with",
        "-- `agentic-analytics setup snowflake --run`. Public keys only; safe to run again.",
        "USE ROLE ACCOUNTADMIN;",
        f"CREATE USER IF NOT EXISTS {ADMIN_USER} TYPE = SERVICE DEFAULT_ROLE = SYSADMIN",
        f"  RSA_PUBLIC_KEY = '{admin_pub}';",
        f"ALTER USER {ADMIN_USER} SET RSA_PUBLIC_KEY = '{admin_pub}';",
        f"GRANT ROLE SYSADMIN TO USER {ADMIN_USER};",
        f"GRANT ROLE SECURITYADMIN TO USER {ADMIN_USER};",
        "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE SYSADMIN;"
        "  -- metered-credit reconcile",
        f"CREATE USER IF NOT EXISTS {RO_USER} TYPE = SERVICE",
        f"  RSA_PUBLIC_KEY = '{ro_pub}';",
        f"ALTER USER {RO_USER} SET RSA_PUBLIC_KEY = '{ro_pub}';",
        "-- The pack's database and schemas, owned by SYSADMIN so the admin user can load them.",
        "USE ROLE SYSADMIN;",
        f"CREATE DATABASE IF NOT EXISTS {db};",
        f"CREATE SCHEMA IF NOT EXISTS {db}.{schema};",
        f"CREATE SCHEMA IF NOT EXISTS {db}.{schema}_PROBES;",
        "-- The read-only role (warehouse/roles.py), granted to the read-only user.",
        "USE ROLE ACCOUNTADMIN;",
        role_sql.rstrip("\n"),
        f"GRANT USAGE ON WAREHOUSE {SF_WAREHOUSE} TO ROLE SYSADMIN;  -- the loader's warehouse",
        "SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS SNOWFLAKE_ACCOUNT;",
    ]
    return "\n".join(lines) + "\n"


def env_lines(keys: list[KeyPair], account: str | None) -> dict[str, str]:
    """The non-secret `.env` lines this setup produces (account last, when known)."""
    paths = {k.user: str(k.private_path) for k in keys}
    out = {
        "SNOWFLAKE_ADMIN_USER": ADMIN_USER,
        "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH": paths[ADMIN_USER],
        "SNOWFLAKE_RO_USER": RO_USER,
        "SNOWFLAKE_RO_PRIVATE_KEY_PATH": paths[RO_USER],
        "SNOWFLAKE_WAREHOUSE": SF_WAREHOUSE,
    }
    if account:
        out["SNOWFLAKE_ACCOUNT"] = account
    return out


def clean_account(raw: str) -> str:
    """`https://ORG-ACCT.snowflakecomputing.com/...` or `org-acct` -> `org-acct`."""
    acct = raw.strip()
    acct = re.sub(r"^https?://", "", acct, flags=re.I)
    acct = acct.split("/")[0]
    acct = re.sub(r"\.snowflakecomputing\.com$", "", acct, flags=re.I)
    if not _ACCOUNT.match(acct):
        raise SetupError("that is not a Snowflake account identifier (for example ORG-ACCOUNT)")
    return acct


# ---- running it ------------------------------------------------------------------------------


def _run_bootstrap(
    sql: str,
    account: str,
    *,
    connect: Connect,
    prompt: Prompt,
    secret_prompt: Prompt,
    box: SecretBox,
    say: Callable[[str], None],
) -> str:
    user = prompt("Snowflake username (a user with ACCOUNTADMIN): ").strip()
    if not _USER.match(user):
        raise SetupError("that is not a Snowflake username")
    password = secret_prompt("Password (hidden): ")
    code = secret_prompt("MFA code (hidden; press Enter to approve a Duo push instead): ").strip()
    if not password:
        raise SetupError("no password given")
    box.add(password)
    box.add(code)
    params: dict[str, Any] = {
        "account": account,
        "user": user,
        "password": password,
        "role": "ACCOUNTADMIN",
        "authenticator": "username_password_mfa",
        "login_timeout": 120,
    }
    if code:
        params["passcode"] = code
    say("logging in to Snowflake as ACCOUNTADMIN (approve the Duo push if you pressed Enter)")
    try:
        conn = connect(**params)
    except Exception as exc:
        raise SetupError(
            box.scrub(f"Snowflake login failed ({type(exc).__name__}): {exc}")[:400]
        ) from None
    finally:
        password = ""
        params.clear()
    found = ""
    try:
        for cur in conn.execute_string(sql, remove_comments=True):
            desc = getattr(cur, "description", None) or ()
            if desc and str(desc[0][0]).upper() == "SNOWFLAKE_ACCOUNT":
                row = cur.fetchone()
                found = str(row[0]) if row else ""
    except Exception as exc:
        raise SetupError(
            box.scrub(f"the bootstrap SQL failed ({type(exc).__name__}): {exc}")[:500]
        ) from None
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    return found or account.upper()


def verify_key_logins(
    account: str, keys: list[KeyPair], *, connect: Connect, box: SecretBox
) -> dict[str, str]:
    """Log in as each key-pair user and read CURRENT_USER(); any failure is a SetupError."""
    out: dict[str, str] = {}
    for k in keys:
        try:
            conn = connect(
                account=account,
                user=k.user,
                authenticator="SNOWFLAKE_JWT",
                private_key_file=str(k.private_path),
                login_timeout=60,
            )
            try:
                row = conn.cursor().execute("SELECT CURRENT_USER()").fetchone()
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
        except Exception as exc:
            msg = f"key-pair login failed for {k.user} ({type(exc).__name__}): {exc}"
            raise SetupError(box.scrub(msg)[:400]) from None
        who = str(row[0]) if row else ""
        if who.upper() != k.user:
            raise SetupError(f"key-pair login for {k.user} came back as {who or 'nobody'}")
        out[k.user] = "ok"
    return out


def snowflake_connect(**params: Any) -> Any:  # pragma: no cover - the real connector
    import snowflake.connector

    return snowflake.connector.connect(**params)


def setup_snowflake(
    opts: SnowflakeOptions,
    *,
    env_path: Path,
    environ: Mapping[str, str],
    connect: Connect = snowflake_connect,
    prompt: Prompt = input,
    secret_prompt: Prompt | None = None,
    say: Callable[[str], None] = print,
    box: SecretBox | None = None,
) -> SnowflakeResult:
    """Make the key pairs and the bootstrap SQL; with `opts.run`, run it and fill in `.env`."""
    if secret_prompt is None:
        import getpass

        secret_prompt = getpass.getpass
    box = box or SecretBox.from_environ(environ)
    key_dir = opts.key_dir.expanduser()
    keys = ensure_keys(key_dir)
    for k in keys:
        say(
            box.scrub(
                f"{'created' if k.created else 'reusing'} key pair for {k.user}: {k.private_path}"
            )
        )
    sql = bootstrap_sql(keys, pack_name=opts.pack)
    sql_path = key_dir / SQL_FILE
    tmp = sql_path.with_name(f".{SQL_FILE}.tmp")
    tmp.write_text(sql, encoding="utf-8")
    os.chmod(tmp, KEY_MODE)
    os.replace(tmp, sql_path)
    result = SnowflakeResult(key_dir=key_dir, keys=keys, sql=sql, sql_path=sql_path)
    if not opts.run:
        result.env_lines = env_lines(keys, None)
        return result
    env = read_env_file(env_path)
    env.update({k: v for k, v in environ.items() if v and v.strip()})
    raw = (
        opts.account
        or env.get("SNOWFLAKE_ACCOUNT")
        or prompt(
            "Snowflake account identifier, e.g. ORG-ACCOUNT (Snowsight > your name > Connect a "
            "tool to Snowflake): "
        )
    )
    account = clean_account(raw)
    found = _run_bootstrap(
        sql, account, connect=connect, prompt=prompt, secret_prompt=secret_prompt, box=box, say=say
    )
    account = clean_account(found)
    result.ran, result.account = True, account
    result.env_lines = env_lines(keys, account)
    result.env_written = update_env_file(env_path, result.env_lines)
    say(f"bootstrap SQL ran; SNOWFLAKE_ACCOUNT={account} and the key paths are saved in .env")
    result.logins = verify_key_logins(account, keys, connect=connect, box=box)
    for user in result.logins:
        say(f"key-pair login ok: {user}")
    return result


__all__ = [
    "ADMIN_USER",
    "DEFAULT_KEY_DIR",
    "RO_USER",
    "SNOWSIGHT_STEPS",
    "KeyPair",
    "SnowflakeOptions",
    "SnowflakeResult",
    "bootstrap_sql",
    "clean_account",
    "ensure_key_pair",
    "ensure_keys",
    "env_lines",
    "setup_snowflake",
    "verify_key_logins",
]
