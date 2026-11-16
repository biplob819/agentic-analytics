import json
import re
import tomllib

import pytest

from agentic_analytics import __version__, cli
from harness_lib import ROOT


def test_cli_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"agentic-analytics {__version__}"


@pytest.mark.contract
def test_pyproject_distribution_metadata():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = meta["project"]
    assert project["name"] == "agentic-analytics"
    assert project["scripts"] == {
        "agentic-analytics": "agentic_analytics.cli:main",
        "aa": "agentic_analytics.cli:main",
    }
    assert project["license"] == "MIT"
    assert project["requires-python"] == ">=3.11"
    assert project["version"] == __version__


def test_pre_commit_runs_ruff_and_secret_scan():
    text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    for hook in ("id: ruff", "id: ruff-format", "secret_scan.py"):
        assert hook in text


def test_unbuilt_subcommand_exits_2_and_names_its_node(capsys, monkeypatch):
    # every subcommand is built now; unbuild one to keep the not-built path covered
    monkeypatch.delitem(cli.BUILT, "load")
    assert cli.main(["load", "--engine", "all"]) == 2
    assert "N1.3" in capsys.readouterr().err


@pytest.mark.contract
def test_unbuilt_subcommand_json_shape(capsys):
    unbuilt = [name for name in cli.COMMANDS if name not in cli.BUILT]
    if not unbuilt:
        pytest.skip("every subcommand is built")
    assert cli.main([unbuilt[0], "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "ok": False,
        "error": "not_built",
        "node": cli.COMMANDS[unbuilt[0]][1],
        "message": out["message"],
    }


@pytest.mark.contract
def test_json_flag_before_the_subcommand(capsys, monkeypatch):
    monkeypatch.delitem(cli.BUILT, "load")
    assert cli.main(["--json", "load"]) == 2
    assert json.loads(capsys.readouterr().out)["node"] == "N1.3"


def test_unknown_top_level_argument_is_an_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--bogus"])
    assert exc.value.code == 2


def test_every_command_names_a_real_build_node():
    from harness_lib import load_graph

    nodes = load_graph()
    for name, (_help, node) in cli.COMMANDS.items():
        assert node in nodes, name


def test_makefile_targets_call_known_subcommands():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = dict(re.findall(r"^([a-z-]+):\n\t(.+)$", text, re.M))
    for target in ("load", "gold", "smoke", "run", "report"):
        assert target in targets, target
        recipe = targets[target]
        assert recipe.startswith("uv run agentic-analytics "), recipe
        sub = recipe.split()[3]
        assert sub in cli.COMMANDS, sub
