"""Record the real Pi hook order for a fleet-style custom delivery run (offline provider).

Uses only an owned temporary PTY / Pi agent dir; no cmux, fleet, Redis, real model or ~/.pi.
python3 tests/record_pi_event_order.py --pi /absolute/pi --output tests/fixtures/pi_events_<version>.json
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
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    version = subprocess.run([args.pi, '--version'], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PI_SKIP_VERSION_CHECK='1')).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='fleet-pi-events-') as directory:
        root = Path(directory)
        home = root / 'agent'
        home.mkdir()
        (home / 'settings.json').write_text(json.dumps({
            'quietStartup': True, 'cacheWarming': 'off',
            'compaction': {'enabled': False}, 'retry': {'enabled': False},
            'enableInstallTelemetry': False, 'enableAnalytics': False,
        }))
        record = root / 'events.jsonl'
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 34, 100, 0, 0))
        argv = [args.pi, '--model', 'fleet-event-fixture/fixture', '--thinking', 'off',
                '--tui-mode', 'regular', '--no-extensions', '--extension',
                str(Path(__file__).with_name('pi_event_recorder_fixture.ts').resolve()),
                '--no-mcp', '--no-skills', '--no-prompt-templates', '--no-themes',
                '--no-context-files', '--no-approve', '--offline',
                '--tools', 'fleet_event_tool', '--session-dir', str(root / 'sessions'),
                '--name', 'supervisor-1', '--', 'startup: reply READY']
        env = dict(os.environ, PI_CODING_AGENT_DIR=str(home), PI_SKIP_VERSION_CHECK='1',
                   FLEET_EVENT_RECORD=str(record), TERM='xterm-256color')
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                cwd=root, env=env, start_new_session=True)
        os.close(slave)
        output = bytearray()
        events = []
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b''
                    output.extend(data)
                    if b'\x1b[6n' in data:
                        os.write(master, b'\x1b[1;1R')
                if record.exists():
                    events = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
                    if any(e['hook'] == 'done' for e in events):
                        time.sleep(.5)
                        events = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
                        break
                if proc.poll() is not None:
                    raise AssertionError('Pi exited early rc=%s: %s' % (
                        proc.returncode, output.decode('utf-8', 'replace')[-2000:]))
            assert any(e['hook'] == 'done' for e in events), (events, output.decode('utf-8', 'replace')[-2000:])
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            os.close(master)
    Path(args.output).write_text(json.dumps({
        'pi_version': version, 'recorded_with': 'tests/pi_event_recorder_fixture.ts (offline provider, real Pi TUI)',
        'events': events}, ensure_ascii=False, indent=1) + '\n')
    print(json.dumps({'pi_version': version, 'events': len(events)}))


if __name__ == '__main__':
    main()
