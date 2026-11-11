"""The resume brief must fit one bus message however long the run has been (2026-11-09 incident).

A dev_task run with a few hundred accepted dispatches made `RuntimeV2.brief` queue a message over
message_bus.MAX_BODY: `start --resume` raised ValueError inside the resume transaction and the daemon died
on every restart. The full summary now goes to a file under <run>/handoffs and the queued brief points at it.
"""
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_resume import managed, task_run, half_done, crash_and_resume, answer_turn  # noqa: F401  (fixtures)
from test_v2_codex_claude import typed
from fleetmux import message_bus, runtime_v2

HUGE = 400000  # bytes of progress: twice the bus limit


def test_resume_with_a_huge_progress_summary_still_briefs_the_supervisor(task_run, monkeypatch):  # noqa: F811
    state = task_run
    half_done(state)
    real = runtime_v2.RuntimeV2.progress
    monkeypatch.setattr(runtime_v2.RuntimeV2, 'progress',
                        lambda self, task: real(self, task) + '\n' + ('- worker-9: earlier dispatch line ' * 12 + '\n') * 1000)
    f = crash_and_resume(state)  # raised ValueError('body/result exceeds 200000 UTF-8 bytes') before the fix
    assert answer_turn(f, 'supervisor', 'READY', startup=True)['status'] == 'ready'
    f.pump()
    brief = typed(f, 'supervisor')[-1]
    assert brief.startswith('[Resumed task ')
    assert len(brief.encode()) < message_bus.MAX_BODY
    files = sorted((f.run_dir / 'handoffs').glob('resume-brief-*.md'))
    assert files, 'the full summary must be kept on disk'
    full = files[-1].read_text()
    assert str(files[-1]) in brief and 'Read it' in brief
    assert len(full.encode()) > message_bus.MAX_BODY and full.startswith('[Resumed task ')
    assert 'Dispatch only work that is still missing' in brief  # the closing instruction survives


def test_a_normal_resume_brief_is_unchanged_and_writes_no_file(task_run):  # noqa: F811
    state = task_run
    half_done(state)
    f = crash_and_resume(state)
    assert answer_turn(f, 'supervisor', 'READY', startup=True)['status'] == 'ready'
    f.pump()
    brief = typed(f, 'supervisor')[-1]
    assert brief.startswith('[Resumed task ') and 'resume-brief-' not in brief
    assert not list((f.run_dir / 'handoffs').glob('resume-brief-*.md')) if (f.run_dir / 'handoffs').exists() else True
