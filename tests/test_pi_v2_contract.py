import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def _pi_root():
    """The globally installed Pi package: $FLEETMUX_PI_ROOT, else <node prefix>/lib/node_modules/..."""
    if os.environ.get('FLEETMUX_PI_ROOT'):
        return Path(os.environ['FLEETMUX_PI_ROOT'])
    node = shutil.which('node')
    prefix = Path(node).resolve().parents[1] if node else Path('/nonexistent')
    return prefix / 'lib/node_modules/@earendil-works/pi-coding-agent'


PI_ROOT = _pi_root()


def test_actual_v2_extension_contract(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    version = subprocess.check_output([node, '--version'], text=True).strip().lstrip('v')
    if tuple(map(int, version.split('.'))) < (22, 19, 0):
        pytest.skip('Node >=22.19 required')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'fleetmux/pi_extension_v2.ts', extension)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    records = tmp_path / 'bridge-records.jsonl'
    child = tmp_path / 'bridge-fixture.py'
    child.write_text('''import json,sys,pathlib
records=pathlib.Path(sys.argv[1])
late=silent=False
for line in sys.stdin:
 d=json.loads(line)
 records.open('a').write(json.dumps(d)+'\\n')
 if d['kind']=='fixture' and d['event']=='settle_pending':
  late=True
  continue
 if d['kind']=='fixture' and d['event']=='settle_silent':
  silent=True
  continue
 if d['kind']=='settled' and silent:  # the bridge never answers in time (busy on an earlier command)
  silent=False
  continue
 if d['kind']=='fixture':
  if d['event']=='ui': frame={'kind':'ui','event_id':d['event_id'],'card':{'render':d['render']}}
  elif d['event']=='completed': frame={'kind':'completed','message_id':d['message_id']}
  elif d['event']=='fault': frame={'kind':'fault','reason':d['reason']}
  else: frame={'kind':'delivery','delivery':{'run_id':'test-run','slot':d.get('slot','worker-1'),'generation':0,'session_id':'session-test',
   'message_id':d['message_id'],'delivery_id':'d-test','stream_id':'1-0','fence':1,
   'body':{'text':'业务 JSON {"amount":2} /tmp/report.md https://example.org','title':'派工任务','mode':'service','purpose':'worker','reply':{'required':['reply'],'allowed':['reply']}}}}
  print(json.dumps(frame),flush=True)
  continue
 status='registered' if d['kind']=='init' else 'ready' if d['kind']=='startup' else 'recorded'
 if d['kind']=='settled' and late: status,late='pending',False  # a busy daemon (design §47)
 print(json.dumps({'kind':'reply','call_id':d.get('call_id'),'answer':{'status':status}}),flush=True)
 if d['kind']=='shutdown': break
''')
    env = dict(os.environ, FLEETMUX_PI_BRIDGE=json.dumps([sys.executable,'-u',str(child),str(records),'worker-1','0']),
               FLEETMUX_PI_SLOT='worker-1', FLEETMUX_PI_MODE='service')
    result = subprocess.run([node,'--experimental-strip-types',str(root/'tests/test_pi_extension_v2.mjs'),str(extension),str(records)],
        env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'persistent bridge contract passed' in result.stdout
