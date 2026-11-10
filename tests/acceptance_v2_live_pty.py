"""Opt-in real Redis + Fleet + official Pi TUI + configured-model smoke.

cmux layout is FAKE; each actor instead owns a real PTY. Never reads/writes the
current fleet registry/latest.json or existing Redis. Only owned child groups
are stopped. Model access is explicit; there is no cloud fallback.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import fleet, pi_adapter
from test_fleet import FakeMgr


class PtyManager(FakeMgr):
    def __init__(self):
        super().__init__()
        self.children = {}
        self.lock = threading.RLock()

    def spawn_interactive_cli(self, **kw):
        with self.lock:
            answer = super().spawn_interactive_cli(**kw)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 130, 0, 0))
            args = shlex.split(kw['cli_command']) + [Path(kw['prompt_file']).read_text()]
            process = subprocess.Popen(args, cwd=kw['start_directory'],
                env=dict(os.environ, TERM='xterm-256color', **kw.get('env_vars', {})),
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            os.close(slave)
            child = {'process': process, 'master': master, 'output': bytearray(),
                     'label': kw['window_name'], 'stop': threading.Event()}
            self.children[answer.pane_id] = child
            thread = threading.Thread(target=self._read, args=(child,), daemon=True)
            child['thread'] = thread
            thread.start()
            return answer

    def _read(self, child):
        while not child['stop'].is_set():
            try:
                if select.select([child['master']], [], [], .1)[0]:
                    data = os.read(child['master'], 65536)
                    if not data:
                        return
                    child['output'].extend(data)
                    if b'\x1b[6n' in data:
                        os.write(child['master'], b'\x1b[1;1R')
                elif child['process'].poll() is not None:
                    return
            except OSError:
                return

    def send_prompt_to_idle(self, pane, text):
        raise AssertionError('v2 attempted legacy cmux paste')

    def type_operator(self, pane, text):
        os.write(self.children[pane]['master'], text.encode() + b'\r')

    def capture_to(self, pane, path):
        Path(path).write_bytes(self.children[pane]['output'])

    def get_pane_status(self, pane):
        child = self.children.get(pane)
        return pane if child and child['process'].poll() is None else None

    def kill_pane(self, pane):
        child = self.children.get(pane)
        if not child:
            return False
        process = child['process']
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        child['stop'].set()
        child['thread'].join(timeout=1)
        self.alive.discard(pane)
        self.killed.append(pane)
        return process.poll() is not None

    def close(self, out):
        for pane, child in list(self.children.items()):
            self.kill_pane(pane)
            (out / (child['label'] + '.ansi')).write_bytes(child['output'])
            os.close(child['master'])


def wait_for(check, errors, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if errors:
            raise RuntimeError('daemon failed: ' + repr(errors[0]))
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError('isolated live smoke condition did not become true')


def assistant_count(f, slot):
    path = f.agent(slot).get('transcript')
    if not path or not Path(path).exists():
        return 0
    rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
    return sum(row.get('type') == 'message' and row.get('message', {}).get('role') == 'assistant'
               for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pi', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--agent-dir', default=str(Path.home()/'.pi/agent'))
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--handoff', action='store_true')
    args = parser.parse_args()
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = {'real_pi_tui': True, 'real_redis': True, 'model': args.model,
              'cmux_layout': False, 'passed': False, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='fleet-v2-live-') as directory:
        root = Path(directory)
        sock = '/tmp/cmux-fleet-live-' + uuid.uuid4().hex[:10] + '.sock'
        home = pi_adapter.prepare_home(root/'pi-home', args.agent_dir)
        (home/'settings.json').write_text(json.dumps({'quietStartup': True, 'cacheWarming': 'off',
            'compaction': {'enabled': True}, 'retry': {'enabled': False},
            'enableInstallTelemetry': False, 'enableAnalytics': False}))
        cfg = dict(run=str(root), redis_socket=sock, python=sys.executable,
            mode='service', workers=['worker-1'], supervisor_cli='pi', worker_cli='pi',
            pi=args.pi, pi_model=args.model, pi_thinking='off', pi_home=str(home),
            allow_unsandboxed_pi=True, transport_version=2, allow_development_v2=True,
            stall_timeout=0, request_timeout=600, handoff_timeout=180, profile_ready_timeout=180,
            control_filters={}, control_width=120)
        config = root/'config.json'
        fleet.save(config, cfg)
        redis_process = subprocess.Popen(['redis-server', '--port', '0', '--unixsocket', sock,
            '--unixsocketperm', '700', '--dir', directory, '--save', '',
            '--appendonly', 'yes', '--appendfsync', 'everysec'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        manager = PtyManager()
        errors = []
        f = None
        thread = None
        try:
            def connect_new():
                try:
                    return fleet.connect(cfg)
                except Exception:
                    return None
            r = wait_for(connect_new, errors, 10)
            f = fleet.Fleet(config, r, manager)
            def daemon():
                try:
                    f.run()
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=daemon, daemon=True)
            thread.start()
            wait_for(lambda: all(f.agent(s).get('state') == 'idle' for s in f.names), errors)
            result['checks'].append('both actual Pi actors READY')
            before = {s: assistant_count(f, s) for s in f.names}
            manager.type_operator(f.agent('supervisor')['pane'],
                '让 worker-1 回复 OK，请把结果转告我。只做这个简单测试，不执行其他操作。')
            def operator_result():
                rows = [json.loads(raw) for raw in r.lrange(fleet.RESULTS, 0, -1)]
                return next((row for row in rows if row['id'].startswith('ui-')), None)
            public = wait_for(operator_result, errors)
            assert public['status'] == 'ok' and public.get('result', {}).get('reply') == 'OK', public
            pane = manager.children[f.agent('supervisor')['pane']]
            wait_for(lambda: 'worker-1：OK' in pane['output'].decode(errors='replace'), errors, 10)
            after = {s: assistant_count(f, s) for s in f.names}
            time.sleep(2)
            assert assistant_count(f, 'supervisor') == after['supervisor'], 'mechanical result woke supervisor'
            assert not list(root.rglob('msg-*.md')), 'v2 wrote inbox message files'
            result['checks'].extend(['real supervisor typed input -> worker structured result -> result card',
                                     'no message files / no cmux paste', 'result UI caused no extra supervisor response'])
            result['business_turn_counts'] = {'before': before, 'after': after}
            result['operator_result'] = public
            if args.handoff:
                # This is only the owned development runtime, not a saved production setting.
                wait_for(lambda: f.agent('worker-1').get('state') == 'idle', errors, 20)
                r.hset(fleet.key('agent', 'worker-1'), mapping={'ctx_used': '80000', 'ctx_window': '100000'})
                ack = fleet.request(cfg, {'kind': 'request', 'spec': {'id': 'handoff-check',
                    'to': 'worker-1', 'instruction': 'Return reply OK using fleet_result. Do no other work.',
                    'payload': {}, 'reply': {'required': ['reply'], 'allowed': ['reply']}}})
                assert ack['status'] == 'accepted', ack
                public2 = wait_for(lambda: json.loads(r.get(fleet.RESULT+'handoff-check') or 'null'), errors, 240)
                assert public2['status'] == 'ok' and public2['result']['reply'] == 'OK', public2
                assert int(f.agent('worker-1')['generation']) >= 1
                assert f.agent('worker-1')['label'] == 'worker-1.1'
                result['checks'].append('real actor handoff -> successor READY -> queued result (layout fake)')
            ack = fleet.request(cfg, {'kind': 'pause', 'reason': 'owned timeout display test'})
            assert ack['status'] == 'accepted', ack
            stable = {s: assistant_count(f, s) for s in f.names}
            ack = fleet.request(cfg, {'kind': 'request', 'spec': {'id': 'timeout-card-check',
                'to': 'worker-1', 'payload': {}, 'reply': {'required': ['reply']}, 'timeout_s': 600}})
            assert ack['status'] == 'accepted', ack
            # Simulate the elapsed wall clock only on our private test request.
            r.hset(fleet.REQ+'timeout-card-check', 'created', time.time()-601.8)
            timeout = wait_for(lambda: json.loads(r.get(fleet.RESULT+'timeout-card-check') or 'null'), errors, 10)
            assert timeout['status'] == 'timeout', timeout
            wait_for(lambda: '处理超时' in pane['output'].decode(errors='replace'), errors, 10)
            time.sleep(2)
            assert stable == {s: assistant_count(f, s) for s in f.names}, 'timeout card triggered a model turn'
            result['checks'].append('real timeout card with zero added model responses')
            result['timeout_result'] = timeout
            result['passed'] = True
        except Exception as exc:
            result['error'] = type(exc).__name__ + ': ' + str(exc)
            raise
        finally:
            if f:
                result['final_agents'] = {s: f.agent(s) for s in f.names}
                (out/'audit.json').write_text(json.dumps(f.v2.bus.audit_tail(200), ensure_ascii=False, indent=2))
                f.stopping = True
            if thread:
                thread.join(timeout=5)
            manager.close(out)
            if f:
                for path in root.rglob('*.jsonl'):
                    if 'pi-sessions' in path.parts:
                        target = out/'sessions'/path.relative_to(root)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(path.read_bytes())
                f.r.close()
            redis_process.terminate()
            redis_process.wait(timeout=5)
            Path(sock).unlink(missing_ok=True)
            (out/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
