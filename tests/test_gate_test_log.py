"""The dev_task gate keeps the full test output (fix/gate-test-log).

Today ``lifecycle_deliver.deliver`` keeps only the last 1500 characters of stdout+stderr in
``rec['tests']['tail']``; a failing test's first error, or a success's build log, is gone, and a timeout
or a start failure records no output at all.  The contract these tests pin:

* the full combined output goes to ``<run>/gate/<task_id>-tests.log`` (``run`` is the run directory the
  daemon passes); without ``run`` the file is ``<workspace>.parent/gate/<task_id>-tests.log`` — beside the
  workspace, never inside it;
* ``rec['tests']['tail']`` keeps its old meaning: the last 1500 **characters** of the combined output;
* only the ``lifecycle.gate`` audit record's ``tests.tail`` is the last 20 **lines** of that tail (the
  record/result.json stay small, the file keeps everything);
* a timeout keeps the partial stdout/stderr it got before the kill (``returncode`` is None), accepting both
  ``bytes`` and ``str`` partial output;
* a start failure is a gate problem, never an exception, and still writes a log with the command, cwd,
  start time and the start exception;
* the log header records command, cwd, start time and the exit code / timeout;
* a log larger than 20 MB is cut to the first 1 MiB + the last 19 MiB with a visible truncation marker
  (a little header/marker overhead is allowed);
* a log that cannot be written never changes the gate outcome; ``rec['problem']`` names the log failure and,
  for a failing command, still keeps ``tests failed (exit N)``.

Run: PYTHONPATH=$PWD python -m pytest -q tests/test_gate_test_log.py tests/test_lifecycle_close.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from test_lifecycle_close import (as_task, complete, deliver_spec, notes, repo, table,  # noqa: F401
                                  write_verdict)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fx fixture dependency)
from test_runtime_v2 import fx  # noqa: F401

from cmux_fleet import lifecycle_close, lifecycle_deliver

MiB = 1024 * 1024
LOG_CAP = 20 * MiB         # the log never grows past 20 MiB ...
LOG_HEAD = 1 * MiB         # ... keeping the first 1 MiB ...
LOG_TAIL = 19 * MiB        # ... and the last 19 MiB, with a marker in between.
SLACK = 64 * 1024          # header + truncation marker overhead


def _verdict(ws):
    (ws / 'reviewer').mkdir(parents=True, exist_ok=True)
    (ws / 'reviewer' / 'verdict.json').write_text(
        json.dumps({'round': 1, 'verdict': 'PASS', 'reviewer': 'opus', 'issues': []}))


def _log_path(base, task_id):
    return Path(base) / 'gate' / (task_id + '-tests.log')


def _gate(tmp_path, repo, code, *, task_id='t1', run=None, timeout=None, ws_name='ws', runner=None):
    """A dev_task gate whose test command is ``python -c code``; returns (spec, ws, record)."""
    spec = deliver_spec(repo, tmp_path, [sys.executable, '-c', code])
    if timeout is not None:
        spec['test_timeout_s'] = timeout
    ws = tmp_path / ws_name
    _verdict(ws)
    kwargs = {'task_id': task_id}
    if run is not None:
        kwargs['run'] = run
    if runner is not None:
        kwargs['runner'] = runner
    return spec, ws, lifecycle_deliver.deliver(spec, ws, **kwargs)


def _header(text, lines=15):
    return '\n'.join(text.splitlines()[:lines])


def _assert_header(text, repo, *, exit_line=None, timeout=False, start_failed=False):
    head = _header(text)
    assert 'cmd:' in head and 'cwd:' in head
    assert str(repo) in head                                   # cwd
    assert re.search(r'started:\s*\S*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', head)  # start time
    if exit_line:
        assert exit_line in head
    if timeout:
        assert re.search(r'timeout:\s*1', head)
    if start_failed:
        assert 'start failed' in head.lower()


def _lines_output(prefix, n, width):
    return ''.join(prefix + ' %03d %s\n' % (i, 'x' * width) for i in range(n))


# ------------------------------------------------------------------ full output, success and failure
def test_gate_keeps_the_full_output_of_a_passing_test(tmp_path, repo):
    run = tmp_path / 'run'
    run.mkdir()
    out = 'HEAD-MARK\n' + _lines_output('line', 200, 40) + 'TAIL-MARK\n'
    code = ('print("HEAD-MARK")\n'
            'for i in range(200):\n'
            '    print("line %03d %s" % (i, "x" * 40))\n'
            'print("TAIL-MARK")\n')
    spec, ws, rec = _gate(tmp_path, repo, code, run=str(run))
    assert rec['ok'], rec.get('problem')
    log = Path(rec['tests']['log'])
    assert log == _log_path(run, 't1') and log.is_absolute() and log.exists()
    text = log.read_text()
    assert len(text) > 1500                       # the old 1500-char slice lost the head
    assert 'HEAD-MARK' in text and 'TAIL-MARK' in text
    _assert_header(text, repo, exit_line='exit: 0')
    assert rec['tests']['returncode'] == 0
    assert rec['tests']['tail'] == out[-1500:]    # the record keeps its 1500-char tail
    assert len(rec['tests']['tail']) == 1500


def test_gate_keeps_the_head_of_a_failing_test(tmp_path, repo):
    run = tmp_path / 'run'
    run.mkdir()
    out = 'FIRST-ERROR\n' + _lines_output('line', 200, 40) + 'LAST-ERROR\n'
    code = ('print("FIRST-ERROR")\n'
            'for i in range(200):\n'
            '    print("line %03d %s" % (i, "x" * 40))\n'
            'print("LAST-ERROR")\n'
            'raise SystemExit(3)\n')
    spec, ws, rec = _gate(tmp_path, repo, code, run=str(run))
    assert not rec['ok'] and 'tests failed (exit 3)' in rec['problem']
    text = Path(rec['tests']['log']).read_text()
    assert 'FIRST-ERROR' in text and 'LAST-ERROR' in text   # head kept, not just the last 1500 chars
    _assert_header(text, repo, exit_line='exit: 3')
    assert rec['tests']['returncode'] == 3
    assert rec['tests']['tail'] == out[-1500:]


# ------------------------------------------------------------------ timeout / start failure
def test_gate_timeout_keeps_the_partial_stdout_and_stderr(tmp_path, repo):
    run = tmp_path / 'run'
    run.mkdir()
    code = ('import sys, time\n'
            'sys.stdout.write("OUT-BEFORE-TIMEOUT\\n"); sys.stdout.flush()\n'
            'sys.stderr.write("ERR-BEFORE-TIMEOUT\\n"); sys.stderr.flush()\n'
            'time.sleep(30)\n')
    spec, ws, rec = _gate(tmp_path, repo, code, run=str(run), timeout=1)
    assert not rec['ok'] and 'timed out' in rec['problem']
    assert 'tests' in rec and rec['tests']['returncode'] is None
    text = Path(rec['tests']['log']).read_text()
    assert 'OUT-BEFORE-TIMEOUT' in text and 'ERR-BEFORE-TIMEOUT' in text
    _assert_header(text, repo, timeout=True)
    assert rec['tests']['tail'] == ('OUT-BEFORE-TIMEOUT\n' + 'ERR-BEFORE-TIMEOUT\n')[-1500:]
    assert 'tests' not in rec['steps']


@pytest.mark.parametrize('out,err', [
    (b'OUT-BYTES\n', b'ERR-BYTES\n'),
    ('OUT-STR\n', 'ERR-STR\n'),
])
def test_gate_timeout_accepts_bytes_and_str_partial_output(tmp_path, repo, out, err):
    spec = deliver_spec(repo, tmp_path, [sys.executable, '-c', 'pass'])
    spec['test_timeout_s'] = 1
    ws = tmp_path / 'ws'
    _verdict(ws)

    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1, output=out, stderr=err)

    rec = lifecycle_deliver.deliver(spec, ws, task_id='t1', run=str(tmp_path / 'run'), runner=runner)
    assert not rec['ok'] and 'timed out' in rec['problem']
    assert rec['tests']['returncode'] is None
    text = Path(rec['tests']['log']).read_text()
    assert 'OUT-' in text and 'ERR-' in text      # bytes decoded, str kept: both become text
    out_text = out.decode() if isinstance(out, bytes) else out
    err_text = err.decode() if isinstance(err, bytes) else err
    assert rec['tests']['tail'] == (out_text + err_text)[-1500:]


def test_gate_start_failure_is_a_problem_and_writes_a_start_log(tmp_path, repo):
    binary = '/nonexistent/cmux-fleet-gate-test-binary'
    spec = deliver_spec(repo, tmp_path, [binary])
    ws = tmp_path / 'ws'
    _verdict(ws)
    run = tmp_path / 'run'
    rec = lifecycle_deliver.deliver(spec, ws, task_id='t1', run=str(run))   # never raises
    assert not rec['ok'] and 'tests could not start' in rec['problem']
    assert rec['tests']['returncode'] is None
    log = Path(rec['tests']['log'])               # the attempt is logged, not left empty
    assert log == _log_path(run, 't1') and log.exists()
    text = log.read_text()
    _assert_header(text, repo, start_failed=True)
    assert binary in text                          # the command that could not start
    assert 'No such file or directory' in text     # the start exception


# ------------------------------------------------------------------ 20 MB cap
def test_gate_log_is_capped_at_20mb_keeping_head_1mb_tail_19mb_and_a_marker(tmp_path, repo):
    run = tmp_path / 'run'
    run.mkdir()
    code = ('import sys\n'
            'sys.stdout.write(("H" * 1023 + "\\n") * 1024)\n'      # 1 MiB head
            'sys.stdout.write(("M" * 1023 + "\\n") * 2048)\n'      # 2 MiB middle, dropped
            'sys.stdout.write(("T" * 1023 + "\\n") * 19456)\n'     # 19 MiB tail
            'sys.stdout.write("LAST-LINE\\n")\n')
    spec, ws, rec = _gate(tmp_path, repo, code, run=str(run))
    assert rec['ok'], rec.get('problem')
    log = Path(rec['tests']['log'])
    size = log.stat().st_size
    assert size <= LOG_CAP + SLACK                # 20 MiB plus header/marker overhead
    text = log.read_text()
    assert ('H' * 1023 + '\n') * 1024 in text     # the first 1 MiB is kept
    assert text.rstrip('\n').endswith('LAST-LINE')  # the last 19 MiB is kept
    assert 'truncated' in text.lower()            # the cut is visible, never silent
    assert 'M' * 1023 not in text                 # the dropped middle is really gone


# ------------------------------------------------------------------ run fallback / unwritable log
def test_gate_log_falls_back_beside_the_workspace_without_a_run_dir(tmp_path, repo):
    spec = deliver_spec(repo, tmp_path, [sys.executable, '-c', 'print("NO-RUN-MARK")'])
    ws = tmp_path / 'ws'
    _verdict(ws)
    rec = lifecycle_deliver.deliver(spec, ws, task_id='t1')   # run defaults to ''
    assert rec['ok'], rec.get('problem')
    log = Path(rec['tests']['log'])
    assert log == _log_path(ws.parent, 't1')      # beside the workspace, not inside it
    assert log.is_absolute() and log.exists()
    assert ws.resolve() not in log.resolve().parents
    assert 'NO-RUN-MARK' in log.read_text()


def test_gate_log_write_failure_never_fails_the_gate_and_is_noted(tmp_path, repo):
    run_file = tmp_path / 'not-a-dir'
    run_file.write_text('a regular file cannot hold the log\n')
    spec, ws, rec = _gate(tmp_path, repo, 'print("OK")', run=str(run_file))
    assert rec['ok'] is True                      # the log is evidence, not the gate
    assert rec['tests']['log'] in (None, '')
    assert 'log' in rec['problem'].lower()        # ... but the missing evidence is reported


def test_gate_failure_keeps_the_exit_code_when_the_log_cannot_be_written(tmp_path, repo):
    run_file = tmp_path / 'not-a-dir'
    run_file.write_text('a regular file cannot hold the log\n')
    spec, ws, rec = _gate(tmp_path, repo, 'raise SystemExit(3)', run=str(run_file))
    assert rec['ok'] is False
    assert 'tests failed (exit 3)' in rec['problem']   # the real outcome survives
    assert 'log' in rec['problem'].lower()             # the log failure is named too
    assert rec['tests']['log'] in (None, '')


# ------------------------------------------------------------------ lifecycle.gate audit record
def test_lifecycle_gate_audit_carries_returncode_log_and_the_20_line_tail(fx, table, notes, repo, tmp_path):  # noqa: F811
    f = as_task(fx, 'dev_task')
    write_verdict(f)
    out = ''.join('line %02d %s\n' % (i, 'x' * 30) for i in range(40))   # > 1500 chars
    code = 'for i in range(40):\n    print("line %02d %s" % (i, "x" * 30))\n'
    complete(f, {'goal': 'ship', 'deliver': deliver_spec(repo, tmp_path, [sys.executable, '-c', code])})
    f.tick()
    assert f.task()['status'] == 'completed'
    tid = f.task()['id']
    gate = json.loads(f.r.hget(lifecycle_close.LIFE, 'gate:' + tid))
    assert gate['tests']['tail'] == out[-1500:]    # the record keeps its 1500-char tail
    recs = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'lifecycle.gate']
    assert len(recs) == 1
    meta = json.loads(recs[0]['tests'])
    assert meta['returncode'] == 0
    log = Path(meta['log'])
    assert log == _log_path(f.run_dir, tid) and log.is_absolute() and log.exists()
    assert log.read_text().splitlines()[-1] == 'line 39 ' + 'x' * 30
    tail = meta['tail'].splitlines()
    assert tail == gate['tests']['tail'].splitlines()[-20:]   # only the audit tail is 20 lines
    assert len(tail) == 20 and tail[-1] == 'line 39 ' + 'x' * 30 and tail[0] == 'line 20 ' + 'x' * 30
