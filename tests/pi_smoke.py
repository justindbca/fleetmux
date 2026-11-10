"""Opt-in live Pi smoke, with a TEMPORARY PRIVATE Redis and fake cmux. No Claude calls.

python tests/pi_smoke.py --model local/model [--pi /path/to/pi] [--agent-dir ~/.pi/agent]
Requires a preconfigured model. Never reads/writes fleet's latest.json or a live fleet.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import fleet, pi_adapter
from test_fleet import FakeMgr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--pi', default=shutil.which('pi'))
    parser.add_argument('--redis-server', default=shutil.which('redis-server'))
    parser.add_argument('--agent-dir', default=str(Path.home() / '.pi/agent'))
    args = parser.parse_args()
    if not args.pi or not args.redis_server:
        parser.error('pi and redis-server must be installed')
    with tempfile.TemporaryDirectory(prefix='cmux-fleet-pi-smoke-') as tmp:
        root = Path(tmp)
        sock = '/tmp/cmux-fleet-smoke-' + uuid.uuid4().hex[:12] + '.sock'
        cfg = dict(run=str(root), python=sys.executable, redis_socket=sock, workers=['worker-1'],
                   mode='service', supervisor_cli='pi', worker_cli='pi', pi=args.pi,
                   pi_model=args.model, pi_thinking='off', allow_unsandboxed_pi=True,
                   pi_home=str(pi_adapter.prepare_home(root / 'pi-home', args.agent_dir)),
                   codex_home=str(root / 'unused-codex-home'))
        cfg_path = root / 'config.json'
        fleet.save(cfg_path, cfg)
        redis_proc = subprocess.Popen([args.redis_server, '--port', '0', '--unixsocket', sock,
                                       '--unixsocketperm', '700', '--save', '', '--appendonly', 'no',
                                       '--dir', tmp, '--logfile', str(root / 'redis.log')],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import time
            for _ in range(50):
                if redis_proc.poll() is not None:
                    raise RuntimeError('temporary Redis exited before READY:\n' + (root / 'redis.log').read_text())
                if not Path(sock).exists():
                    time.sleep(.1)
                    continue
                try:
                    r = fleet.connect(cfg)
                    break
                except Exception:
                    time.sleep(.1)
            else:
                raise RuntimeError('temporary Redis did not start')
            f = fleet.Fleet(cfg_path, r, FakeMgr())
            f.start_all()

            def run_prompt(name, prompt, resume=True):
                info = f.agent(name)
                gen = int(info['generation'])
                argv = pi_adapter.argv(cfg, name, gen, prompt, fleet.agent_cwd(cfg, name), label=f.label(name))
                options = ['--mode', 'json']
                if resume and info.get('transcript'):
                    options += ['--session', info['transcript']]
                argv[-2:-2] = options
                env = dict(os.environ, **pi_adapter.environment(cfg_path, cfg, name, gen))
                proc = subprocess.Popen(argv, cwd=fleet.agent_cwd(cfg, name), env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        start_new_session=True)
                try:
                    out, err = proc.communicate(timeout=180)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGTERM)
                    out, err = proc.communicate(timeout=10)
                    raise RuntimeError('Pi smoke prompt timed out: ' + name)
                if proc.returncode:
                    raise RuntimeError('Pi failed: ' + err[-2000:])
                count = 0
                while r.llen(fleet.EVENTS):
                    ev = json.loads(r.lpop(fleet.EVENTS))
                    count += ev['kind'] == 'turn'
                    f.handle(ev)
                if count != 1:
                    raise RuntimeError('expected one settled callback, got %s; stderr=%s; output=%s' %
                                       (count, err[-2000:], out[-3000:]))
                print('PASS settled callback:', name, 'generation', gen, flush=True)

            for name in f.names:
                run_prompt(name, (root / 'prompts' / (name + '-g0.md')).read_text(), resume=False)
                assert f.agent(name)['state'] == 'idle'
                assert f.usage(name)[0] > 0
            req = {'id': 'smoke1', 'to': 'worker-1', 'instruction': 'Return the payload value as answer. Do no other work.',
                   'payload': {'value': 7419}, 'reply': {'required': ['answer'], 'allowed': ['answer']}}
            f.request(req)
            f.pump()
            run_prompt('worker-1', f.mgr.sent[-1][1])
            result = json.loads(r.get(fleet.RESULT + 'smoke1') or '{}')
            assert result.get('status') == 'ok', result
            assert str(result['result']['answer']) == '7419', result
            print('PASS real Pi request -> validated JSON', flush=True)

            # Exercise a real handoff using a simulated threshold, not a giant model prompt.
            f.setf('worker-1', ctx_used=70000, ctx_window=100000)
            f.request(dict(req, id='smoke2'))
            f.pump()
            assert f.agent('worker-1')['state'] == 'handoff'
            run_prompt('worker-1', f.mgr.sent[-1][1])
            assert f.agent('worker-1')['generation'] == '1'
            assert f.agent('worker-1')['state'] == 'starting'
            run_prompt('worker-1', (root / 'prompts/worker-1-g1.md').read_text(), resume=False)
            f.pump()
            run_prompt('worker-1', f.mgr.sent[-1][1])
            result = json.loads(r.get(fleet.RESULT + 'smoke2') or '{}')
            assert result.get('status') == 'ok' and str(result['result']['answer']) == '7419', result
            print('PASS handoff -> new generation READY -> pending request completed', flush=True)
            assert not Path(cfg['codex_home']).exists()
            print('PASS no Codex home, no Claude invocation, no real cmux, no production Redis', flush=True)
        finally:
            redis_proc.terminate()
            try:
                redis_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                redis_proc.kill()
                redis_proc.wait(timeout=5)
            Path(sock).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
