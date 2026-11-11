"""Free shell contracts: mocks only, no OpenCode login/model or production fleet."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'launchers/start-pi-deepseek-free.command'


def _pi_root():
    """The globally installed Pi package: $PI_TEST_PACKAGE_DIR / $FLEETMUX_PI_ROOT, else <node prefix>/lib/node_modules/..."""
    for name in ('PI_TEST_PACKAGE_DIR', 'FLEETMUX_PI_ROOT'):
        if os.environ.get(name):
            return Path(os.environ[name])
    node = shutil.which('node')
    prefix = Path(node).resolve().parents[1] if node else Path('/nonexistent')
    return prefix / 'lib/node_modules/@earendil-works/pi-coding-agent'


PI_ROOT = _pi_root()


def node_binary():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required')
    version = subprocess.check_output([node, '--version'], text=True).strip().lstrip('v')
    if tuple(map(int, version.split('.'))) < (22, 19, 0):
        pytest.skip('Node >=22.19 required')
    return node


def test_streaming_backend_contracts():
    # The proxy sub-tests set their own proxy; a proxy in the caller's environment must not leak in.
    env = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    result = subprocess.run([node_binary(), '--test', str(ROOT / 'tests/test_opencode_free_backend.mjs')],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_pi_free_provider(tmp_path):
    if not (PI_ROOT / 'node_modules/@earendil-works/pi-ai').exists():
        pytest.skip('installed Pi required; set PI_TEST_PACKAGE_DIR')
    target = tmp_path / 'extension'
    shutil.copytree(ROOT / 'extensions/opencode-free', target)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    result = subprocess.run([node_binary(), '--experimental-strip-types',
                             str(ROOT / 'tests/test_opencode_free_provider.mjs'), str(target / 'index.ts')],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def launcher(tmp_path):
    directory = tmp_path / 'project with spaces' / 'launchers'
    directory.mkdir(parents=True)
    script = directory / LAUNCHER.name
    shutil.copyfile(LAUNCHER, script)
    extension = directory.parent / 'extensions/opencode-free'
    extension.mkdir(parents=True)
    (extension / 'index.ts').write_text('// launcher fixture\n')
    (extension / 'backend.mjs').write_text('// launcher fixture\n')
    (extension / 'mcp-stub.mjs').write_text('// launcher fixture\n')
    (directory / 'start-pi-full.command').write_text(
        '#!/bin/bash\nexec python3 - "$@" <<\'PY\'\n'
        'import os,sys,json\n'
        'print(json.dumps({"argv":sys.argv[1:],"model":os.environ["PI_MODEL"],'
        '"thinking":os.environ["PI_THINKING"],"cwd":os.environ["PI_WORKDIR"]}))\nPY\n'
    )
    cmux = tmp_path / 'fake cmux'
    cmux.write_text(
        '#!/usr/bin/env python3\n'
        'import os,sys,json\n'
        'with open(os.environ["CMUX_TEST_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
        'if sys.argv[1]=="--version": print(os.environ.get("CMUX_TEST_VERSION","cmux 0.65.0"))\n'
        'elif sys.argv[1]=="ping": sys.exit(int(os.environ.get("CMUX_TEST_PING_EXIT","0")))\n'
        'elif sys.argv[1]=="new-workspace":\n'
        ' print(json.dumps({"cmux_argv":sys.argv[1:]}))\n'
        ' sys.exit(int(os.environ.get("CMUX_TEST_EXIT","0")))\n'
        'else: sys.exit(99)\n'
    )
    cmux.chmod(0o700)
    open_app = tmp_path / 'fake open'
    open_app.write_text(
        '#!/usr/bin/env python3\nimport os,sys,json\n'
        'with open(os.environ["CMUX_TEST_OPEN_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
        'sys.exit(int(os.environ.get("CMUX_TEST_OPEN_EXIT","0")))\n'
    )
    open_app.chmod(0o700)
    env = {**os.environ, 'PI_OPENCODE_BIN': '/usr/bin/true', 'PI_MODEL': 'paid/unwanted',
           'PI_CODING_AGENT_DIR': str(tmp_path / 'private pi'), 'CMUX_BIN': str(cmux),
           'CMUX_TEST_LOG': str(tmp_path / 'cmux-calls.jsonl'),
           'CMUX_OPEN_BIN': str(open_app), 'CMUX_TEST_OPEN_LOG': str(tmp_path / 'open-calls.txt')}
    env.pop('PI_WORKDIR', None)
    env.pop('PI_BIN', None)
    return script, env


def run_launcher(launcher, *args, **overrides):
    script, env = launcher
    return subprocess.run(['/bin/bash', str(script), *args], env={**env, **overrides},
                          text=True, capture_output=True, timeout=10)


def test_free_launcher_terminal_and_passthrough(launcher):
    result = run_launcher(launcher, '--terminal', '--continue', 'prompt with spaces')
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.splitlines()[-1])
    assert output['model'] == 'opencode-cli/deepseek-v4.1-flash-free'
    assert output['thinking'] == 'off'
    assert output['argv'][-2:] == ['--continue', 'prompt with spaces']
    assert '--extension' in output['argv']
    assert output['argv'][0:2] == ['--provider', 'opencode-cli']
    assert output['argv'][-3].endswith('/sessions/opencode-free')
    assert not Path(launcher[1]['CMUX_TEST_LOG']).exists()
    assert not Path(launcher[1]['CMUX_TEST_OPEN_LOG']).exists()


@pytest.mark.parametrize('args', [('--model', 'paid/model'), ('--provider=other',), ('--models', '*'), ('--api-key=x',)])
def test_free_launcher_rejects_override(launcher, args):
    assert run_launcher(launcher, *args).returncode == 2


def test_free_launcher_check_and_missing_cli(launcher, tmp_path):
    result = run_launcher(launcher, '--check-only', OPENCODE_API_KEY='do-not-print')
    assert result.returncode == 0
    assert '尚未验证' in result.stdout
    assert 'do-not-print' not in result.stdout + result.stderr
    calls = Path(launcher[1]['CMUX_TEST_LOG']).read_text().splitlines()
    assert [json.loads(line) for line in calls] == [['--version']]
    assert not Path(launcher[1]['CMUX_TEST_OPEN_LOG']).exists()
    assert run_launcher(launcher, PI_OPENCODE_BIN='/no/such/opencode').returncode == 127
    assert run_launcher(launcher, PI_WORKDIR=str(tmp_path / 'missing')).returncode == 2


def test_free_launcher_cmux_default_and_safe_command(launcher, tmp_path):
    prompt = '中文 "quotes"; $(touch SHOULD_NOT_EXIST)\nnext line'
    workdir = tmp_path / 'work dir'
    workdir.mkdir()
    result = run_launcher(launcher, '--continue', prompt, PI_WORKDIR=str(workdir),
                          OPENCODE_API_KEY='secret-not-in-command')
    assert result.returncode == 0, result.stderr
    argv = json.loads(result.stdout.splitlines()[-1])['cmux_argv']
    assert argv[:3] == ['new-workspace', '--name', 'pi · DeepSeek Free']
    assert argv[argv.index('--cwd') + 1] == str(workdir.resolve())
    assert argv[-2:] == ['--focus', 'true']
    command = argv[argv.index('--command') + 1]
    assert 'secret-not-in-command' not in command
    # Execute exactly what cmux would put in its login shell. The second invocation
    # must start Pi in place, preserving quotes/newlines without another workspace.
    child = subprocess.run(['/bin/bash', '-c', command], env=launcher[1], cwd=tmp_path,
                           text=True, capture_output=True, timeout=10)
    assert child.returncode == 0, child.stderr
    output = json.loads(child.stdout.splitlines()[-1])
    assert output['argv'][-2:] == ['--continue', prompt]
    assert output['cwd'] == str(workdir.resolve())
    assert not (tmp_path / 'SHOULD_NOT_EXIST').exists()
    calls = [json.loads(line) for line in Path(launcher[1]['CMUX_TEST_LOG']).read_text().splitlines()]
    assert [call[0] for call in calls] == ['--version', 'ping', 'new-workspace']
    assert json.loads(Path(launcher[1]['CMUX_TEST_OPEN_LOG']).read_text()) == ['-a', '/Applications/cmux.app']


@pytest.mark.parametrize('version', ['cmux 0.64.24', 'unknown'])
def test_free_launcher_rejects_old_or_unknown_cmux(launcher, version):
    assert run_launcher(launcher, '--check-only', CMUX_TEST_VERSION=version).returncode == 2


def test_free_launcher_missing_cmux_and_no_silent_fallback(launcher):
    assert run_launcher(launcher, CMUX_BIN='/missing/cmux').returncode == 127
    assert run_launcher(launcher, '--terminal', '--check-only', CMUX_BIN='/missing/cmux').returncode == 0
    assert run_launcher(launcher, CMUX_TEST_EXIT='7').returncode == 7
    assert not Path(launcher[1]['CMUX_TEST_OPEN_LOG']).exists()


def test_free_launcher_long_command_does_not_create_workspace(launcher):
    assert run_launcher(launcher, 'x' * 1000).returncode == 2
    calls = [json.loads(line) for line in Path(launcher[1]['CMUX_TEST_LOG']).read_text().splitlines()]
    assert calls == [['--version']]


@pytest.mark.parametrize('terminal', [False, True])
def test_project_root_compat_wrapper(launcher, tmp_path, terminal):
    project = tmp_path / 'business project'
    project.mkdir()
    wrapper = project / LAUNCHER.name
    shutil.copyfile(ROOT / 'launchers/compat' / LAUNCHER.name, wrapper)
    (tmp_path / 'cmux-fleet-pi').symlink_to(launcher[0].parent.parent, target_is_directory=True)
    args = ['--terminal', '--continue', 'prompt with spaces'] if terminal else ['--continue']
    result = run_launcher((wrapper, launcher[1]), *args)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.splitlines()[-1])
    if terminal:
        assert output['cwd'] == str(project.resolve())
        assert output['argv'][-2:] == ['--continue', 'prompt with spaces']
    else:
        argv = output['cmux_argv']
        assert argv[argv.index('--cwd') + 1] == str(project.resolve())
        command = argv[argv.index('--command') + 1]
        assert '/cmux-fleet-pi/launchers/' in command
    # Caller can explicitly choose a different project without editing the wrapper.
    result = run_launcher((wrapper, launcher[1]), '--terminal', PI_WORKDIR=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['cwd'] == str(tmp_path.resolve())


def test_compat_wrapper_missing_checkout(tmp_path):
    project = tmp_path / 'business'
    project.mkdir()
    wrapper = project / LAUNCHER.name
    shutil.copyfile(ROOT / 'launchers/compat' / LAUNCHER.name, wrapper)
    result = run_launcher((wrapper, os.environ.copy()), '--check-only')
    assert result.returncode == 127
    assert '找不到正式启动器' in result.stderr


@pytest.mark.parametrize('missing', ['launchers/start-pi-full.command',
                                     'extensions/opencode-free/index.ts',
                                     'extensions/opencode-free/backend.mjs',
                                     'extensions/opencode-free/mcp-stub.mjs'])
def test_incomplete_launcher_fails_before_cmux_even_in_check_only(launcher, missing):
    (launcher[0].parent.parent / missing).unlink()
    result = run_launcher(launcher, '--check-only')
    assert result.returncode == 127
    assert '缺少' in result.stderr
    assert not Path(launcher[1]['CMUX_TEST_LOG']).exists()


def test_native_command_open_when_external_socket_client_is_denied(launcher, tmp_path):
    prompt = '外部启动 "quotes"\n$(touch SHOULD_NOT_EXIST)'
    result = run_launcher(launcher, '--continue', prompt, CMUX_TEST_PING_EXIT='1', TMPDIR=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert '原生 .command' in result.stdout
    opened = json.loads(Path(launcher[1]['CMUX_TEST_OPEN_LOG']).read_text())
    assert opened[:2] == ['-a', '/Applications/cmux.app']
    bootstrap = Path(opened[2])
    assert bootstrap.name == 'pi-deepseek-free.command'
    assert bootstrap.stat().st_mode & 0o777 == 0o700
    assert bootstrap.parent.stat().st_mode & 0o777 == 0o700
    # Simulate the documented cmux .command handler, not a socket client or UI paste.
    child = subprocess.run(['/bin/bash', str(bootstrap)], env=launcher[1], cwd=tmp_path,
                           stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=10)
    assert child.returncode == 0, child.stderr
    output = json.loads(child.stdout.splitlines()[-1])
    assert output['argv'][-2:] == ['--continue', prompt]
    assert not bootstrap.parent.exists()
    assert not (tmp_path / 'SHOULD_NOT_EXIST').exists()
    calls = [json.loads(line) for line in Path(launcher[1]['CMUX_TEST_LOG']).read_text().splitlines()]
    assert calls == [['--version'], ['ping']]


def test_native_command_open_failure_cleans_bootstrap(launcher, tmp_path):
    result = run_launcher(launcher, CMUX_TEST_PING_EXIT='1', CMUX_TEST_OPEN_EXIT='1', TMPDIR=str(tmp_path))
    assert result.returncode == 1
    opened = json.loads(Path(launcher[1]['CMUX_TEST_OPEN_LOG']).read_text())
    assert not Path(opened[2]).parent.exists()


def test_external_launch_requires_version_with_native_command_support(launcher):
    result = run_launcher(launcher, CMUX_TEST_PING_EXIT='1', CMUX_TEST_VERSION='cmux 0.64.25')
    assert result.returncode == 2
    assert '0.65.0' in result.stderr
    assert not Path(launcher[1]['CMUX_TEST_OPEN_LOG']).exists()


def test_free_launcher_syntax():
    for script in (LAUNCHER, ROOT / 'launchers/compat' / LAUNCHER.name):
        subprocess.run(['/bin/bash', '-n', str(script)], check=True)
