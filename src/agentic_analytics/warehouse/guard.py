"""SQL guard, the first of the four read-only layers (EXE-01, D-19, Tech Spec §8).

`check_sql(sql, engine)` parses with sqlglot in the engine's dialect and allows only a single
read query: one `SELECT` statement, optionally with `WITH` and set operations (`UNION`,
`INTERSECT`, `EXCEPT`). It is an allow-list over the parse tree, never a keyword search, so
comments and string literals cannot hide or fake anything; a parse failure is a refusal.

Policy decisions (also in specs/N1.2-executors/spec.md):

- Refused statements: everything that is not a query, including DDL, DML, `SET`, `USE`,
  `SYSTEM`, `GRANT`, `CALL`, `COPY`, `PUT`, `GET`, `SHOW`, `DESCRIBE` and `EXPLAIN`. `EXPLAIN` is
  harmless but its output is not a result the scorer compares, so it is not worth the surface.
- `SELECT ... INTO` (any target, including `INTO OUTFILE`), a ClickHouse `SETTINGS` clause and a
  ClickHouse `FORMAT` clause are refused: the executor owns settings and the wire format.
- Table functions are refused unless they only generate rows (ClickHouse `numbers`, `zeros`,
  `generateSeries`; Snowflake `FLATTEN`, `SPLIT_TO_TABLE`, `GENERATOR`). Functions that reach
  files, the network, other servers, dictionaries, stages or the control plane are refused in
  any position (`url`, `s3`, `file`, `remote`, `SYSTEM$...`, ...).
- Schema-qualified function calls (`db.fn(x)`) are refused: they are user-defined or external
  functions, which a benchmark query never needs.
- Snowflake stage references (`@stage`, `@~`, `@%table`, quoted `'@s/f.csv'`) and any table
  named by a string literal are refused, as are Cortex AI functions. Positional `$1` is allowed.
- Every FROM/JOIN target anywhere in the tree must be a table, subquery, VALUES or an allowed
  table function; ARRAY JOIN over a subquery is refused. Deny-listed names match function calls
  only, never plain table or column names. Nested block comments are refused.
- ClickHouse `SETTINGS` and `FORMAT` are refused on every query node (set operations,
  subqueries, CTEs) and again at token level; references to the `system` database are refused.
- `SELECT 1; -- note` is one statement. Known false positive: sqlglot cannot parse ClickHouse
  `LIMIT n BY x LIMIT m`, so it is refused; put the outer LIMIT on an enclosing query.
- Control characters and invisible format characters (zero-width, bidi overrides) are refused
  before parsing; function names are NFKC-normalised and case-folded before comparison.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ErrorLevel
from sqlglot.tokenizer_core import TokenType

from agentic_analytics.schemas import Engine

MAX_SQL_CHARS: Final = 100_000

# Table functions that only generate rows from their arguments.
SAFE_TABLE_FUNCTIONS: Final[dict[str, frozenset[str]]] = {
    "clickhouse": frozenset(
        {
            "numbers",
            "numbers_mt",
            "zeros",
            "zeros_mt",
            "generateseries",
            "generate_series",
            "primes",
        }
    ),
    "snowflake": frozenset(
        {"flatten", "split_to_table", "strtok_split_to_table", "generator", "explode"}
    ),
}

# Functions refused anywhere in the tree, on either engine.
DENIED_FUNCTIONS: Final = frozenset(
    {
        # ClickHouse: network, files, other servers, external engines, processes, dictionaries
        "url",
        "urlcluster",
        "s3",
        "s3cluster",
        "file",
        "filecluster",
        "remote",
        "remotesecure",
        "cluster",
        "clusterallreplicas",
        "input",
        "jdbc",
        "odbc",
        "mysql",
        "postgresql",
        "sqlite",
        "mongodb",
        "redis",
        "hdfs",
        "hdfscluster",
        "azureblobstorage",
        "azureblobstoragecluster",
        "gcs",
        "oss",
        "cosn",
        "deltalake",
        "deltalakecluster",
        "hudi",
        "hudicluster",
        "executable",
        "dictionary",
        "fuzzjson",
        "fuzzquery",
        "loop",
        "merge",
        "view",
        "viewifpermitted",
        "viewexplain",
        "generaterandom",
        "sleep",
        "sleepeachrow",
        # ClickHouse: server, access and configuration introspection; model files
        "hascolumnintable",
        "getmacro",
        "getsetting",
        "getsettingordefault",
        "getserversetting",
        "getmergetreesetting",
        "currentprofiles",
        "enabledprofiles",
        "defaultprofiles",
        "currentroles",
        "enabledroles",
        "defaultroles",
        "getclienthttpheader",
        "getserverport",
        "filesystemavailable",
        "filesystemcapacity",
        "filesystemunreserved",
        "getoskernelversion",
        "zookeepersessionuptime",
        "addresstoline",
        "addresstolinewithinlines",
        "addresstosymbol",
        "demangle",
        "serveruuid",
        "catboostevaluate",
        # Snowflake: stages, files, results of other queries, schema inference
        "get_presigned_url",
        "build_scoped_file_url",
        "build_stage_file_url",
        "get_stage_location",
        "get_relative_path",
        "get_absolute_path",
        "infer_schema",
        "result_scan",
        "external_table_files",
        "external_table_file_registration_history",
        "validate",
        "validate_pipe_load",
        "copy_history",
        "stage_storage_usage_history",
    }
)
# Every ClickHouse dict* function (dictGet*, dictHas, dictIsIn, dictGetHierarchy, ...) reads a
# dictionary, whose source can be a URL or another server.
DENIED_PREFIXES: Final = ("iceberg", "paimon", "dict", "system$")

# Snowflake only: Cortex AI functions spend credits (AI_COMPLETE, AI_CLASSIFY, ...; the legacy
# SNOWFLAKE.CORTEX.* functions are also schema-qualified, which is refused on its own). Kept
# per engine because some names (e.g. TRANSLATE) are ordinary string functions elsewhere.
SNOWFLAKE_DENIED_FUNCTIONS: Final = frozenset(
    {
        "complete",
        "try_complete",
        "summarize",
        "sentiment",
        "entity_sentiment",
        "extract_answer",
        "classify_text",
        "embed_text_768",
        "embed_text_1024",
        "parse_document",
        "count_tokens",
        "finetune",
        "split_text_recursive_character",
        "split_text_markdown_header",
        "prompt",
        "to_file",  # reads a staged file
    }
)
SNOWFLAKE_DENIED_PREFIXES: Final = ("ai_", "cortex")

# Every documented ClickHouse table function (case-insensitive). A CALL to one of these is refused
# anywhere in a query (IN, notIn(...), globalIn(...), any nesting or parentheses), except a row
# generator (numbers, zeros, generateSeries, primes; VALUES is parsed as its own node) used as a
# FROM/JOIN source; `null` and `view` stay refused there too. `format` is left out: it is also
# an ordinary string function, and as a FROM source it is refused by the generator allow-list.
# Known limitation: a future table function missing here is allowed outside FROM/JOIN
# (e.g. `x IN newFn(...)`); the read-only role, holding no SOURCES grants, still refuses it.
CLICKHOUSE_TABLE_FUNCTIONS: Final = frozenset(
    n.casefold()
    for n in (
        "arrowFlight",
        "bigquery",
        "elasticsearch",
        "eval",
        "mergeTreeCodecBlockCounts",
        "mergeTreeTextIndex",
        "paimonCluster",
        "timeSeriesMetricFamilies",
        "timeSeriesSamples",
        "timeSeriesTimeRanges",
        "arrowflight",
        "azureBlobStorage",
        "azureBlobStorageCluster",
        "azureQueue",
        "cluster",
        "clusterAllReplicas",
        "cosn",
        "deltaLake",
        "deltaLakeAzure",
        "deltaLakeCluster",
        "deltaLakeLocal",
        "deltaLakeS3",
        "dictionary",
        "executable",
        "file",
        "fileCluster",
        "filesystem",
        "fuzzJSON",
        "fuzzQuery",
        "gcs",
        "generateRandom",
        "generateSeries",
        "generate_series",
        "hdfs",
        "hdfsCluster",
        "hive",
        "hudi",
        "hudiCluster",
        "iceberg",
        "icebergAzure",
        "icebergAzureCluster",
        "icebergCluster",
        "icebergHDFS",
        "icebergHDFSCluster",
        "icebergLocal",
        "icebergS3",
        "icebergS3Cluster",
        "input",
        "jdbc",
        "loop",
        "merge",
        "mergeTreeAnalyzeIndexes",
        "mergeTreeIndex",
        "mergeTreeProjection",
        "mongodb",
        "mysql",
        "null",
        "numbers",
        "numbers_mt",
        "odbc",
        "oss",
        "paimon",
        "paimonAzure",
        "paimonHDFS",
        "paimonLocal",
        "paimonS3",
        "postgresql",
        "primes",
        "prometheusQuery",
        "prometheusQueryRange",
        "redis",
        "remote",
        "remoteSecure",
        "s3",
        "s3Cluster",
        "s3Queue",
        "sqlite",
        "timeSeriesData",
        "timeSeriesMetrics",
        "timeSeriesSelector",
        "timeSeriesTags",
        "url",
        "urlCluster",
        "values",
        "view",
        "viewExplain",
        "viewIfPermitted",
        "ytsaurus",
        "zeros",
        "zeros_mt",
    )
)
CLICKHOUSE_TABLE_FUNCTION_PREFIXES: Final = ("iceberg", "deltalake", "hudi", "paimon")

# References qualified by the ClickHouse `system` database (query_log, processes, users, grants)
# are refused anywhere, including `x IN system.users`. information_schema stays allowed: it is
# read-only metadata filtered by the role's grants, and agents may use it for schema discovery.

# Node types that never belong in a read query, wherever they appear.
_FORBIDDEN_NODE_NAMES: Final = (
    "DDL",
    "DML",
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "Command",
    "Set",
    "SetItem",
    "Use",
    "Copy",
    "Put",
    "Get",
    "Grant",
    "Revoke",
    "TruncateTable",
    "Into",
    "Transaction",
    "Commit",
    "Rollback",
    "Pragma",
    "Describe",
    "Show",
    "Kill",
    "LoadData",
    "Analyze",
    "Cache",
    "Uncache",
    "Refresh",
    "ListStage",
    "Execute",
    "Declare",
    "Export",
    "Summarize",
)
_FORBIDDEN_NODES: Final[tuple[type[exp.Expr], ...]] = tuple(
    t for t in (getattr(exp, n, None) for n in _FORBIDDEN_NODE_NAMES) if isinstance(t, type)
)
_ROOTS: Final = (exp.Select, exp.SetOperation, exp.Subquery)
_FUNC_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _refuse(reason: str) -> GuardVerdict:
    return GuardVerdict(False, reason)


def _norm(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip("`\"'[] ").casefold()


def _bad_char(sql: str) -> str | None:
    for ch in sql:
        if ch in "\t\n\r":
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            return f"U+{ord(ch):04X}"
    return None


def _func_names(node: exp.Func, dialect: str, *, render: bool) -> set[str]:
    names: set[str] = set()
    this = node.args.get("this")
    if isinstance(this, str):  # Anonymous and ClickHouse parameterised/combined aggregates
        names.add(_norm(this))
    if isinstance(node, exp.Anonymous):
        if isinstance(this, exp.Identifier):
            names.add(_norm(this.name))
    else:
        names.update(_norm(n) for n in type(node).sql_names())
        if render:
            m = _FUNC_NAME.match(node.sql(dialect=dialect))
            if m:
                names.add(_norm(m.group(0)))
    return names


def _denied(names: set[str], engine: Engine | None = None) -> str | None:
    for n in sorted(names):
        if n in DENIED_FUNCTIONS or n.startswith(DENIED_PREFIXES):
            return n
        if engine == "snowflake" and (
            n in SNOWFLAKE_DENIED_FUNCTIONS or n.startswith(SNOWFLAKE_DENIED_PREFIXES)
        ):
            return n
    return None


def _table_function(node: exp.Expr) -> exp.Expr | None:
    """The function a FROM/JOIN item calls, if it is a table function."""
    if isinstance(node, exp.Table) and isinstance(node.this, exp.Func):
        return node.this
    if isinstance(node, exp.TableFromRows):
        return node.this
    if isinstance(node, exp.Lateral) and not isinstance(node.this, exp.Query):
        return node.this
    return None


_PLAIN_TARGETS: Final = (exp.Table, exp.Subquery, exp.Values, exp.TableFromRows, exp.Lateral)


def _target_problem(target: exp.Expr | None) -> str | None:
    """A FROM/JOIN target must be a table, a subquery, VALUES or a (checked) table function.
    Anything else is a parser shape the guard does not understand, so it is refused."""
    while isinstance(target, exp.Final):
        target = target.this
    if isinstance(target, _PLAIN_TARGETS):
        return None
    name = type(target).__name__ if target is not None else "nothing"
    return f"FROM/JOIN target {name} is not a table, subquery or allowed function"


def _join_problem(join: exp.Join) -> str | None:
    if str(join.args.get("kind") or "").upper() == "ARRAY":
        # ARRAY JOIN takes array expressions; over a subquery sqlglot mis-reads the inner FROM
        if any(isinstance(n, exp.Query) for n in join.walk() if n is not join):
            return "ARRAY JOIN over a subquery is not allowed"
        return None
    return _target_problem(join.this)


def _table_function_call_problem(node: exp.Func, dialect: str, safe: frozenset[str]) -> str | None:
    """A call to a ClickHouse table function is refused anywhere, except a row generator that is
    the source of its own FROM/JOIN item."""
    names = _func_names(node, dialect, render=False)
    hit = sorted(
        n
        for n in names
        if n in CLICKHOUSE_TABLE_FUNCTIONS or n.startswith(CLICKHOUSE_TABLE_FUNCTION_PREFIXES)
    )
    if not hit:
        return None
    parent = node.parent
    is_source = parent is not None and _table_function(parent) is node
    if is_source and names & safe:
        return None
    return f"table function {hit[0]} is not allowed here"


def _system_qualified(node: exp.Expr) -> bool:
    keys = ("db", "catalog") if isinstance(node, exp.Table) else ("table", "db", "catalog")
    return any(
        isinstance(q, exp.Identifier) and _norm(q.name) == "system"
        for q in (node.args.get(k) for k in keys)
    )


def _check_tree(root: exp.Expr, engine: Engine) -> str | None:
    dialect = engine
    safe = SAFE_TABLE_FUNCTIONS[engine]
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return f"{type(node).__name__} is not allowed in a read query"
        if isinstance(node, exp.Query | exp.CTE):  # SELECT, UNION/EXCEPT/INTERSECT, (subquery)
            if node.args.get("settings"):
                return "a SETTINGS clause is not allowed; the executor sets readonly and limits"
            if node.args.get("format"):
                return "a FORMAT clause is not allowed"
        if isinstance(node, exp.From):
            problem = _target_problem(node.this)
            if problem:
                return problem
        if isinstance(node, exp.Join):
            problem = _join_problem(node)
            if problem:
                return problem
        if (
            engine == "clickhouse"
            and isinstance(node, exp.Values)
            and not isinstance(node.parent, exp.From | exp.Join)
        ):  # sqlglot parses the values('...') table function as a VALUES node
            return "table function values is allowed only as a FROM/JOIN source"
        if engine == "clickhouse" and isinstance(node, exp.Func):
            problem = _table_function_call_problem(node, dialect, safe)
            if problem:
                return problem
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Literal):
            return "a string literal is not a table (stage paths such as '@s/f.csv')"
        if (
            engine == "clickhouse"
            and isinstance(node, exp.Table | exp.Column)
            and _system_qualified(node)
        ):
            return "the system database is not allowed"
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            return "schema-qualified (user-defined or external) functions are not allowed"
        if (
            engine == "clickhouse"
            and isinstance(node, exp.TableAlias)
            and node.args.get("columns")
            and not node.this
        ):
            # sqlglot reads `dictionary('d')` as table `dictionary` with alias columns ('d');
            # ClickHouse has no bare alias-column syntax, so this shape is always a table function
            return "table function call is not allowed"
        fn = _table_function(node)
        if fn is not None:
            if not isinstance(fn, exp.Func):
                return f"table expression {type(fn).__name__} is not allowed"
            names = _func_names(fn, dialect, render=True)
            if not names & safe:
                return f"table function {sorted(names)[0]} is not allowed"
        if isinstance(node, exp.Func):  # the deny list matches function calls only
            bad = _denied(_func_names(node, dialect, render=False), engine)
            if bad:
                return f"function {bad} is not allowed"
        if engine == "snowflake":
            if isinstance(node, exp.Var | exp.Identifier) and str(node.this).startswith("@"):
                return "stage references (@stage) are not allowed"
            if isinstance(node, exp.Parameter) and not (
                isinstance(node.this, exp.Literal) and node.this.is_int
            ):  # @stage inside expressions; positional $1 is allowed
                return "stage references (@stage) are not allowed"
    return None


_FORMAT_NOT_NAME: Final = frozenset(
    {
        TokenType.L_PAREN,
        TokenType.COMMA,
        TokenType.DOT,
        TokenType.ALIAS,
        TokenType.FROM,
        TokenType.DESC,
        TokenType.ASC,
        TokenType.R_PAREN,
        TokenType.EQ,
    }
)
_FORMAT_END: Final = frozenset({TokenType.SEMICOLON, TokenType.SETTINGS, TokenType.R_PAREN})
_FROM_JOIN: Final = frozenset({TokenType.FROM, TokenType.JOIN})
_FUNC_TABLE_OK: Final = frozenset({TokenType.TABLE, TokenType.LATERAL, TokenType.VALUES})


def _token_problem(tokens: list, engine: Engine, sql: str) -> str | None:
    """Checks over the token stream, independent of how sqlglot builds the tree (a backstop).
    `sql` is the source text, used to tell block comments (`/* ... */`) from line comments."""
    safe = SAFE_TABLE_FUNCTIONS[engine]

    def at(i: int) -> Any:
        return tokens[i] if 0 <= i < len(tokens) else None

    for i, tok in enumerate(tokens):
        if any("/*" in c and f"/*{c}" in sql for c in tok.comments):
            return "nested comments are not allowed"  # engines disagree on how they nest
        if tok.token_type == TokenType.INTO:  # INTO OUTFILE, INTO :var, INSERT INTO ...
            return "INTO is not allowed in a read query"
        t1, t2 = at(i + 1), at(i + 2)
        prev = at(i - 1)
        if (
            tok.token_type in _FROM_JOIN
            and not (prev is not None and prev.token_type == TokenType.ARRAY)  # array exprs
            and t1 is not None
            and t2 is not None
            and t2.token_type == TokenType.L_PAREN
            and t1.token_type not in (_FUNC_TABLE_OK | {TokenType.L_PAREN})
            and _norm(t1.text) not in safe
        ):
            return f"table function {_norm(t1.text)} is not allowed"
        if engine != "clickhouse":
            continue
        if _norm(tok.text) == "system" and t1 is not None and t1.token_type == TokenType.DOT:
            return "the system database is not allowed"
        # a clause is `SETTINGS name = value`; a column named settings is left alone
        if (
            tok.token_type == TokenType.SETTINGS
            and t2 is not None
            and t2.token_type == TokenType.EQ
        ):
            return "a SETTINGS clause is not allowed; the executor sets readonly and limits"
        # a clause is `FORMAT Name` at the end of a query; format(...) and a column are left alone
        if (
            tok.token_type == TokenType.FORMAT
            and t1 is not None
            and t1.token_type not in _FORMAT_NOT_NAME
            and (t2 is None or t2.token_type in _FORMAT_END)
        ):
            return "a FORMAT clause is not allowed"
    return None


def _statements(parsed: list) -> list[exp.Expr]:
    """Real statements: drops empty ones and sqlglot's comment-only `Semicolon` remainder, so
    `SELECT 1; -- note` is one statement."""
    semicolon = getattr(exp, "Semicolon", None)
    return [
        s
        for s in parsed
        if s is not None and not (semicolon is not None and isinstance(s, semicolon))
    ]


def check_sql(sql: str, engine: Engine) -> GuardVerdict:
    """Allow exactly one read query; anything else, or anything unparseable, is refused."""
    if engine not in SAFE_TABLE_FUNCTIONS:
        return _refuse(f"unknown engine {engine!r}")
    if not isinstance(sql, str) or not sql.strip():
        return _refuse("empty SQL")
    if len(sql) > MAX_SQL_CHARS:
        return _refuse(f"SQL longer than {MAX_SQL_CHARS} characters")
    bad = _bad_char(sql)
    if bad:
        return _refuse(f"control or invisible character {bad}")
    try:
        tokens = sqlglot.tokenize(sql, read=engine)
        parsed = sqlglot.parse(sql, read=engine, error_level=ErrorLevel.RAISE)
    except Exception as e:  # ParseError, TokenError, RecursionError, ...: all refusals
        return _refuse(f"could not parse as {engine} SQL ({type(e).__name__})")
    problem = _token_problem(tokens, engine, sql)
    if problem:
        return _refuse(problem)
    statements = _statements(parsed)
    if len(statements) != 1:
        return _refuse(f"exactly one statement is allowed, found {len(statements)}")
    root = statements[0]
    if not isinstance(root, _ROOTS):
        return _refuse(f"only SELECT or WITH queries are allowed, not {type(root).__name__}")
    problem = _check_tree(root, engine)
    if problem:
        return _refuse(problem)
    return GuardVerdict(True)


__all__ = ["DENIED_FUNCTIONS", "SAFE_TABLE_FUNCTIONS", "GuardVerdict", "check_sql"]
