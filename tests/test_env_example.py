"""HAR-07: the environment inventory, listed independently from Tech Spec §4 (EK L-10)."""

import re
import subprocess

import pytest

import secret_scan
from harness_lib import ROOT

SPEC_VARIABLES = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLICKHOUSE_MCP_KEY_ID",
    "CLICKHOUSE_MCP_KEY_SECRET",
    "CLICKHOUSE_SERVICE_ID",
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_RO_USER",
    "CLICKHOUSE_RO_PASSWORD",
    "CLICKHOUSE_ADMIN_USER",
    "CLICKHOUSE_ADMIN_PASSWORD",
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_RO_USER",
    "SNOWFLAKE_RO_PRIVATE_KEY_PATH",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ADMIN_USER",
    "SNOWFLAKE_ADMIN_PRIVATE_KEY_PATH",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "DEMO_PASSCODE",
    "EVALKIT_DEMO_DATABASE_URL",
}


def _lines():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


@pytest.mark.safety
def test_env_example_names_every_spec_variable_with_no_values():
    lines = _lines()
    bad = [ln for ln in lines if not re.fullmatch(r"[A-Z][A-Z0-9_]*=", ln)]
    assert not bad, bad
    names = [ln[:-1] for ln in lines]
    assert len(names) == len(set(names)), "duplicate variable"
    assert set(names) == SPEC_VARIABLES


def test_spec_environment_table_matches_inventory():
    spec = (ROOT / "Agentic Analytics Tech Spec.md").read_text(encoding="utf-8")
    table = spec[spec.index("**Environment.**") : spec.index("## 5. Gold protocol")]
    assert set(re.findall(r"`([A-Z][A-Z0-9_]+)`", table)) == SPEC_VARIABLES


def _ignored(path: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert r.returncode in (0, 1), "git check-ignore failed"
    return r.returncode == 0


@pytest.mark.safety
def test_git_really_ignores_private_files():
    for path in (".env", ".env.local", "keys/rsa.p8", "x.pem", "x.p8", "data/pp.csv", ".harness/x"):
        assert _ignored(path), path
    for path in (".env.example", "tests/data/x.json", "src/agentic_analytics/data/x.json"):
        assert not _ignored(path), path


@pytest.mark.safety
@pytest.mark.parametrize(
    "kind, sample",
    [
        ("langfuse secret key", "sk-lf-" + "0a1b2c3d-" * 4),
        ("url with password", "postgresql://owner:" + "Xy7pQ9wz" + "@ep-x.neon.tech/db"),
        ("url with password", "https://default:" + "S3cretPa55" + "@x.clickhouse.cloud:8443"),
        ("url with password", "clickhouses://ro:" + "Pa5" + "@host:9440/uk"),
        ("url with password", "POSTGRESQL://u:" + "zz9" + "@h/db"),
        ("url with password", "mongodb+srv://u:" + "k3y" + "@c.mongodb.net"),
        ("url with password", "redis://u:" + "r3d1s" + "@cache:6379"),
    ],
)
def test_scan_detects_project_secret_formats(tmp_path, kind, sample):
    f = tmp_path / "leak.txt"
    f.write_text(f"value = {sample}\n", encoding="utf-8")
    hits = secret_scan.scan([f])
    assert any(kind in h for h in hits), hits


@pytest.mark.safety
@pytest.mark.parametrize(
    "text",
    [
        "see https://mcp.clickhouse.cloud/mcp and postgresql://localhost/db",
        "postgresql://user:password@localhost/db",
        "postgresql://user:${PGPASSWORD}@localhost/db",
        "https://user:<token>@example.com",
        "contact a@b.com or git@github.com:org/repo",
    ],
)
def test_scan_ignores_placeholders_and_plain_urls(tmp_path, text):
    f = tmp_path / "ok.txt"
    f.write_text(text + "\n", "utf-8")
    assert secret_scan.scan([f]) == []
