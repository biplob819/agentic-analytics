"""CTX-01: key-pair JWT, request shape, timeout, retries and config errors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import traceback

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agentic_analytics.backends.cortex import (
    MAX_JWT_LIFETIME_S,
    CortexClient,
    CortexConfigError,
    CortexHTTPError,
    KeyPairJwt,
    account_host,
    jwt_account,
    public_key_fingerprint,
)
from agentic_analytics.models.errors import classify_error
from tests.backends.cortex_fakes import (
    ACCOUNT,
    URL,
    USER,
    VIEW,
    Api,
    Clock,
    Hang,
    broken_body,
    error,
    garbled,
    make_client,
    make_jwt,
    other_rsa_key,
    rsa_key,
    run,
    sql_body,
    ticks,
)

QUESTION = "What was the median price in Manchester in 2024?"


def _fingerprint_independently(key) -> str:
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def _decode(token: str, key=None) -> dict:
    pub = (key or rsa_key()).public_key()
    return jwt.decode(
        token, pub, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )


# --- JWT ---------------------------------------------------------------------------------------


def test_jwt_claims_follow_snowflake_key_pair_format():
    clock = Clock(1_800_000_000.0)
    token = make_jwt(clock).token()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = _decode(token)
    assert claims["sub"] == "MYORG-MYACCOUNT.AA_RO"
    assert claims["iss"] == f"MYORG-MYACCOUNT.AA_RO.{_fingerprint_independently(rsa_key())}"
    assert claims["iat"] == 1_800_000_000
    assert claims["exp"] - claims["iat"] == 3540
    assert claims["exp"] - claims["iat"] <= 3600


def test_jwt_is_signed_by_the_given_key_only():
    token = make_jwt().token()
    with pytest.raises(jwt.InvalidSignatureError):
        _decode(token, other_rsa_key())


ACCOUNT_FORMS = [
    # (SNOWFLAKE_ACCOUNT, JWT account, hostname label)
    ("xy12345", "XY12345", "xy12345"),
    (" xy12345 ", "XY12345", "xy12345"),
    ("xy12345.us-east-2.aws", "XY12345", "xy12345.us-east-2.aws"),
    ("XY12345.eu-west-1", "XY12345", "xy12345.eu-west-1"),
    ("myorg-myaccount", "MYORG-MYACCOUNT", "myorg-myaccount"),
    ("myorg-myaccount.privatelink", "MYORG-MYACCOUNT", "myorg-myaccount.privatelink"),
    ("myorg-myaccount.global", "MYORG-MYACCOUNT", "myorg-myaccount.global"),
    ("my_org-my_acct", "MY_ORG-MY_ACCT", "my-org-my-acct"),
]


@pytest.mark.parametrize(("account", "jwt_acct", "host"), ACCOUNT_FORMS)
def test_account_forms_for_jwt_and_hostname(account, jwt_acct, host):
    assert jwt_account(account) == jwt_acct
    assert account_host(account) == host
    j = KeyPairJwt(account, "Aa_Ro", rsa_key(), clock=Clock())
    claims = _decode(j.token())
    assert claims["sub"] == f"{jwt_acct}.AA_RO"
    assert claims["iss"] == f"{jwt_acct}.AA_RO.{_fingerprint_independently(rsa_key())}"
    c = CortexClient(account, VIEW, j)
    assert c.url == f"https://{host}.snowflakecomputing.com/api/v2/cortex/analyst/message"


def test_fingerprint_matches_independent_computation_and_differs_per_key():
    assert public_key_fingerprint(rsa_key()) == _fingerprint_independently(rsa_key())
    assert public_key_fingerprint(rsa_key()) != public_key_fingerprint(other_rsa_key())


def test_jwt_cached_until_refresh_window_boundary():
    clock = Clock(1_800_000_000.0)
    j = make_jwt(clock)
    first = j.token()
    exp = j.expires_at
    assert exp == 1_800_000_000 + 3540
    clock.now = exp - 301  # one second outside the refresh window: cached
    assert j.token() == first
    assert j.minted == 1
    clock.now = exp - 300  # exactly at the window: minted again
    second = j.token()
    assert second != first
    assert j.minted == 2
    assert _decode(second)["iat"] == exp - 300
    assert j.token() == second  # and cached again


def test_jwt_after_expiry_is_never_reused():
    clock = Clock(1_800_000_000.0)
    j = make_jwt(clock)
    first = j.token()
    clock.now += 10_000
    assert j.token() != first


@pytest.mark.parametrize(
    ("lifetime", "refresh"), [(3601, 300), (3600, 3600), (600, 0), (600, 700), (300, -1)]
)
def test_jwt_lifetime_and_refresh_limits_refused(lifetime, refresh):
    with pytest.raises(ValueError):
        make_jwt(lifetime_s=lifetime, refresh_s=refresh)


def test_jwt_lifetime_of_exactly_one_hour_allowed():
    j = make_jwt(lifetime_s=MAX_JWT_LIFETIME_S, refresh_s=60)
    claims = _decode(j.token())
    assert claims["exp"] - claims["iat"] == 3600


def test_key_from_pem_der_and_encrypted_pem(tmp_path):
    key = rsa_key()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    enc = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"s3cret"),
    )
    expected = KeyPairJwt(ACCOUNT, USER, key, clock=Clock()).issuer
    assert KeyPairJwt(ACCOUNT, USER, pem, clock=Clock()).issuer == expected
    assert KeyPairJwt(ACCOUNT, USER, der, clock=Clock()).issuer == expected
    path = tmp_path / "rsa_key.p8"
    path.write_bytes(enc)
    j = KeyPairJwt.from_key_file(ACCOUNT, USER, path, passphrase="s3cret", clock=Clock())
    assert j.issuer == expected


def test_bad_key_material_is_a_config_error(tmp_path):
    ec_pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    enc = rsa_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"right"),
    )
    with pytest.raises(CortexConfigError, match="could not load the private key named by"):
        KeyPairJwt(ACCOUNT, USER, b"not a key", clock=Clock())
    with pytest.raises(CortexConfigError, match="must be RSA"):
        KeyPairJwt(ACCOUNT, USER, ec_pem, clock=Clock())
    with pytest.raises(CortexConfigError, match="could not load"):
        KeyPairJwt(ACCOUNT, USER, enc, passphrase="wrong", clock=Clock())
    with pytest.raises(CortexConfigError, match="SNOWFLAKE_RO_PRIVATE_KEY_PATH"):
        KeyPairJwt.from_key_file(ACCOUNT, USER, tmp_path / "missing.p8", clock=Clock())
    with pytest.raises(CortexConfigError, match="account"):
        KeyPairJwt(" ", USER, rsa_key(), clock=Clock())
    with pytest.raises(CortexConfigError, match="account"):
        KeyPairJwt(".us-east-1", USER, rsa_key(), clock=Clock())
    with pytest.raises(CortexConfigError, match="user"):
        KeyPairJwt(ACCOUNT, "", rsa_key(), clock=Clock())


def test_reprs_never_show_the_key_or_token():
    j = make_jwt()
    token = j.token()
    c = make_client(Api([]), jwt_source=j)
    for text in (repr(j), repr(c)):
        assert token not in text
        assert "PRIVATE" not in text


# --- request shape -----------------------------------------------------------------------------


def test_request_url_headers_and_body():
    j = make_jwt()
    out, api, _ = run(sql_body(), jwt_source=j)
    assert out.cortex_messages == 1
    [req] = api.requests
    assert req.method == "POST"
    assert str(req.url) == URL
    assert req.headers["authorization"] == f"Bearer {j.token()}"
    assert req.headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"
    assert req.headers["content-type"] == "application/json"
    assert _decode(req.headers["authorization"].removeprefix("Bearer "))["sub"] == (
        "MYORG-MYACCOUNT.AA_RO"
    )
    assert json.loads(req.content) == {
        "messages": [{"role": "user", "content": [{"type": "text", "text": QUESTION}]}],
        "semantic_view": VIEW,
    }


def test_request_carries_the_timeout():
    _, api, _ = run(sql_body(), timeout_s=12.5)
    assert api.requests[0].extensions["timeout"] == {
        "connect": 12.5,
        "read": 12.5,
        "write": 12.5,
        "pool": 12.5,
    }


def test_base_url_override_and_account_host():
    c = CortexClient(ACCOUNT, VIEW, make_jwt(), base_url="https://proxy.example/")
    assert c.url == "https://proxy.example/api/v2/cortex/analyst/message"
    c = CortexClient("MyOrg-Acct", VIEW, make_jwt())
    assert c.url == "https://myorg-acct.snowflakecomputing.com/api/v2/cortex/analyst/message"


@pytest.mark.parametrize(
    "view", ["ANALYTICS.UK", "A.B.C.D", "", "ANALYTICS..SV", "A.B.C;DROP", "1A.B.C"]
)
def test_semantic_view_must_be_database_schema_view(view):
    with pytest.raises(CortexConfigError, match=r"DATABASE\.SCHEMA\.VIEW"):
        CortexClient(ACCOUNT, view, make_jwt())


def test_semantic_view_quoted_identifiers_allowed():
    CortexClient(ACCOUNT, 'ANALYTICS."uk data".PRICE_PAID_SV', make_jwt())


def test_timeout_must_be_positive():
    with pytest.raises(ValueError):
        CortexClient(ACCOUNT, VIEW, make_jwt(), timeout_s=0)


# --- retries -----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_transient_status_retried_then_succeeds(status):
    out, api, _ = run(error(status), error(status), sql_body())
    assert len(api.requests) == 3
    assert out.answer.error is None
    assert out.cortex_messages == 1


def test_exactly_six_attempts_then_transport():
    out, api, ex = run(*[error(429)] * 7)
    assert len(api.requests) == 6
    assert len(api.script) == 1  # the seventh reply was never asked for
    assert out.answer.error == "transport"
    assert out.answer.sql is None
    assert out.answer.answer == ""
    assert out.cortex_messages == 0
    assert out.warehouse_elapsed_ms == 0
    assert ex.calls == []


def test_sixth_attempt_success_counts():
    out, api, _ = run(*[error(503)] * 5, sql_body())
    assert len(api.requests) == 6
    assert out.answer.error is None


def test_failures_before_sending_are_retried():
    out, api, _ = run(
        httpx2.ConnectError("refused"),
        httpx2.ConnectTimeout("slow handshake"),
        httpx2.PoolTimeout("pool"),
        httpx2.WriteError("write"),
        httpx2.WriteTimeout("write"),
        sql_body(),
    )
    assert len(api.requests) == 6
    assert out.answer.error is None
    assert out.cortex_messages == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ReadError("reset after send"),
        httpx2.RemoteProtocolError("server disconnected"),
        broken_body(httpx2.ReadError("reset mid-body")),
        broken_body(httpx2.RemoteProtocolError("peer closed mid-body")),
    ],
)
def test_failures_after_sending_not_retried_and_counted_as_billed(failure):
    out, api, ex = run(failure, sql_body())
    assert len(api.requests) == 1
    assert out.answer.error == "transport"
    assert out.answer.sql is None
    assert out.answer.answer == ""
    assert out.cortex_messages == 1
    assert ex.calls == []


def test_read_timeout_mid_body_is_a_billed_timeout():
    out, api, _ = run(broken_body(httpx2.ReadTimeout("slow body")), sql_body())
    assert len(api.requests) == 1
    assert out.answer.error == "timeout"
    assert out.cortex_messages == 1


def test_undecodable_200_body_is_a_billed_schema_error():
    out, api, ex = run(garbled(200), sql_body())
    assert len(api.requests) == 1
    assert out.answer.error == "schema"
    assert out.answer.sql is None
    assert out.cortex_messages == 1
    assert ex.calls == []


def test_undecodable_error_bodies_keep_their_status():
    out, api, _ = run(garbled(503), sql_body())
    assert len(api.requests) == 2  # 503 still retried
    assert out.answer.error is None
    with pytest.raises(CortexHTTPError, match="HTTP 401: no error body"):
        run(garbled(401))


def test_too_many_redirects_is_a_config_error():
    with pytest.raises(CortexConfigError, match=r"too many redirects.*SNOWFLAKE_ACCOUNT"):
        run(httpx2.TooManyRedirects("loop"), sql_body())


def test_a_redirect_status_is_a_config_error_naming_the_account():
    with pytest.raises(CortexHTTPError, match="redirect: check SNOWFLAKE_ACCOUNT") as info:
        run(httpx2.Response(302, headers={"Location": "https://elsewhere/"}))
    assert classify_error(info.value) == "config"


def test_unsupported_protocol_is_a_config_error():
    with pytest.raises(CortexConfigError, match="UnsupportedProtocol"):
        run(httpx2.UnsupportedProtocol("ftp"))


def test_total_deadline_cuts_a_hung_request_and_counts_it():
    out, api, _ = run(Hang(5.0), deadline_s=0.05, clock=ticks(1.0, 1.06))
    assert len(api.requests) == 1
    assert out.answer.error == "timeout"
    assert out.answer.sql is None
    assert out.cortex_messages == 1  # the request was in flight: maybe billed
    assert out.api_latency_ms == 60


def test_total_deadline_during_backoff_counts_nothing():
    async def slow_sleep(_: float) -> None:
        await asyncio.sleep(5.0)

    out, api, _ = run(error(503), sql_body(), deadline_s=0.05, sleep=slow_sleep)
    assert len(api.requests) == 1
    assert out.answer.error == "timeout"
    assert out.cortex_messages == 0


def test_total_deadline_spans_retries():
    out, api, _ = run(error(503), Hang(5.0), deadline_s=0.05)
    assert len(api.requests) == 2
    assert out.answer.error == "timeout"


def test_deadline_must_be_positive():
    with pytest.raises(ValueError):
        CortexClient(ACCOUNT, VIEW, make_jwt(), deadline_s=0)


def test_latency_recorded_on_transport_and_timeout_paths():
    out, _, _ = run(*[error(429)] * 6, clock=ticks(1.0, 3.5))
    assert out.answer.error == "transport"
    assert out.api_latency_ms == 2500
    out, _, _ = run(httpx2.ReadTimeout("slow"), clock=ticks(2.0, 2.75))
    assert out.answer.error == "timeout"
    assert out.api_latency_ms == 750
    out, _, _ = run(httpx2.ReadError("lost"), clock=ticks(2.0, 2.125))
    assert out.api_latency_ms == 125


def test_connection_failures_outlasting_retries_are_transport():
    out, api, _ = run(*[httpx2.ConnectError("refused")] * 6)
    assert len(api.requests) == 6
    assert out.answer.error == "transport"
    assert out.cortex_messages == 0


def test_backoff_sleeps_between_attempts_only():
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    run(*[error(500)] * 6, sleep=sleep)
    assert len(slept) == 5
    assert all(0 <= s <= 30 for s in slept)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 415, 422])
def test_client_errors_raise_config_without_retry(status):
    j = make_jwt()
    with pytest.raises(CortexHTTPError) as info:
        run(error(status, message="Invalid semantic view"), sql_body(), jwt_source=j)
    exc = info.value
    assert exc.status_code == status
    assert classify_error(exc) == "config"
    assert "Invalid semantic view" in str(exc)
    assert f"err-{status}" in str(exc)
    assert j.token() not in str(exc)


def test_client_error_raised_after_one_request():
    api = Api([error(401), sql_body()])
    client = make_client(api)
    with pytest.raises(CortexHTTPError):
        asyncio.run(client.send(QUESTION))
    assert len(api.requests) == 1


def test_error_body_that_is_not_json_still_raises_cleanly():
    with pytest.raises(CortexHTTPError, match="HTTP 403: no error body"):
        run(httpx2.Response(403, content=b"<html>Forbidden</html>"))


def test_token_refreshed_between_retries():
    clock = Clock(1_800_000_000.0)
    j = make_jwt(clock)

    async def sleep(_: float) -> None:
        clock.now += 3400  # a long backoff crosses the refresh window

    _, api, _ = run(error(503), sql_body(), jwt_source=j, sleep=sleep)
    tokens = [r.headers["authorization"] for r in api.requests]
    assert len(tokens) == 2
    assert tokens[0] != tokens[1]
    assert j.minted == 2


def test_read_timeout_not_retried_gives_timeout():
    out, api, ex = run(httpx2.ReadTimeout("slow"), sql_body())
    assert len(api.requests) == 1
    assert out.answer.error == "timeout"
    assert out.answer.sql is None
    assert out.answer.answer == ""
    assert out.cortex_messages == 1  # the request was sent: counted for cost
    assert ex.calls == []


# --- secrets never reach an exception ----------------------------------------------------------


def _all_text(exc: BaseException) -> str:
    """Every exception message a traceback would print for `exc`, its cause and its shown
    context (source lines are left out: they would quote this test's own literals)."""
    parts: list[str] = []
    seen: BaseException | None = exc
    while seen is not None:
        parts += [*traceback.format_exception_only(seen), repr(seen)]
        if seen.__cause__ is not None:
            seen = seen.__cause__
        elif not seen.__suppress_context__:
            seen = seen.__context__
        else:
            seen = None
    return "".join(parts)


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        (
            lambda t: httpx2.ConnectError(f"refused; Authorization: Bearer {t}"),
            "CortexConnectionError",
        ),
        (lambda t: httpx2.ReadTimeout(f"slow; Bearer {t}"), "CortexTimeout"),
        (lambda t: httpx2.ReadError(f"reset; Bearer {t}"), "CortexInterrupted"),
        (lambda t: httpx2.RemoteProtocolError(f"closed; {t}"), "CortexInterrupted"),
        (lambda t: broken_body(httpx2.ReadError(f"mid-body {t}")), "CortexInterrupted"),
        (lambda t: httpx2.TooManyRedirects(f"loop {t}"), "CortexConfigError"),
        (lambda t: httpx2.UnsupportedProtocol(f"bad {t}"), "CortexConfigError"),
    ],
)
def test_the_token_never_reaches_an_exception(failure, raised):
    j = make_jwt()
    token = j.token()
    api = Api([failure(token)] * 6)
    client = make_client(api, jwt_source=j)
    with pytest.raises(Exception) as info:
        asyncio.run(client.send(QUESTION))
    assert type(info.value).__name__ == raised
    assert token not in _all_text(info.value)
    assert "Bearer" not in _all_text(info.value)


def test_the_token_never_reaches_an_http_error():
    j = make_jwt()
    token = j.token()
    with pytest.raises(CortexHTTPError) as info:
        run(error(401, message="JWT token is invalid"), jwt_source=j)
    assert token not in _all_text(info.value)


def test_the_passphrase_and_key_bytes_never_reach_an_exception():
    enc = rsa_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"right-passphrase"),
    )
    with pytest.raises(CortexConfigError) as info:
        KeyPairJwt(ACCOUNT, USER, enc, passphrase="PLANTED-passphrase-77", clock=Clock())
    assert "PLANTED-passphrase-77" not in _all_text(info.value)
    with pytest.raises(CortexConfigError) as info:
        KeyPairJwt(ACCOUNT, USER, b"PLANTED-key-bytes-42", clock=Clock())
    assert "PLANTED-key-bytes-42" not in _all_text(info.value)


def test_the_key_path_never_reaches_an_exception(tmp_path):
    missing = tmp_path / "PLANTED-secret-dir" / "rsa_key.p8"
    with pytest.raises(CortexConfigError) as info:
        KeyPairJwt.from_key_file(ACCOUNT, USER, missing, clock=Clock())
    text = _all_text(info.value)
    assert "PLANTED-secret-dir" not in text
    assert "could not read the private key named by SNOWFLAKE_RO_PRIVATE_KEY_PATH" in text
    junk = tmp_path / "PLANTED-junk-dir"
    junk.mkdir()
    (junk / "k.p8").write_bytes(b"junk")
    with pytest.raises(CortexConfigError) as info:
        KeyPairJwt.from_key_file(ACCOUNT, USER, junk / "k.p8", passphrase="pw", clock=Clock())
    assert "PLANTED-junk-dir" not in _all_text(info.value)


def test_unexpected_exception_stops_the_run():
    with pytest.raises(KeyError):
        run(KeyError("bug in the transport"))
