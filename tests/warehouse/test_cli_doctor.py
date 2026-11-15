import json

import pytest

from agentic_analytics import cli
from agentic_analytics.env import load_dotenv
from agentic_analytics.warehouse.doctor import ENV_VARS


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_doctor_without_accounts_reports_missing_and_skips(clean_env, capsys):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out and "missing" in out and "SKIP" in out


@pytest.mark.safety
def test_doctor_never_prints_values(clean_env, capsys, monkeypatch):
    (clean_env / ".env").write_text("OPENAI_API_KEY=sk-proj-" + "Q" * 40 + "\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "Z" * 30)
    assert cli.main(["doctor", "--json"]) == 0
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert report["env"]["ANTHROPIC_API_KEY"] == "set"
    assert report["env"]["OPENAI_API_KEY"] == "set"
    assert "ZZZZ" not in raw and "QQQQ" not in raw


def test_dotenv_never_overrides_and_strips_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# c\nA=1\nexport B=\"two\"\nC=\nD='x y'\nbroken line\n", encoding="utf-8")
    env = {"A": "keep"}
    assert load_dotenv(f, env) == 2
    assert env == {"A": "keep", "B": "two", "D": "x y"}
    assert load_dotenv(tmp_path / "missing", env) == 0
