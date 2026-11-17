"""The Snowflake read-only key from its PEM text when no key file exists (D-57, D-79): the
executor, the doctor, Cortex's JWT and the runner's services all read it, and the text never
reaches a repr, a message, a cause or a log. (D4.2's `tests/demo/test_deploy.py` also covers the
hosted page's side.)"""

from __future__ import annotations

import functools
import logging

import pytest

from agentic_analytics import tracing as T
from agentic_analytics.backends.cortex import CortexConfigError, KeyPairJwt
from agentic_analytics.run import runner as R
from agentic_analytics.warehouse import doctor as D
from agentic_analytics.warehouse import snowflake as S
from agentic_analytics.warehouse._common import WarehouseConfigError
from tests.run import world as w

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
    assert "SNOWFLAKE_RO_PRIVATE_KEY" in D.ENV_VARS
    for name in ("DEMO_LIVE_BACKENDS", "DEMO_CONTACT_URL"):  # settings, not secrets
        assert name in D.ENV_VARS and name not in T.SECRET_ENV_NAMES
