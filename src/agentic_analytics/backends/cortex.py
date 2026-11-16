"""Snowflake Cortex Analyst backend (A2.2; Tech Spec §2.5, §2.6, D-3, D-7, D-35).

One question is one REST call to `POST /api/v2/cortex/analyst/message` with the semantic view in
the body. Cortex returns SQL, suggestions or text; it never executes anything (C1), so the SQL is
run here through an injected read-only executor and the answer text is a fixed template over the
executed rows (D-7). Scoring re-executes the same SQL on its own (D-3).

- **CTX-01** `KeyPairJwt` mints the Snowflake key-pair JWT (RS256; `iss` is
  `ACCOUNT.USER.SHA256:<fingerprint>`, `sub` is `ACCOUNT.USER`, where ACCOUNT is the account
  identifier before its first dot, upper-cased; lifetime at most one hour) and caches it, minting
  a new one `refresh_s` before it expires. The hostname keeps the full account, lower-cased, with
  underscores as hyphens. `CortexClient` posts through `httpx2` (the installed successor of
  httpx) under a per-phase timeout and a total deadline. Failures before the request reaches
  Cortex (connect errors and timeouts, pool and write failures) and 408/409/429/5xx go through
  EvalKit's `with_retries` (six attempts). Failures after it was sent (read errors and timeouts,
  a dropped connection) are never retried, since Cortex may have billed the message. Any other
  non-200 raises `CortexHTTPError`, which `classify_error` calls `config`, so the run stops
  (EK L-21). No exception message or repr carries the JWT, the key, its passphrase or its path.
- **CTX-02** `parse_response` reads `request_id`, `text`, `sql` (`statement`,
  `confidence.verified_query_used`), `suggestions` (type `suggestions` or `suggestion`, list
  under either key), `warnings[].message` and `response_metadata.model_names` /
  `question_category` / `analyst_latency_ms` (Cortex's own time, so the report can separate it
  from the Thailand-to-US transit, OWNER D-62). Anything unexpected is skipped and named in
  `ignored`; only a body that is not a JSON object with a `message.content` list is a
  `CortexSchemaError` (which keeps the body's `request_id` when it has one).
- **CTX-03** SQL is executed with `executor.execute(sql, row_cap=...)` (the Snowflake executor
  under `ANALYTICS_RO`, which runs the SQL guard first). Backends never import `warehouse/`.
- **CTX-04** Suggestions without SQL become an answer with `suggestions`, `sql=None` and
  `clarification=None`; `asked_for_clarification` derives from the suggestions.
- **CTX-05** `template_answer` writes the answer from the executed rows, cell values written
  exactly as the rows hold them, so answer-from-rows passes by construction for numeric cells.
  Cortex's text block is kept as `interpretation`. A failed execution keeps the SQL, says the
  query failed and leaves `answer.error` None: S2.1 scores it from the `Execution` (§2.6).
- **CTX-06** Single turn. Transport failures give `error="transport"`, a read timeout or the
  deadline `error="timeout"` (both with `sql=None`, `answer=""`); a text-only reply is
  `error="refusal"` with the text kept; an unreadable or undecodable 200 body is
  `error="schema"`. `CortexRun` carries the answer, `cortex_messages` (1 when Cortex answered
  200 or may have received the request before the reply was lost, counted conservatively for
  cost; else 0), the API latency (D-35), the warehouse time and Cortex's warnings.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from evalkit.retry import with_retries

from agentic_analytics.models.errors import classify_error
from agentic_analytics.schemas import AnalyticsAnswer, Execution

API_PATH = "/api/v2/cortex/analyst/message"
TOKEN_TYPE = "KEYPAIR_JWT"
MAX_JWT_LIFETIME_S = 3600  # Snowflake rejects key-pair JWTs that live longer than one hour
JWT_LIFETIME_S = 3540  # one minute under the limit, for clock skew
JWT_REFRESH_S = 300  # mint a new token when less than five minutes remain
REQUEST_TIMEOUT_S = 60.0  # per phase (connect, write, read, pool)
REQUEST_DEADLINE_S = 120.0  # the whole call, retries and backoff included (the agents' 120 s)
RETRY_ATTEMPTS = 6  # Tech Spec §2.6
ROW_CAP = 10_000  # Tech Spec §8: the same cap as the scoring executor
BACKEND = "cortex_analyst"
KEY_ENV = "SNOWFLAKE_RO_PRIVATE_KEY_PATH"
KEY_TEXT_ENV = "SNOWFLAKE_RO_PRIVATE_KEY"  # D-57: the PEM text, when there is no key file
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
SEMANTIC_VIEW_RE = re.compile(rf"{_IDENT}\.{_IDENT}\.{_IDENT}")
_MAX_ERROR_TEXT = 500

Clock = Callable[[], float]

# httpx2 failures that happen before the request reaches Cortex: safe to retry.
_BEFORE_SEND: tuple[type[Exception], ...] = (
    httpx2.ConnectError,
    httpx2.ConnectTimeout,
    httpx2.PoolTimeout,
    httpx2.WriteError,
    httpx2.WriteTimeout,
    httpx2.ProxyError,
)


class CortexConfigError(RuntimeError):
    """Unusable key material, account, user, URL or semantic view: stops the run (EK L-21)."""


class CortexHTTPError(Exception):
    """A non-200 reply. 408/409/429/5xx are retried; anything else is a config error."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str | None = None,
        message: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        detail = ": ".join(p for p in (code, message) if p) or "no error body"
        if 300 <= status_code < 400:
            detail += " (a redirect: check SNOWFLAKE_ACCOUNT or base_url)"
        super().__init__(
            f"Cortex Analyst returned HTTP {status_code}: {detail} (request_id={request_id})"
        )

    @classmethod
    def from_response(cls, resp: httpx2.Response) -> CortexHTTPError:
        code = message = request_id = None
        try:
            body = resp.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            code = _short(body.get("code") or body.get("error_code"))
            message = _short(body.get("message"))
            request_id = _short(body.get("request_id"))
        return cls(resp.status_code, code=code, message=message, request_id=request_id)


class CortexConnectionError(ConnectionError):
    """The request never reached Cortex (refused, connect or pool timeout, write failure):
    retried, and not billed."""


class CortexInterrupted(Exception):
    """The request was sent but the reply was lost (read error, dropped connection). Never
    retried: Cortex may have billed it, so it counts as one message; `error="transport"`."""

    maybe_billed = True


class CortexTimeout(Exception):
    """No reply within the read timeout or the total deadline. Never retried;
    `error="timeout"`. `maybe_billed` says whether a request was in flight."""

    def __init__(self, message: str, *, maybe_billed: bool) -> None:
        super().__init__(message)
        self.maybe_billed = maybe_billed


class CortexSchemaError(ValueError):
    """A 200 body that cannot be decoded or is not a JSON object with a `message.content` list."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


def _short(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_MAX_ERROR_TEXT]


# --- CTX-01: key-pair JWT ---------------------------------------------------------------------


def jwt_account(account: str) -> str:
    """The account part of `iss`/`sub`: the identifier before its first dot, upper-cased.

    `xy12345.us-east-2.aws` -> `XY12345`; `myorg-myaccount.privatelink` -> `MYORG-MYACCOUNT`.
    """
    return account.strip().partition(".")[0].upper()


def account_host(account: str) -> str:
    """The hostname label: the full account, lower-cased, underscores as hyphens."""
    return account.strip().lower().replace("_", "-")


def public_key_fingerprint(key: rsa.RSAPrivateKey) -> str:
    """`SHA256:` + base64 of the SHA-256 of the DER SubjectPublicKeyInfo (Snowflake's format)."""
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")


def load_rsa_key(
    data: bytes, passphrase: str | None = None, *, where: str = f"named by {KEY_ENV}"
) -> rsa.RSAPrivateKey:
    """Load a PEM or DER RSA private key; anything else is a `CortexConfigError` whose message
    names only where the key came from and the exception types (never the passphrase or the key
    bytes)."""
    password = passphrase.encode("utf-8") if passphrase else None
    key: Any = None
    errors: list[str] = []
    for loader in (serialization.load_pem_private_key, serialization.load_der_private_key):
        try:
            key = loader(data, password=password)
            break
        except (ValueError, TypeError) as e:
            errors.append(type(e).__name__)
    if key is None:
        raise CortexConfigError(f"could not load the private key {where} ({', '.join(errors)})")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CortexConfigError(f"the private key {where} must be RSA (RS256)")
    return key


class KeyPairJwt:
    """A cached Snowflake key-pair JWT, minted again `refresh_s` before it expires."""

    def __init__(
        self,
        account: str,
        user: str,
        private_key: rsa.RSAPrivateKey | bytes,
        *,
        passphrase: str | None = None,
        lifetime_s: int = JWT_LIFETIME_S,
        refresh_s: int = JWT_REFRESH_S,
        clock: Clock = time.time,
        key_where: str = f"named by {KEY_ENV}",
    ) -> None:
        if not isinstance(account, str) or not jwt_account(account):
            raise CortexConfigError("the Snowflake account is blank")
        if not isinstance(user, str) or not user.strip():
            raise CortexConfigError("the Snowflake user is blank")
        if not 0 < refresh_s < lifetime_s <= MAX_JWT_LIFETIME_S:
            raise ValueError(
                f"need 0 < refresh_s < lifetime_s <= {MAX_JWT_LIFETIME_S}, "
                f"got refresh_s={refresh_s}, lifetime_s={lifetime_s}"
            )
        self._key = (
            private_key
            if isinstance(private_key, rsa.RSAPrivateKey)
            else load_rsa_key(private_key, passphrase, where=key_where)
        )
        self.subject = f"{jwt_account(account)}.{user.strip().upper()}"
        self.issuer = f"{self.subject}.{public_key_fingerprint(self._key)}"
        self.lifetime_s = lifetime_s
        self.refresh_s = refresh_s
        self._clock = clock
        self._token: str | None = None
        self._exp = 0
        self.minted = 0

    @classmethod
    def from_key_file(
        cls, account: str, user: str, path: str | Path, *, passphrase: str | None = None, **kw: Any
    ) -> KeyPairJwt:
        try:
            data = Path(path).read_bytes()
        except OSError as e:  # the OSError text holds the path: drop it from the chain too
            raise CortexConfigError(
                f"could not read the private key named by {KEY_ENV} ({type(e).__name__})"
            ) from None
        return cls(account, user, data, passphrase=passphrase, **kw)

    @classmethod
    def from_key_text(
        cls, account: str, user: str, text: str, *, passphrase: str | None = None, **kw: Any
    ) -> KeyPairJwt:
        """The key from its PEM text (`SNOWFLAKE_RO_PRIVATE_KEY`, D-57): a failure names the
        variable and the exception types only."""
        try:
            data = text.encode("utf-8")
        except UnicodeError:
            raise CortexConfigError(f"the private key in {KEY_TEXT_ENV} is not text") from None
        try:
            return cls(account, user, data, passphrase=passphrase,
                       key_where=f"in {KEY_TEXT_ENV}", **kw)  # fmt: skip
        except CortexConfigError as e:
            raise CortexConfigError(str(e)) from None

    def __repr__(self) -> str:
        return f"KeyPairJwt(subject={self.subject!r})"

    @property
    def expires_at(self) -> int:
        return self._exp

    def token(self) -> str:
        now = self._clock()
        if self._token is None or now >= self._exp - self.refresh_s:
            iat = int(now)
            exp = iat + self.lifetime_s
            claims = {"iss": self.issuer, "sub": self.subject, "iat": iat, "exp": exp}
            self._token = jwt.encode(claims, self._key, algorithm="RS256")
            self._exp = exp
            self.minted += 1
        return self._token


# --- CTX-01: REST client ----------------------------------------------------------------------


class CortexClient:
    """Posts one question to Cortex Analyst and returns the 200 body as bytes.

    Each attempt asks `jwt.token()` afresh, so a retry after a long backoff never sends a token
    that is about to expire. A new `httpx2.AsyncClient` is opened per question, so the client is
    safe to use from any event loop. Exceptions are raised `from None` with messages that name
    only exception types, so no header can leak through a message or a chained cause.
    """

    def __init__(
        self,
        account: str,
        semantic_view: str,
        jwt_source: KeyPairJwt,
        *,
        base_url: str | None = None,
        timeout_s: float = REQUEST_TIMEOUT_S,
        deadline_s: float = REQUEST_DEADLINE_S,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if not isinstance(semantic_view, str) or not SEMANTIC_VIEW_RE.fullmatch(semantic_view):
            raise CortexConfigError(
                f"semantic_view must be DATABASE.SCHEMA.VIEW, got {semantic_view!r}"
            )
        if not isinstance(account, str) or not account.strip():
            raise CortexConfigError("the Snowflake account is blank")
        if not (timeout_s > 0):
            raise ValueError(f"timeout_s must be positive, got {timeout_s!r}")
        if not (deadline_s > 0):
            raise ValueError(f"deadline_s must be positive, got {deadline_s!r}")
        host = f"https://{account_host(account)}.snowflakecomputing.com"
        self.base_url = (base_url or host).rstrip("/")
        self.semantic_view = semantic_view
        self.timeout_s = timeout_s
        self.deadline_s = deadline_s
        self._jwt = jwt_source
        self._transport = transport
        self._sleep = sleep
        self._rng = rng

    def __repr__(self) -> str:
        return f"CortexClient(url={self.url!r}, semantic_view={self.semantic_view!r})"

    @property
    def url(self) -> str:
        return self.base_url + API_PATH

    def request_body(self, question: str) -> dict[str, Any]:
        return {
            "messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
            "semantic_view": self.semantic_view,
        }

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt.token()}",
            "X-Snowflake-Authorization-Token-Type": TOKEN_TYPE,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _attempt(
        self, client: httpx2.AsyncClient, body: bytes, in_flight: list[bool]
    ) -> bytes:
        request = client.build_request("POST", self.url, content=body, headers=self.headers())
        in_flight[0] = True
        try:
            content = await self._exchange(client, request)
        except asyncio.CancelledError:
            raise  # the deadline fired mid-request: leave the request marked in flight
        except BaseException:
            in_flight[0] = False
            raise
        in_flight[0] = False
        return content

    async def _exchange(self, client: httpx2.AsyncClient, request: httpx2.Request) -> bytes:
        try:
            resp = await client.send(request, stream=True)
        except httpx2.TooManyRedirects:
            raise CortexConfigError(
                "too many redirects from the Cortex endpoint: check SNOWFLAKE_ACCOUNT or base_url"
            ) from None
        except (httpx2.UnsupportedProtocol, httpx2.LocalProtocolError) as e:
            raise CortexConfigError(
                f"cannot send to the Cortex endpoint ({type(e).__name__})"
            ) from None
        except _BEFORE_SEND as e:
            raise CortexConnectionError(f"Cortex not reached ({type(e).__name__})") from None
        except httpx2.TimeoutException:
            raise CortexTimeout(
                f"no reply within {self.timeout_s:g} s", maybe_billed=True
            ) from None
        except httpx2.TransportError as e:
            raise CortexInterrupted(f"reply lost after sending ({type(e).__name__})") from None
        try:
            if resp.status_code != 200:
                with contextlib.suppress(httpx2.HTTPError):  # the status matters, not the body
                    await resp.aread()
                raise CortexHTTPError.from_response(resp)
            try:
                return await resp.aread()
            except httpx2.DecodingError:
                raise CortexSchemaError("the 200 body could not be decoded") from None
            except httpx2.TimeoutException:
                raise CortexTimeout(
                    f"no reply within {self.timeout_s:g} s", maybe_billed=True
                ) from None
            except httpx2.TransportError as e:
                raise CortexInterrupted(f"reply lost while reading ({type(e).__name__})") from None
        finally:
            await resp.aclose()

    async def send(self, question: str) -> bytes:
        body = json.dumps(self.request_body(question)).encode("utf-8")
        in_flight = [False]
        deadline = asyncio.timeout(self.deadline_s)
        try:
            async with deadline:
                timeout = httpx2.Timeout(self.timeout_s)
                async with httpx2.AsyncClient(transport=self._transport, timeout=timeout) as c:
                    return await with_retries(
                        lambda: self._attempt(c, body, in_flight),
                        attempts=RETRY_ATTEMPTS,
                        sleep=self._sleep,
                        rng=self._rng,
                    )
        except TimeoutError:
            if not deadline.expired():
                raise
            raise CortexTimeout(
                f"no reply within the {self.deadline_s:g} s deadline", maybe_billed=in_flight[0]
            ) from None


# --- CTX-02: response parser ------------------------------------------------------------------


@dataclass(frozen=True)
class CortexResponse:
    request_id: str | None
    text: str | None
    sql: str | None
    verified_query_used: Mapping[str, Any] | None
    suggestions: tuple[str, ...]
    warnings: tuple[str, ...]
    model_names: tuple[str, ...]
    question_category: str | None
    ignored: tuple[str, ...]  # content the parser skipped, named, for the record
    analyst_latency_ms: float | None = None  # Cortex's own time; finite and >= 0, else None

    @property
    def model_snapshot(self) -> str | None:
        return ",".join(self.model_names) or None


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())


def parse_response(body: bytes | str | Mapping[str, Any]) -> CortexResponse:
    """Read a Cortex Analyst 200 body defensively (CTX-02)."""
    data: Any = body
    if isinstance(body, bytes | str):
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError) as e:  # deep nesting recurses
            raise CortexSchemaError(f"the body is not JSON ({type(e).__name__})") from None
    if not isinstance(data, Mapping):
        raise CortexSchemaError("the body is not a JSON object")
    raw_id = data.get("request_id")
    request_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
    message = data.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        raise CortexSchemaError("the body has no message.content list", request_id=request_id)

    texts: list[str] = []
    sql: str | None = None
    verified: Mapping[str, Any] | None = None
    suggestions: dict[str, None] = {}  # ordered and de-duplicated in linear time
    ignored: list[str] = []
    for item in content:
        kind = item.get("type") if isinstance(item, Mapping) else None
        if not isinstance(item, Mapping) or not isinstance(kind, str):
            ignored.append("<untyped>")
        elif kind == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
            else:
                ignored.append("text (empty)")
        elif kind == "sql":
            statement = item.get("statement")
            if not isinstance(statement, str) or not statement.strip():
                ignored.append("sql (no statement)")
            elif sql is not None:
                ignored.append("sql (more than one)")
            else:
                sql = statement
                confidence = item.get("confidence")
                vq = (
                    confidence.get("verified_query_used")
                    if isinstance(confidence, Mapping)
                    else None
                )
                verified = dict(vq) if isinstance(vq, Mapping) else None
        elif kind in ("suggestions", "suggestion"):
            found = _strings(item.get("suggestions") or item.get("suggestion"))
            if found:
                suggestions.update(dict.fromkeys(found))
            else:
                ignored.append(f"{kind} (empty)")
        else:
            ignored.append(kind)
    if sql is not None and suggestions:  # documented as mutually exclusive; SQL wins
        ignored.append("suggestions (alongside sql)")
        suggestions = {}

    warnings: list[str] = []
    raw_warnings = data.get("warnings")
    for w in raw_warnings if isinstance(raw_warnings, list) else ():
        msg = w.get("message") if isinstance(w, Mapping) else w
        if isinstance(msg, str) and msg.strip():
            warnings.append(msg.strip())
    meta = data.get("response_metadata")
    meta = meta if isinstance(meta, Mapping) else {}
    category = meta.get("question_category")
    took: Any = meta.get("analyst_latency_ms")
    if isinstance(took, bool) or not isinstance(took, int | float):
        took = None
    else:
        try:
            took = float(took)  # an int too large for a float overflows: not a latency
        except OverflowError:
            took = None
        if took is not None and not (math.isfinite(took) and took >= 0):
            took = None
    return CortexResponse(
        request_id=request_id,
        text="\n\n".join(texts) or None,
        sql=sql,
        verified_query_used=verified,
        suggestions=tuple(suggestions),
        warnings=tuple(warnings),
        model_names=_strings(meta.get("model_names")),
        question_category=category if isinstance(category, str) and category.strip() else None,
        ignored=tuple(ignored),
        analyst_latency_ms=took,
    )


# --- CTX-05: answer template ------------------------------------------------------------------


def format_cell(value: Any) -> str:
    """A cell exactly as the row holds it: integers in full, floats in their shortest exact
    decimal form without an exponent, `NULL` for None, `true`/`false`, strings unchanged."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _row_text(columns: Sequence[str], row: Sequence[Any]) -> str:
    return ", ".join(f"{c}: {format_cell(v)}" for c, v in zip(columns, row, strict=False))


def template_answer(execution: Execution) -> str:
    """The Cortex answer text (D-7), written only from the executed rows.

    - failed: `The query failed (<error>).`
    - no rows: `The query returned no rows.`
    - one row, one column: `The result is <v>.`
    - one row: `COL1: v1, COL2: v2`
    - more rows: `<n> rows returned; first: COL1: v1, COL2: v2`, where n is the rows held (with
      `(cut at the row cap)` after it when the executor truncated).
    """
    if execution.result is None:
        return f"The query failed ({execution.error})."
    columns, rows = execution.result.columns, execution.result.rows
    if not rows:
        return "The query returned no rows."
    if len(rows) == 1 and len(columns) == 1:
        return f"The result is {format_cell(rows[0][0])}."
    if len(rows) == 1:
        return _row_text(columns, rows[0])
    cut = " (cut at the row cap)" if execution.truncated else ""
    return f"{len(rows)} rows returned{cut}; first: {_row_text(columns, rows[0])}"


# --- CTX-03..06: one question -----------------------------------------------------------------


class SqlExecutor(Protocol):
    """The Snowflake executor's contract (N1.2), restated so backends never import warehouse/."""

    def execute(self, sql: str, *, row_cap: int) -> Execution: ...


@dataclass(frozen=True)
class CortexRun:
    answer: AnalyticsAnswer
    cortex_messages: int  # 1 when Cortex answered 200 or may have got the request; else 0
    api_latency_ms: int  # the API call, retries and backoff included (D-35)
    warehouse_elapsed_ms: int  # the executor's elapsed_ms, 0 when nothing ran
    warnings: tuple[str, ...] = ()
    execution: Execution | None = None
    response: CortexResponse | None = None


def _ms(clock: Clock, t0: float) -> int:
    return max(0, round((clock() - t0) * 1000))


def _failed(error: Any, messages: int, latency: int, request_id: str | None = None) -> CortexRun:
    answer = AnalyticsAnswer(
        backend=BACKEND,
        model_snapshot=None,
        sql=None,
        answer="",
        error=error,
        request_id=request_id,
    )
    return CortexRun(answer, messages, latency, 0)


async def ask_cortex(
    question: str,
    client: CortexClient,
    executor: SqlExecutor,
    *,
    row_cap: int = ROW_CAP,
    clock: Clock = time.perf_counter,
) -> CortexRun:
    """Ask Cortex Analyst once, execute its SQL and return the templated answer (CTX-03..06).

    Raises `CortexConfigError`, a non-retryable `CortexHTTPError` (4xx) or anything else that
    `classify_error` calls `config`: those stop the run instead of being scored (EK L-21).
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("the question is blank")
    t0 = clock()
    try:
        body = await client.send(question)
    except CortexTimeout as exc:
        return _failed("timeout", int(exc.maybe_billed), _ms(clock, t0))
    except CortexInterrupted:
        return _failed("transport", 1, _ms(clock, t0))
    except CortexSchemaError:  # a 200 whose body could not be decoded
        return _failed("schema", 1, _ms(clock, t0))
    except CortexConfigError:
        raise
    except Exception as exc:
        if classify_error(exc) == "config":
            raise
        return _failed("transport", 0, _ms(clock, t0))
    latency = _ms(clock, t0)
    try:
        resp = parse_response(body)
    except CortexSchemaError as exc:
        return _failed("schema", 1, latency, exc.request_id)

    common: dict[str, Any] = dict(
        backend=BACKEND,
        model_snapshot=resp.model_snapshot,
        interpretation=resp.text,
        request_id=resp.request_id,
    )
    if resp.sql is not None:
        execution = executor.execute(resp.sql, row_cap=row_cap)
        if not isinstance(execution, Execution) or execution.engine != "snowflake":
            raise TypeError("the Cortex executor must return a Snowflake Execution")
        answer = AnalyticsAnswer(sql=resp.sql, answer=template_answer(execution), **common)
        return CortexRun(answer, 1, latency, execution.elapsed_ms, resp.warnings, execution, resp)
    if resp.suggestions:
        answer = AnalyticsAnswer(sql=None, answer="", suggestions=resp.suggestions, **common)
    else:  # text only (or nothing): Cortex declined to write SQL
        answer = AnalyticsAnswer(sql=None, answer=resp.text or "", error="refusal", **common)
    return CortexRun(answer, 1, latency, 0, resp.warnings, None, resp)


__all__ = [
    "API_PATH",
    "JWT_LIFETIME_S",
    "JWT_REFRESH_S",
    "MAX_JWT_LIFETIME_S",
    "REQUEST_DEADLINE_S",
    "REQUEST_TIMEOUT_S",
    "RETRY_ATTEMPTS",
    "TOKEN_TYPE",
    "CortexClient",
    "CortexConfigError",
    "CortexConnectionError",
    "CortexHTTPError",
    "CortexInterrupted",
    "CortexResponse",
    "CortexRun",
    "CortexSchemaError",
    "CortexTimeout",
    "KeyPairJwt",
    "SqlExecutor",
    "account_host",
    "ask_cortex",
    "format_cell",
    "jwt_account",
    "load_rsa_key",
    "parse_response",
    "public_key_fingerprint",
    "template_answer",
]
