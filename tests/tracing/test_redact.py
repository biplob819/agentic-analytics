"""ANS-05: no key or auth header in any span or export (NFR4)."""

from __future__ import annotations

import json

import pytest

from agentic_analytics.tracing import (
    SECRET_ENV_NAMES,
    SECRET_PATTERNS,
    Redactor,
)
from secret_scan import PATTERNS
from tests.tracing.helpers import (
    ANTHROPIC_KEY,
    AWS_KEY,
    BEARER,
    DB_URL,
    ENV_SECRET,
    GITHUB_TOKEN,
    LANGFUSE_SECRET,
    OPENAI_KEY,
    PEM,
    SECRETS,
    URL_PW,
)

pytestmark = pytest.mark.safety


def test_patterns_are_a_faithful_copy_of_the_secret_scanner():
    def shape(pats):
        return {name: (p.pattern, p.flags) for name, p in pats.items()}

    assert shape(SECRET_PATTERNS) == shape(PATTERNS)


@pytest.mark.parametrize(
    ("planted", "label"),
    [
        (ANTHROPIC_KEY, "anthropic key"),
        (OPENAI_KEY, "openai key"),
        (AWS_KEY, "aws access key"),
        (GITHUB_TOKEN, "github token"),
        (LANGFUSE_SECRET, "langfuse secret key"),
    ],
)
def test_each_scanner_pattern_is_scrubbed(planted, label):
    out = Redactor().text(f"x {planted} y")
    assert planted not in out
    assert out == f"x [REDACTED:{label}] y"


def test_url_password_is_scrubbed_but_the_url_kept():
    out = Redactor().text(f"dsn={DB_URL}")
    assert URL_PW not in out
    assert out == "dsn=postgres://app:" + "[REDACTED]@db.example.com/x"


def test_whole_private_key_block_is_scrubbed():
    out = Redactor().text(f"key:\n{PEM}\nend")
    assert "MIIEvQIBADANBg" not in out and "PRIVATE KEY" not in out
    assert out == "key:\n[REDACTED:private key]\nend"


def test_env_secret_values_are_scrubbed_by_name():
    r = Redactor(SECRETS)
    assert r.text(f"pw={ENV_SECRET};") == "pw=[REDACTED:CLICKHOUSE_RO_PASSWORD];"


def test_env_secrets_are_scrubbed_longest_first():
    r = Redactor({"A_KEY": "abcd1234", "B_KEY": "abcd1234ZZZZ"})
    assert r.text("abcd1234ZZZZ") == "[REDACTED:B_KEY]"


def test_empty_secret_values_are_ignored():
    assert Redactor({"A_KEY": "", "B_KEY": "   "}).text("abc and more") == "abc and more"


def test_short_secrets_are_scrubbed_only_as_whole_tokens():
    r = Redactor({"DEMO_PASSCODE": "482"})
    assert r.text("code 482 ok") == "code [REDACTED:DEMO_PASSCODE] ok"
    assert r.text("passcode=482&x") == "passcode=[REDACTED:DEMO_PASSCODE]&x"
    assert r.text("4821 and x482y and 1482") == "4821 and x482y and 1482"


def test_numeric_cells_are_never_changed_even_when_equal_to_a_secret():
    r = Redactor({"DEMO_PASSCODE": "482", "PIN": "12345678"})
    data = {"rows": [[482, 482.0, 12345678]], "text": "482"}
    assert r(data) == {"rows": [[482, 482.0, 12345678]], "text": "[REDACTED:DEMO_PASSCODE]"}


def test_long_secrets_are_scrubbed_even_inside_words():
    r = Redactor({"C_KEY": "abcd1234efgh"})
    assert r.text("xabcd1234efghx") == "x[REDACTED:C_KEY]x"


def test_json_escaped_forms_of_a_secret_are_scrubbed():
    secret = 'Pa"ss\\wörd-' + "12345"
    r = Redactor({"CLICKHOUSE_RO_PASSWORD": secret})
    once = json.dumps({"pw_hint": secret})  # quote and backslash escaped, non-ASCII as \u00f6
    twice = json.dumps(once)  # JSON inside a JSON string
    for text in (once, twice, json.dumps(secret, ensure_ascii=False), secret):
        out = r.text(text)
        assert secret not in out and json.dumps(secret)[1:-1] not in out, text
        assert "[REDACTED:CLICKHOUSE_RO_PASSWORD]" in out


def test_url_encoded_secret_is_scrubbed():
    secret = "a b/c+d=" + "e&f12345"
    from urllib.parse import quote

    out = Redactor({"X_SECRET": secret}).text("https://h/p?q=" + quote(secret, safe=""))
    assert quote(secret, safe="") not in out


def test_json_held_in_a_string_field_is_decoded_and_scrubbed():
    inner = json.dumps({"user": "app", "password": "opensesame1", "nested": {"api_key": "k1"}})
    out = Redactor()({"arguments": {"payload": inner}})
    decoded = json.loads(out["arguments"]["payload"])
    assert decoded == {"user": "app", "password": "[REDACTED]", "nested": {"api_key": "[REDACTED]"}}
    assert "opensesame1" not in json.dumps(out)


def test_doubly_nested_json_strings_are_scrubbed():
    deep = json.dumps({"outer": json.dumps({"secret": "hiddenvalue9"})})
    out = Redactor()({"x": deep})
    assert "hiddenvalue9" not in json.dumps(out)


def test_unparseable_json_like_strings_still_get_text_rules():
    out = Redactor()("{not json Authorization: Bearer abcDEF123456")
    assert "abcDEF123456" not in out


def test_from_environ_picks_named_and_secret_looking_variables():
    env = {
        "ANTHROPIC_API_KEY": "anthkeyvalue1",
        "MY_SERVICE_TOKEN": "tok-12345678",
        "SOME_FLAG": "visible-value",
        "HOME": "/Users/x",
        "DEMO_PASSCODE": "",
    }
    r = Redactor.from_environ(env)
    out = r.text("anthkeyvalue1 tok-12345678 visible-value /Users/x")
    assert out == "[REDACTED:ANTHROPIC_API_KEY] [REDACTED:MY_SERVICE_TOKEN] visible-value /Users/x"
    assert "ANTHROPIC_API_KEY" in SECRET_ENV_NAMES and "LANGFUSE_SECRET_KEY" in SECRET_ENV_NAMES


def test_secret_looking_env_names_need_eight_characters():
    r = Redactor.from_environ({"FEATURE_TOKEN": "true"})
    assert r.text("true") == "true"


@pytest.mark.parametrize(
    "header",
    [
        f"Authorization: Bearer {BEARER}",
        f"authorization=Bearer {BEARER}",
        '"Authorization": "Basic dXNlcjpwYXNzd29yZA=="',
        "X-Api-Key: k3y-value-123",
        "api_key=k3y-value-123",
    ],
)
def test_auth_headers_are_scrubbed(header):
    out = Redactor().text(header)
    for part in (BEARER, "dXNlcjpwYXNzd29yZA==", "k3y-value-123"):
        assert part not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ("COOKIE: sid=abc123; theme=dark", "sid=abc123; theme=dark"),
        ("set-cookie: sid=xyz789; Path=/; HttpOnly", "sid=xyz789"),
        ("Proxy-Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("x-api-key=AbC123xyz", "AbC123xyz"),
        ('{\\"Authorization\\": \\"Bearer tok123abcXYZ\\"}', "tok123abcXYZ"),
        ("{'authorization': 'Token tok999zzz'}", "tok999zzz"),
        ("headers: authorization: digest u=1,nonce=2", "u=1"),
    ],
)
def test_header_values_are_scrubbed_case_insensitively_anywhere(text, hidden):
    out = Redactor().text(text)
    assert hidden not in out and "[REDACTED]" in out


def test_cookie_values_are_scrubbed_to_the_end_of_the_line_only():
    out = Redactor().text("Cookie: a=1; b=2\nnext line stays")
    assert out == "Cookie: [REDACTED]\nnext line stays"


@pytest.mark.parametrize(
    "param", ["token", "access_token", "key", "sig", "signature", "password", "API_KEY"]
)
def test_url_query_secrets_are_scrubbed(param):
    out = Redactor().text(f"https://x.example/a?page=2&{param}=s3cr3tv4l&b=1")
    assert "s3cr3tv4l" not in out
    assert out == f"https://x.example/a?page=2&{param}=[REDACTED]&b=1"


def test_bare_bearer_and_basic_credentials_are_scrubbed():
    r = Redactor()
    assert r.text(f"send Bearer {BEARER} now") == "send Bearer [REDACTED] now"
    assert r.text("Basic dXNlcjpwYXNz") == "Basic [REDACTED]"


def test_plain_english_after_basic_is_kept():
    text = "Basic statistics: the median is 250000."
    assert Redactor().text(text) == text


def test_token_secret_password_passcode_and_key_fields_are_replaced():
    data = {
        "csrf_token": "t",
        "clientSecret": "s",
        "DB_PASSWORD": "p",
        "passcode": 482,
        "api_key": "k",
        "apiKey": "k",
        "key": "v",
        "Set-Cookie": "c",
        "x-api-key": "x",
        "tokens_in": 5,
        "max_tokens": 10,
        "monkey": "kept",
        "keys": "kept",
        "query_id": "kept",
    }
    out = Redactor()(data)
    replaced = {k for k, v in out.items() if v == "[REDACTED]"}
    assert replaced == {
        "csrf_token", "clientSecret", "DB_PASSWORD", "passcode", "api_key", "apiKey", "key",
        "Set-Cookie", "x-api-key",
    }  # fmt: skip
    assert out["tokens_in"] == 5 and out["max_tokens"] == 10


def test_secret_named_mapping_keys_have_their_values_replaced():
    out = Redactor()(
        {
            "Authorization": "anything",
            "password": 123,
            "tokens_in": 5,
            "x": None,
            "nested": [{"api_key": "v", "client_secret": {"a": 1}}],
        }
    )
    assert out == {
        "Authorization": "[REDACTED]",
        "password": "[REDACTED]",
        "tokens_in": 5,
        "x": None,
        "nested": [{"api_key": "[REDACTED]", "client_secret": "[REDACTED]"}],
    }


def test_structures_become_plain_json():
    out = Redactor()({"t": (1, 2.5, True), "s": {"k": "v"}, "f": float("inf")})
    assert out == {"t": [1, 2.5, True], "s": {"k": "v"}, "f": None}
    json.dumps(out, allow_nan=False)


def test_clean_text_is_untouched():
    text = "SELECT town, median(price) FROM uk.uk_price_paid WHERE year = 2024"
    assert Redactor(SECRETS).text(text) == text


# -- A2.3 live verifier: forms and names the first rules missed (each failed on the old code) ---
def test_double_url_encoded_and_lower_case_percent_hex_secrets_are_scrubbed():
    from urllib.parse import quote

    secret = "a b/c+d=" + "e&f12345"
    r = Redactor({"X_SECRET": secret})
    twice = quote(quote(secret, safe=""), safe="")
    assert r.text("https://h/p?q=" + twice) == "https://h/p?q=[REDACTED:X_SECRET]"
    lower_hex = "a%20b%2fc%2bd%3de%26f12345"  # what some encoders write
    assert r.text(lower_hex) == "[REDACTED:X_SECRET]"


@pytest.mark.parametrize(
    "param", ["client_secret", "X-Amz-Signature", "X-Amz-Credential", "api-key", "refresh_token"]
)
def test_url_query_names_ending_in_a_secret_word_are_scrubbed(param):
    out = Redactor().text(f"https://x.example/a?page=2&{param}=s3cr3tv4l&b=1")
    assert out == f"https://x.example/a?page=2&{param}=[REDACTED]&b=1"


def test_query_names_merely_containing_a_secret_word_are_kept():
    text = "https://x.example/a?monkey=1&oauth=2&keyboard=3"
    assert Redactor().text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "host=db user=app password=s3cr3tv4l dbname=x",
        "connect failed: PASSWORD: 's3cr3tv4l'",
        "Pwd=s3cr3tv4l;Server=x",
        '{\\"passwd\\": \\"s3cr3tv4l\\"}',
    ],
)
def test_password_values_in_text_are_scrubbed(text):
    out = Redactor().text(text)
    assert "s3cr3tv4l" not in out and "[REDACTED]" in out


def test_api_key_and_access_key_fields_are_replaced_whatever_their_prefix():
    out = Redactor()(
        {"openai_api_key": "opaque1", "aws_access_key": "opaque2", "apiKey": "opaque3", "k": 1}
    )
    assert out == {
        "openai_api_key": "[REDACTED]",
        "aws_access_key": "[REDACTED]",
        "apiKey": "[REDACTED]",
        "k": 1,
    }


# -- A2.3 independent verifier: each test below failed on the code it was written against -------
def test_a_secret_used_as_a_mapping_key_is_scrubbed():
    out = Redactor(SECRETS)({ENV_SECRET: 1, "nested": [{f"k {ANTHROPIC_KEY}": "v"}]})
    assert out == {
        "[REDACTED:CLICKHOUSE_RO_PASSWORD]": 1,
        "nested": [{"k [REDACTED:ANTHROPIC_API_KEY]": "v"}],
    }


def test_json_too_deep_to_check_fails_closed():
    deep = "[" * 5000 + json.dumps(ENV_SECRET) + "]" * 5000
    assert Redactor(SECRETS)({"arg": deep}) == {"arg": "[REDACTED]"}


@pytest.mark.parametrize("safe", ["", "/"])
def test_url_encoded_secret_is_scrubbed_with_plus_or_percent_20_and_slash_kept_or_not(safe):
    from urllib.parse import quote, quote_plus

    secret = "ab/cd " + "ef+gh=ij9"
    r = Redactor({"X_SECRET": secret})
    for encoded in (quote(secret, safe=safe), quote_plus(secret, safe=safe)):
        assert r.text(f"q={encoded}&n=1") == "q=[REDACTED:X_SECRET]&n=1"


def test_url_encoded_secret_with_mixed_case_percent_hex_is_scrubbed():
    r = Redactor({"X_SECRET": "ab/cd+ef=gh/ij9"})
    assert r.text("x ab%2Fcd%2bef%3Dgh%2fij9 y") == "x [REDACTED:X_SECRET] y"


@pytest.mark.parametrize(
    "text",
    [
        "env: PGPASSWORD=s3cr3tv4l",
        "db_password=s3cr3tv4l host=x",
        "MYSQL_PWD: s3cr3tv4l",
        "X_API_KEY=s3cr3tv4l",
    ],
)
def test_password_and_api_key_values_after_a_longer_name_are_scrubbed(text):
    out = Redactor().text(text)
    assert "s3cr3tv4l" not in out and out.endswith(("[REDACTED]", "[REDACTED] host=x"))


@pytest.mark.parametrize(
    "url",
    [
        "https://x.example/cb#access_token=s3cr3tv4l&state=1",
        "https://x.example/a;token=s3cr3tv4l",
        "https://x.example/a?b=1&amp;token=s3cr3tv4l",
        "https://x.example/a?accessToken=s3cr3tv4l",
        "https://x.example/a?sessionToken=s3cr3tv4l",
    ],
)
def test_fragment_matrix_escaped_and_camel_case_query_secrets_are_scrubbed(url):
    out = Redactor().text(url)
    assert "s3cr3tv4l" not in out and "=[REDACTED]" in out


def test_camel_case_rule_spares_names_that_merely_end_in_key():
    text = "https://x.example/a?hotkey=1&turnkey=2&keyword=3&Monkey=4"
    assert Redactor().text(text) == text
