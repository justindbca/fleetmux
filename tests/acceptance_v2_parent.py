"""Parent-owned adversarial runtime probes; only new private temporary Redis.

Not a production launcher. Run as a script during independent review. The
cmux/model boundary is fake; Redis, Lua intents, queue and result paths are real.
"""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import fleet
from cmux_fleet.agent_bridge import AgentBridge
from cmux_fleet.message_bus import BusError
from test_fleet import FakeMgr


def run():
    results = []
    with tempfile.TemporaryDirectory(prefix='fleet-parent-v2-') as directory:
        root = Path(directory)
        sock = '/tmp/cmux-fleet-parent-' + uuid.uuid4().hex[:10] + '.sock'
        proc = subprocess.Popen(['redis-server', '--port', '0', '--unixsocket', sock,
            '--unixsocketperm', '700', '--dir', directory, '--save', '', '--appendonly', 'no'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r = redis.Redis(unix_socket_path=sock, decode_responses=True, socket_timeout=5)
        runtime = None
        try:
            for _ in range(100):
                try:
                    r.ping()
                    break
                except redis.ConnectionError:
                    time.sleep(.02)
            else:
                raise RuntimeError('owned temporary Redis not ready')
            cfg = dict(run=str(root), redis_socket=sock, workers=['worker-1'], mode='service',
                python=sys.executable, supervisor_cli='pi', worker_cli='pi',
                transport_version=2, allow_development_v2=True,
                allow_unsandboxed_pi=True, pi_model='fixture/model', pi_home=str(root/'pi-home'),
                stall_timeout=0, request_timeout=600)
            path = root/'config.json'
            fleet.save(path, cfg)
            f = fleet.Fleet(path, r, FakeMgr())
            runtime = f.v2
            f.start_all()
            for slot in f.names:
                f.setf(slot, state='idle', thread='session-'+slot, bridge_available=1,
                       runtime_ready=1, idle_confirmed=1)
                runtime.bus.register_slot(slot, 0, 'session-'+slot)
            spec = {'id': 'race:1', 'to': 'worker-1', 'payload': {'question': 'OK'},
                    'reply': {'required': ['reply'], 'allowed': ['reply']}}
            runtime.transaction('parent-submit', lambda: f.request(spec))
            f.pump()
            env = runtime.bus.receive('worker-1', 'parent-consumer', block_ms=25,
                                      generation=0, session_id='session-worker-1')
            assert env and env['execution_allowed']
            event = dict(kind='settled', run_id=runtime.bus.run_id, agent='worker-1', generation=0,
                thread='session-worker-1', delivery=env,
                candidates=[{'tool': 'fleet_result', 'value': {'reply': 'OK'}}],
                failed=False, aborted=False, unfinished=False)
            original_apply = runtime.apply

            def expire_at_commit(intent):
                r.hset(runtime.bus.lease_key('worker-1'), 'lease_until', int(time.time()*1000)-1)
                runtime.bus.recover('worker-1', 'parent-consumer')
                return original_apply(intent)

            runtime.apply = expire_at_commit
            error = None
            try:
                runtime.transaction('parent-settle', lambda: runtime.handle(event))
            except (BusError, redis.RedisError) as exc:
                error = str(exc)
            finally:
                runtime.apply = original_apply
            message = runtime.bus.message(env['message_id'])
            public = json.loads(r.get(fleet.RESULT+'race:1') or '{}')
            results.append({'name': 'commit_rechecks_expired_lease_after_intent_preparation',
                'passed': public.get('status') != 'ok' and message.get('status') != 'committed',
                'message_state': message.get('status'), 'public_status': public.get('status'),
                'error': error})

            # A durable startup ACK is not evidence the Pi-side handler processed
            # that ACK. Do not enable inbox consumption until explicit idle/ready
            # availability arrives from Pi after its handler has armed delivery.
            bridge = AgentBridge(cfg, 'supervisor', 0, io.StringIO())
            try:
                bridge.binding = ('session-supervisor', str(root/'pi-sessions/supervisor/g0/test.jsonl'))
                bridge.metadata = lambda data: {}
                bridge.event = lambda *a, **kw: {'status': 'ready'}
                ack = bridge.command({'kind': 'startup', 'text': 'READY'})
                results.append({'name': 'startup_does_not_release_receiver_before_pi_ack_handling',
                    'passed': ack['status'] == 'ready' and not bridge.idle_event.is_set(),
                    'receiver_enabled_before_ack_written': bridge.idle_event.is_set()})
            finally:
                bridge.stop()
            assert not f.mgr.sent, 'v2 unexpectedly pasted terminal messages'
            assert not list(root.rglob('msg-*.md')), 'v2 unexpectedly wrote message files'
        finally:
            if runtime:
                runtime.close()
            r.close()
            proc.terminate()
            proc.wait(timeout=5)
            Path(sock).unlink(missing_ok=True)
    print(json.dumps({'scope': 'real Redis, fake cmux/model; independent adversarial probes',
                      'checks': results}, ensure_ascii=False, indent=2))
    return all(row['passed'] for row in results)


if __name__ == '__main__':
    raise SystemExit(0 if run() else 1)
