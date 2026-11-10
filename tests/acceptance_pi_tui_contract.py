"""Opt-in real Pi TUI capability probe with an offline deterministic provider.

Uses only an owned temporary PTY/home; no cmux, fleet, Redis, or real model.
python3 tests/acceptance_pi_tui_contract.py --pi /absolute/pi --output-dir /report/path
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pi', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fleet-tui-contract-') as directory:
        root = Path(directory)
        home = root / 'agent'
        home.mkdir()
        (home / 'settings.json').write_text(json.dumps({
            'quietStartup': True, 'cacheWarming': 'off',
            'compaction': {'enabled': False}, 'retry': {'enabled': False},
            'enableInstallTelemetry': False, 'enableAnalytics': False,
        }))
        record = root / 'fixture.json'
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 34, 100, 0, 0))
        argv = [args.pi, '--model', 'fleet-tui-fixture/fixture', '--thinking', 'off',
                '--tui-mode', 'regular', '--no-extensions', '--extension',
                str(Path(__file__).with_name('pi_tui_contract_fixture.ts').resolve()),
                '--no-mcp', '--no-skills', '--no-prompt-templates', '--no-themes',
                '--no-context-files', '--no-approve', '--offline',
                '--tools', 'fleet_contract_result', '--session-dir', str(root / 'sessions'),
                '--name', 'supervisor-1']
        env = dict(os.environ, PI_CODING_AGENT_DIR=str(home), PI_SKIP_VERSION_CHECK='1',
                   FLEET_CONTRACT_RECORD=str(record), TERM='xterm-256color')
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                cwd=root, env=env, start_new_session=True)
        os.close(slave)
        output = bytearray()
        sent = False
        settled_at = None
        result = {}
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b''
                    output.extend(data)
                    # Reply to cursor-position queries emitted by the real TUI.
                    if b'\x1b[6n' in data:
                        os.write(master, b'\x1b[1;1R')
                if record.exists():
                    try:
                        result = json.loads(record.read_text())
                    except json.JSONDecodeError:
                        continue
                    if result['phase'] == 'ready' and not sent:
                        assert result['requests'] == 0
                        time.sleep(.2)
                        os.write(master, '让 worker-1 回复 OK，请把结果转告我。'.encode() + b'\r')
                        sent = True
                    if result['phase'] == 'settled':
                        settled_at = settled_at or time.monotonic()
                        if time.monotonic() - settled_at >= .7:
                            break
                if proc.poll() is not None:
                    raise AssertionError('Pi exited before completion: ' + str(proc.returncode))
            assert result.get('phase') == 'settled', result
            assert result['mode'] == 'tui', result
            assert result['requests'] == 1, result
            assert result['bodySeen'] and not result['transportSeen'] and not result['uiSeen'], result
            assert result['boundaries'] == ['candidate', 'settled'], result
            text = output.decode('utf-8', errors='replace')
            assert 'worker-1：OK' in text, 'result card missing from real TUI'
            assert '处理超时' in text and '尚未确认停止' in text, 'timeout card missing from real TUI'
            for forbidden in ('TRANSPORT_ONLY_SENTINEL', 'BUSINESS_BODY_SENTINEL',
                              'PROTOCOL_ARGUMENT_SENTINEL', 'UI_ONLY_TIMEOUT_SENTINEL'):
                assert forbidden not in text, 'internal data displayed: ' + forbidden
            result.update(passed=True, real_pi_tui=True, real_model=False,
                          actual_fleet=False, infrastructure='owned PTY only')
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            (out / 'pi-tui-contract.ansi').write_bytes(output)
            (out / 'pi-tui-contract.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            os.close(master)


if __name__ == '__main__':
    main()
