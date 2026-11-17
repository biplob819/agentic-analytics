"""MON-01: the scheduled smoke workflow (.github/workflows/monitor.yml).

Parses the workflow and checks: hand-run only, the weekly schedule present but disabled until
the release (OWNER D-103), never on pull requests or in forks, environment `monitoring`,
secrets only in the smoke step (never in a workflow- or job-level `env`), the smoke run under
`--max-cost 2`, then `evalkit gate` (EvalKit B5.3) on the run's metrics-1 export and
`monitoring/gates.yaml` as exactly that command, whose exit code nothing can swallow; the
spend line this run appended (never an older committed one) in the job summary; and the run's
metrics folder, gate verdict and spend line uploaded as an artifact `evalkit compare` reads.
Each check is also run against a planted defect, so a check that matches nothing cannot pass
silently (L-11); the shell steps are also run, offline, on a fake checkout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from evalkit.compare import compare_runs

from tests.golden.record import GOLDEN

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "monitor.yml"
UPSTREAM = "biplob819/agentic-analytics"
GATES = "monitoring/gates.yaml"
GATE_RUN = (
    'uv run evalkit gate "results/$(cat results/latest)/metrics/metrics.json" '
    f'--config {GATES} --out gate.json | tee -a "$GITHUB_STEP_SUMMARY"'
)
ARTIFACT_DIR = "monitor-artifact/"
UPLOAD = re.compile(r"actions/upload-artifact@[0-9a-f]{40}")
# GitHub runs `shell: bash` as `bash --noprofile --norc -eo pipefail {0}`, so a failing command
# on the left of `| tee` fails the step. Any other shell spelling may drop pipefail.
PIPEFAIL_SHELL = "bash"


def _on(data: dict[Any, Any]) -> Any:
    return data.get("on", data.get(True))  # PyYAML reads a bare `on` key as True


def _steps(data: dict[str, Any]) -> list[dict[str, Any]]:
    return data["jobs"]["smoke"]["steps"]


def _named(data: dict[str, Any], name: str) -> list[int]:
    return [i for i, s in enumerate(_steps(data)) if s.get("name") == name]


def _has_secrets(value: Any) -> bool:
    return "secrets" in json.dumps(value)


def problems(text: str) -> list[str]:
    """Why this monitor workflow breaks MON-01 (empty means fine)."""
    out: list[str] = []
    data = yaml.safe_load(text)
    on = _on(data)
    if not isinstance(on, dict):
        return ["triggers are not a mapping"]
    if "pull_request" in on or "pull_request_target" in on:
        out.append("runs on pull requests")
    if "schedule" in on:
        out.append("the schedule is live: it stays off until the release (OWNER D-103)")
    if "workflow_dispatch" not in on:
        out.append("cannot be run by hand")
    if not re.search(r"^  # schedule:\n  #   - cron: \"[^\"]+\"", text, re.M):
        out.append("no disabled weekly schedule to switch on")
    if "owner must approve recurring spend" not in text:
        out.append("does not say why the schedule is off")
    if _has_secrets({str(k): v for k, v in data.items() if k != "jobs"}):
        out.append("secrets outside the smoke step (workflow level)")
    jobs = data.get("jobs") or {}
    if list(jobs) != ["smoke"]:
        return [*out, f"jobs are {list(jobs)}, expected only smoke"]
    job = jobs["smoke"]
    if job.get("if") != f"github.repository == '{UPSTREAM}'":
        out.append("not guarded against forks")
    if job.get("environment") != "monitoring":
        out.append("not in environment monitoring")
    if data.get("permissions") != {"contents": "read"} or "permissions" in job:
        out.append("permissions beyond contents: read")
    if _has_secrets({k: v for k, v in job.items() if k != "steps"}):
        out.append("secrets outside the smoke step (job level)")
    if job.get("continue-on-error") not in (None, False):
        out.append("the job ignores its own failure (continue-on-error)")
    steps = _steps(data)
    runs = [" ".join(str(s.get("run", "")).split()) for s in steps]
    smoke = [i for i, r in enumerate(runs) if "agentic-analytics smoke" in r]
    if len(smoke) != 1:
        return [*out, "no single smoke step"]
    if runs[smoke[0]] != "uv run agentic-analytics smoke --max-cost 2":
        out.append("smoke not run under --max-cost 2")
    if [i for i, s in enumerate(steps) if _has_secrets(s)] != smoke:
        out.append("secrets reach a step other than the smoke run")
    if any(s.get("continue-on-error") not in (None, False) for s in steps[smoke[0] :]):
        out.append("a step from the smoke run onward ignores its failure (continue-on-error)")
    out += _gate_problems(steps, runs, smoke[0])
    out += _spend_problems(data, steps, runs, smoke[0])
    out += _artifact_problems(data, steps)
    if re.search(r"\bTODO\b", text):
        out.append("a TODO is left in the workflow")
    return out


def _gate_problems(steps: list[dict[str, Any]], runs: list[str], smoke: int) -> list[str]:
    gate = [i for i, r in enumerate(runs) if "evalkit gate" in r]
    if len(gate) != 1 or gate[0] < smoke:
        return ["no single evalkit gate step after the smoke run"]
    step, run = steps[gate[0]], runs[gate[0]]
    out = []
    # exactly the command: no `|| true`, `; true`, `exit 0` or `set +o pipefail` can join it
    if run != GATE_RUN:
        out.append("the evalkit gate step is not exactly the gate command piped to the summary")
    if "if" in step or step.get("continue-on-error") not in (None, False):
        out.append("the evalkit gate step can be skipped or ignored")
    if step.get("shell") != PIPEFAIL_SHELL:
        out.append("the evalkit gate step's exit code can be lost in the pipe (no pipefail)")
    return out


def _spend_problems(
    data: dict[str, Any], steps: list[dict[str, Any]], runs: list[str], smoke: int
) -> list[str]:
    before, spend = _named(data, "Ledger length before the run"), _named(data, "Spend line")
    if len(before) != 1 or before[0] > smoke:
        return ["the ledger length is not taken before the smoke run"]
    if len(spend) != 1 or spend[0] < smoke:
        return ["no spend line step after the smoke run"]
    out = []
    if "$RUNNER_TEMP/ledger-before" not in runs[before[0]]:
        out.append("the ledger length is not kept for the spend line")
    if "$RUNNER_TEMP/ledger-before" not in runs[spend[0]] or "tail -n 1 " in runs[spend[0]]:
        out.append("the spend line can show a line the run did not append")
    if steps[spend[0]].get("if") != "always()":
        out.append("the spend line is skipped when the gate fails")
    return out


def _artifact_problems(data: dict[str, Any], steps: list[dict[str, Any]]) -> list[str]:
    collect = _named(data, "Collect the artifact")
    upload = [i for i, s in enumerate(steps) if UPLOAD.fullmatch(str(s.get("uses", "")))]
    if len(collect) != 1 or len(upload) != 1 or upload[0] < collect[0]:
        return ["the run's metrics are not collected and uploaded as an artifact"]
    out = []
    run = str(steps[collect[0]].get("run", ""))
    if 'cp -R "results/$(cat results/latest)/metrics" monitor-artifact/metrics' not in run:
        out.append("the artifact does not hold the run's metrics folder")
    if "gate.json" not in run or "spend-line.jsonl" not in run:
        out.append("the artifact does not hold the gate verdict and the spend line")
    up = steps[upload[0]]
    if (up.get("with") or {}).get("path") != ARTIFACT_DIR:
        out.append("the artifact uploads something other than the collected folder")
    if any(steps[i].get("if") != "always()" for i in (collect[0], upload[0])):
        out.append("the artifact is skipped when the gate fails")
    return out


def test_monitor_workflow_meets_mon_01():
    assert problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_gates_file_the_workflow_reads_is_checked_in():
    assert (ROOT / GATES).is_file()


SCHEDULE = '  # schedule:\n  #   - cron: "17 6 * * 1"'
SMOKE = "        run: uv run agentic-analytics smoke --max-cost 2\n"
GATE_NAME = "      - name: evalkit gate\n"
GATE_TEE = '          | tee -a "$GITHUB_STEP_SUMMARY"\n'
SECRET = "${{ secrets.OPENAI_API_KEY }}"
TOP = "permissions:\n  contents: read\n"
TIMEOUT = "    timeout-minutes: 45\n"
CP_METRICS = '            cp -R "results/$(cat results/latest)/metrics" monitor-artifact/metrics\n'


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request:\n", "pull requests"),
        ("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request_target:\n",
         "pull requests"),
        (SCHEDULE, '  schedule:\n    - cron: "17 6 * * 1"', "schedule is live"),
        ("  workflow_dispatch:\n", "  push:\n", "by hand"),
        ("owner must approve recurring spend", "owner said so", "why the schedule"),
        (f"    if: github.repository == '{UPSTREAM}'\n", "", "forks"),
        ("    environment: monitoring\n", "    environment: production\n", "monitoring"),
        (SMOKE, "        run: uv run agentic-analytics smoke --max-cost 20\n", "--max-cost 2"),
        (SMOKE, "        run: uv run agentic-analytics smoke\n", "--max-cost 2"),
        # W2: secrets in a workflow-level or job-level env
        (TOP, f"env:\n  K: {SECRET}\n" + TOP, "workflow level"),
        (TIMEOUT, TIMEOUT + f"    env:\n      K: {SECRET}\n", "job level"),
        (GATE_TEE, f"          --token {SECRET}\n" + GATE_TEE, "secrets reach"),
        # W4: the gate's exit code swallowed or the step ignored
        (GATE_TEE, '          | tee -a "$GITHUB_STEP_SUMMARY" || true\n', "not exactly"),
        (GATE_TEE, '          | tee -a "$GITHUB_STEP_SUMMARY"; true\n', "not exactly"),
        (GATE_TEE, '          | tee -a "$GITHUB_STEP_SUMMARY"; exit 0\n', "not exactly"),
        ("          uv run evalkit gate", "          set +o pipefail; uv run evalkit gate",
         "not exactly"),
        ("--config monitoring/gates.yaml", "--config x.yaml", "not exactly"),
        ('results/$(cat results/latest)/metrics/metrics.json"', 'x/summary.json"', "not exactly"),
        (GATE_NAME, GATE_NAME + "        continue-on-error: true\n", "skipped or ignored"),
        (GATE_NAME, GATE_NAME + "        if: false\n", "skipped or ignored"),
        (TIMEOUT, TIMEOUT + "    continue-on-error: true\n", "job ignores its own failure"),
        (SMOKE, SMOKE + "        continue-on-error: true\n", "ignores its failure"),
        (GATE_NAME + "        shell: bash\n", GATE_NAME, "no pipefail"),
        (GATE_NAME + "        shell: bash\n", GATE_NAME + "        shell: bash {0}\n",
         "no pipefail"),
        (GATE_NAME + "        shell: bash\n        run: >-\n          uv run evalkit gate",
         GATE_NAME + "        shell: bash\n        run: >-\n          echo evalkit-gate",
         "no single evalkit gate"),
        # the spend line and the artifact
        ('tail -n "+$(( $(cat "$RUNNER_TEMP/ledger-before") + 1 ))"', "tail -n 1",
         "did not append"),
        ("      - name: Ledger length before the run\n", "      - name: Ledger length\n",
         "ledger length is not taken"),
        ("      - name: Spend line\n        if: always()\n", "      - name: Spend line\n",
         "spend line is skipped"),
        (CP_METRICS, "            true\n", "metrics folder"),
        ("          path: monitor-artifact/\n", "          path: results/\n", "something other"),
        ("ea165f8d65b6e75b540449e92b4886f43607fa02", "v4", "not collected and uploaded"),
        ("      # `evalkit gate` (EvalKit B5.3)", "      # TODO `evalkit gate` (EvalKit B5.3)",
         "a TODO"),
        (TIMEOUT, TIMEOUT + "    permissions:\n      contents: write\n", "permissions"),
    ],
)  # fmt: skip
def test_each_check_catches_a_planted_defect(old, new, why):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count(old) == 1, old  # L-11: a planted edit that matches nothing proves nothing
    found = problems(text.replace(old, new))
    assert any(why in p for p in found), found


def _script(name: str) -> str:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    [i] = _named(data, name)
    return str(_steps(data)[i]["run"])


def _bash(script: str, cwd: Path, env: dict[str, str]) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    full = {**os.environ, **env}
    subprocess.run([bash, "-eo", "pipefail", "-c", script], cwd=cwd, env=full, check=True)


NEEDS_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="the runner has bash")


@NEEDS_BASH
def test_the_spend_line_is_only_what_this_run_appended(tmp_path):
    ledger = tmp_path / "docs" / "measurements" / "spend-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"run_id": "old-1"}\n{"run_id": "old-2"}\n', encoding="utf-8")
    env = {"RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    _bash(_script("Ledger length before the run"), tmp_path, env)
    with ledger.open("a", encoding="utf-8") as fh:  # what the smoke run appends
        fh.write('{"run_id": "smoke-new"}\n')
    _bash(_script("Spend line"), tmp_path, env)
    assert (tmp_path / "summary.md").read_text("utf-8") == '{"run_id": "smoke-new"}\n'
    assert (tmp_path / "spend-line.jsonl").read_text("utf-8") == '{"run_id": "smoke-new"}\n'
    (tmp_path / "summary.md").unlink()
    _bash(_script("Ledger length before the run"), tmp_path, env)  # a run that appended nothing
    _bash(_script("Spend line"), tmp_path, env)
    assert (tmp_path / "summary.md").read_text("utf-8") == ""


def _checkout_after_a_run(root: Path, run_id: str) -> Path:
    run = root / "results" / run_id / "metrics"
    run.mkdir(parents=True)
    for f in ("metrics.json", "cases.jsonl"):
        shutil.copyfile(GOLDEN / "metrics" / f, run / f)
    (root / "results" / run_id / "run.json").write_text("{}", encoding="utf-8")
    (root / "results" / "latest").write_text(run_id + "\n", encoding="utf-8")
    (root / "gate.json").write_text('{"exit_code": 0}', encoding="utf-8")
    (root / "spend-line.jsonl").write_text(f'{{"run_id": "{run_id}"}}\n', encoding="utf-8")
    return root


@NEEDS_BASH
def test_two_ci_artifacts_compare_with_evalkit(tmp_path):
    """The collected artifact is a folder `evalkit compare` reads (metrics.json beside its
    sidecar, no run.json), so two downloaded CI runs compare paired on shared questions."""
    arts = []
    for run_id in ("smoke-a", "smoke-b"):
        co = _checkout_after_a_run(tmp_path / run_id, run_id)
        _bash(_script("Collect the artifact"), co, {})
        art = co / ARTIFACT_DIR
        assert sorted(p.name for p in art.iterdir()) == ["gate.json", "metrics", "spend-line.jsonl"]
        assert sorted(p.name for p in (art / "metrics").iterdir()) == [
            "cases.jsonl",
            "metrics.json",
        ]
        arts.append(art)
    c = compare_runs(arts[0] / "metrics", arts[1] / "metrics")
    assert c.exit_code == 0 and not c.errors
    acc = [ch for ch in c.changes if ch.metric == "accuracy"]
    assert acc and all(ch.pairing == "paired" for ch in acc)


@NEEDS_BASH
def test_the_artifact_step_survives_a_run_that_wrote_nothing(tmp_path):
    _bash(_script("Collect the artifact"), tmp_path, {})
    assert list((tmp_path / ARTIFACT_DIR).iterdir()) == []
