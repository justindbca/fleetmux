"""auto-handoff relays the way the handed-off session was started (fake cmux; installed pi for the TS part)."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / 'launchers/start-pi-autohandoff.command'


def _pi_root():
    """The globally installed Pi package: $PI_TEST_PACKAGE_DIR / $FLEETMUX_PI_ROOT, else <node prefix>/lib/node_modules/..."""
    for name in ('PI_TEST_PACKAGE_DIR', 'FLEETMUX_PI_ROOT'):
        if os.environ.get(name):
            return Path(os.environ[name])
    node = shutil.which('node')
    prefix = Path(node).resolve().parents[1] if node else Path('/nonexistent')
    return prefix / 'lib/node_modules/@earendil-works/pi-coding-agent'


PI_ROOT = _pi_root()


def relay_command(tmp_path, extra_env):
    launchers = tmp_path / 'launchers'
    launchers.mkdir()
    shutil.copy(RELAY, launchers / RELAY.name)
    canon = launchers / 'start-codex-flashnext.command'
    canon.write_text('#!/bin/bash\nIN_TERMINAL=0\n')
    canon.chmod(0o755)
    record = tmp_path / 'cmux-args'
    cmux = tmp_path / 'cmux'
    cmux.write_text(f'#!/bin/bash\n[ "$1" = ping ] && exit 0\nprintf "%s\\n" "$@" > {record}\n')
    cmux.chmod(0o755)
    handoff = tmp_path / 'handoff.md'
    handoff.write_text('# handoff\n')
    env = {**os.environ, 'CMUX_BIN': str(cmux), 'FLEETMUX_PI_DIR': str(tmp_path / 'none'), 'PI_WORKDIR': str(tmp_path),
           **extra_env}
    env.pop('PI_CANONICAL_LAUNCHER', None)
    subprocess.run(['/bin/bash', str(launchers / RELAY.name), str(handoff)], env=env, check=True, timeout=30)
    args = record.read_text().splitlines()
    return args[args.index('--command') + 1]


def test_relaunch_script_is_used_whatever_the_model(tmp_path):
    script = tmp_path / 'sid.relaunch.sh'
    script.write_text('#!/bin/bash\n')
    command = relay_command(tmp_path, {'PI_HANDOFF_RELAUNCH': str(script),
                                       'PI_AUTO_HANDOFF_MODEL': 'opencode-cli/deepseek-v4.1-flash-free'})
    assert command == f'/bin/bash {script}'


def test_without_relaunch_script_the_old_default_launcher(tmp_path):
    assert 'start-codex-flashnext.command' in relay_command(tmp_path, {'PI_AUTO_HANDOFF_MODEL': 'opencode-cli/x'})


def test_relaunch_arguments_env_and_script(tmp_path):
    if not (PI_ROOT / 'node_modules/@earendil-works/pi-ai').exists():
        pytest.skip('installed Pi required; set PI_TEST_PACKAGE_DIR')
    node = shutil.which('node') or pytest.skip('Node required')
    modules = tmp_path / 'node_modules'
    (modules / '@earendil-works').mkdir(parents=True)
    for entry in (PI_ROOT / 'node_modules').iterdir():
        if entry.name != '@earendil-works':
            (modules / entry.name).symlink_to(entry, target_is_directory=True)
    for entry in (PI_ROOT / 'node_modules/@earendil-works').iterdir():
        (modules / '@earendil-works' / entry.name).symlink_to(entry, target_is_directory=True)
    (modules / '@earendil-works/pi-coding-agent').symlink_to(PI_ROOT, target_is_directory=True)
    shutil.copy(ROOT / 'extensions/auto-handoff.ts', tmp_path / 'auto-handoff.ts')
    probe = tmp_path / 'probe.mjs'
    probe.write_text('''
const m = await import(process.argv[2]);
const args = m.relaunchArgs(['--thinking', 'off', '--model', 'opencode-cli/old', '--provider', 'opencode-cli',
  '--models', 'opencode-cli/x', '--extension', '/a/opencode-free/index.ts', '-e', '/b.ts', '--session-dir', '/s',
  '--session', 'abc', '-c', '--api-key', 'sk-1', '--model=zz', 'hello', '--', 'more words'], 'opencode-cli/x');
const env = m.relaunchEnv({ PI_OPENCODE_BIN: '/o', PATH: '/bin', TZ: 'Asia/Shanghai', OPENCODE_API_KEY: 'k',
  PI_HANDOFF_FILE: '/old.md', PI_AUTO_HANDOFF_MODEL: 'm', GH_TOKEN: 't', HOME: '/h', PI_AUTO_HANDOFF_PCT: '80' });
const script = m.relaunchScript(process.argv[3], { PI_X: "it's $HOME `x`" }, ['/bin/sh', '-c', 'pwd; printf %s "$PI_X"']);
console.log(JSON.stringify({ args, env, script }));
''')
    out = subprocess.run([node, '--experimental-strip-types', str(probe), str(tmp_path / 'auto-handoff.ts'), str(tmp_path)],
                         capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got['args'] == ['--thinking', 'off', '--provider', 'opencode-cli', '--models', 'opencode-cli/x',
                           '--extension', '/a/opencode-free/index.ts', '-e', '/b.ts', '--session-dir', '/s',
                           '--model', 'opencode-cli/x']
    assert got['env'] == {'PI_OPENCODE_BIN': '/o', 'PATH': '/bin', 'TZ': 'Asia/Shanghai', 'PI_AUTO_HANDOFF_PCT': '80'}
    script = tmp_path / 'r.sh'
    script.write_text(got['script'])
    ran = subprocess.run(['/bin/bash', str(script)], capture_output=True, text=True, timeout=30, cwd='/')
    assert ran.stdout == f"{tmp_path.resolve()}\nit's $HOME `x`"
