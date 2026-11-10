"""Real Pi TUI + the real pi_extension_v2.ts + offline provider + stub bridge: one fleet delivery.

Checks the run that Pi starts for the extension's sendMessage(triggerTurn) delivery is accepted:
fleet_result is recorded, then started -> candidate -> settled reach the bridge.
Uses only an owned temporary PTY / Pi agent dir; no cmux, Redis, real model or ~/.pi.
python3 tests/acceptance_pi_v2_delivery.py --pi /absolute/pi [--pi /other/pi ...]
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
import sys
import tempfile
import termios
import time

BRIDGE = r'''import json,sys,pathlib
records=pathlib.Path(sys.argv[1]); offered=False
def out(frame): print(json.dumps(frame),flush=True)
for line in sys.stdin:
    d=json.loads(line); records.open('a').write(json.dumps(d)+'\n')
    status={'init':'registered','startup':'ready'}.get(d['kind'],'recorded')
    out({'kind':'reply','call_id':d.get('call_id'),'answer':{'status':status}})
    if d['kind']=='availability' and d.get('idle') is True and not offered:
        offered=True
        out({'kind':'delivery','delivery':{'run_id':'acceptance','slot':'worker-1','generation':0,
             'session_id':d['thread'],'message_id':'m-acceptance','delivery_id':'d-acceptance','stream_id':'1-0','fence':1,
             'body':{'text':'DELIVERY_BODY_SENTINEL: return OK','title':'派工任务','mode':'service','purpose':'worker'}}})
    if d['kind']=='settled': out({'kind':'completed','message_id':'m-acceptance'})
    if d['kind']=='shutdown': break
'''


def run(pi):
    root_dir = Path(__file__).resolve().parents[1]
    version = subprocess.run([pi, '--version'], capture_output=True, text=True, timeout=30,
                             env=dict(os.environ, PI_SKIP_VERSION_CHECK='1')).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='fleet-pi-v2-delivery-') as directory:
        root = Path(directory)
        home = root / 'agent'
        home.mkdir()
        (home / 'settings.json').write_text(json.dumps({
            'quietStartup': True, 'cacheWarming': 'off', 'compaction': {'enabled': False},
            'retry': {'enabled': False}, 'enableInstallTelemetry': False, 'enableAnalytics': False}))
        records, bridge = root / 'bridge.jsonl', root / 'bridge.py'
        bridge.write_text(BRIDGE)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 34, 100, 0, 0))
        argv = [pi, '--model', 'fleet-v2-fixture/fixture', '--thinking', 'off', '--tui-mode', 'regular',
                '--no-extensions', '--extension', str(root_dir / 'cmux_fleet/pi_extension_v2.ts'),
                '--extension', str(Path(__file__).with_name('pi_v2_offline_provider_fixture.ts').resolve()),
                '--no-mcp', '--no-skills', '--no-prompt-templates', '--no-themes', '--no-context-files',
                '--no-approve', '--offline', '--tools', 'read,fleet_result',
                '--session-dir', str(root / 'sessions'), '--name', 'worker-1', '--', 'startup: reply READY']
        env = dict(os.environ, PI_CODING_AGENT_DIR=str(home), PI_SKIP_VERSION_CHECK='1', TERM='xterm-256color',
                   CMUX_FLEET_PI_BRIDGE=json.dumps([sys.executable, '-u', str(bridge), str(records), 'worker-1', '0']),
                   CMUX_FLEET_PI_SLOT='worker-1', CMUX_FLEET_PI_MODE='service')
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=root, env=env,
                                start_new_session=True)
        os.close(slave)
        output, kinds = bytearray(), []
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
                if records.exists():
                    rows = [json.loads(x) for x in records.read_text().splitlines() if x.strip()]
                    kinds = [r['kind'] for r in rows]
                    if 'settled' in kinds:
                        break
                if proc.poll() is not None:
                    break
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
            os.close(master)
        rows = [json.loads(x) for x in records.read_text().splitlines() if x.strip()] if records.exists() else []
    settled = [r for r in rows if r['kind'] == 'settled']
    ok = bool(settled) and settled[0]['candidates'] == [{'tool': 'fleet_result', 'value': {'reply': 'OK'}}] \
        and 'started' in kinds and False not in [r.get('idle') for r in rows if r['kind'] == 'availability']
    return {'pi_version': version, 'passed': ok, 'bridge_kinds': kinds,
            'candidate_errors': [r['candidate'] for r in rows if r['kind'] == 'candidate' and 'error' in r['candidate']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pi', action='append', required=True)
    args = parser.parse_args()
    results = [run(pi) for pi in args.pi]
    print(json.dumps(results, ensure_ascii=False, indent=1))
    sys.exit(0 if all(r['passed'] for r in results) else 1)


if __name__ == '__main__':
    main()
