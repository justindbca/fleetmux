"""Launcher contracts; no real model, credential store, or cmux access."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launchers/start-pi-opencode.command"


@pytest.fixture
def launcher(tmp_path):
    directory = tmp_path / "project with spaces" / "launchers"
    directory.mkdir(parents=True)
    script = directory / LAUNCHER.name
    shutil.copyfile(LAUNCHER, script)
    # Capture the contract to the shared wrapper, not its process-group cleanup.
    (directory / "start-pi-full.command").write_text(
        "#!/bin/bash\nexec python3 - \"$@\" <<'PY'\n"
        "import os, sys, json\n"
        "print(json.dumps({'argv':sys.argv[1:], 'model':os.environ['PI_MODEL'], "
        "'thinking':os.environ['PI_THINKING'], 'cwd':os.environ['PI_WORKDIR']}))\n"
        "PY\n"
    )
    env = os.environ.copy()
    for key in ("PI_OPENCODE_MODEL", "PI_OPENCODE_THINKING", "PI_WORKDIR"):
        env.pop(key, None)
    env.update(PI_MODEL="openai-codex/unwanted", PI_THINKING="high",
               PI_CODING_AGENT_DIR=str(tmp_path / "private pi"))
    return script, env


def run(launcher, *args, model=None, **overrides):
    script, env = launcher
    env = {**env, **overrides}
    if model is not None:
        env["PI_OPENCODE_MODEL"] = model
    return subprocess.run(["/bin/bash", str(script), *args], env=env,
                          text=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("model", [None, "deepseek-v4.1-flash-free",
                                   "opencode/deepseek-v4.1-flash-free"])
def test_no_silent_paid_fallback(launcher, model):
    result = run(launcher, model=model)
    assert result.returncode == 2
    assert "免费" in result.stderr or "仅允许" in result.stderr
    assert '"argv"' not in result.stdout


@pytest.mark.parametrize("model", ["opencode-go/example", "openai/example", "opencode/"])
def test_wrong_provider_rejected(launcher, model):
    assert run(launcher, model=model).returncode == 2


def test_pi_contract_and_argument_boundaries(launcher):
    result = run(launcher, "--continue", "a prompt with spaces", model="example-api-model")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.splitlines()[-1])
    assert output["model"] == "opencode/example-api-model"
    assert output["thinking"] == "off"
    assert output["cwd"] == str(launcher[0].parent.parent)
    assert output["argv"] == ["--provider", "opencode", "--session-dir",
                              launcher[1]["PI_CODING_AGENT_DIR"] + "/sessions/opencode",
                              "--continue", "a prompt with spaces"]


def test_check_only_no_spawn_or_secret_output(launcher):
    result = run(launcher, "--check-only", model="opencode/example-api-model",
                 OPENCODE_API_KEY="test-secret-not-for-output")
    assert result.returncode == 0
    assert "尚未验证" in result.stdout
    assert '"argv"' not in result.stdout
    assert "test-secret-not-for-output" not in result.stdout + result.stderr


def test_invalid_workdir(launcher, tmp_path):
    result = run(launcher, model="example-api-model", PI_WORKDIR=str(tmp_path / "missing"))
    assert result.returncode == 2


def test_bash_syntax():
    subprocess.run(["/bin/bash", "-n", str(LAUNCHER)], check=True)
