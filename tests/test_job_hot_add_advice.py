"""A worker the program adds for a queued job is never held for the advisor's first sheet (D36; Studio 2026-11-22).

With the advisor on, spawn_ready holds a new Claude worker behind its launch gate until the advisor's first sheet,
and a sheet only comes with a delivery. A worker added for a job (Fleet.job_hot_add) gets no delivery before
job_start adopts it, and job_start needs it idle and READY: held, the job waits forever. The supervisor_slot of a
named sub-fleet is already exempt (10-14 Studio deadlock); the job's added worker joins it. An ordinary new worker is
still held (control). An attached child's added worker is covered by the supervisor_slot exemption already (check).

Only private test Redis, fake cmux, fake CLIs (fixtures of test_service_job / test_service_attach); the probe cache
points at a missing file, so no real cache or fleet data is read."""
import pytest

from test_service_job import GOAL, jobs, job_rec, submit  # noqa: F401
from test_service_job import split_tree  # noqa: F401  (fixture)
from test_subfleet import hook, step, svc, write_roster  # noqa: F401  (fixture)
from fleetmux import discover, named_subfleet, subfleet

pytestmark = pytest.mark.usefixtures('job_chains_as_before')  # the job tests' model chains: the slot keeps its model


@pytest.fixture(autouse=True)
def no_probe_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(discover.CACHE_ENV, str(tmp_path / 'no-discovered.yaml'))


def root_roster(f, tmp_path):
    """The root roster with the advisor switch on (`advisor: on`): every roster reload sets the switch from the file,
    so setting cfg['advisor'] by hand would be undone by the reload that starts the new worker."""
    path = tmp_path / 'root-roster.yaml'
    path.write_text('# root roster\nadvisor: on\nworkers:\n  - {name: worker-1}\n  - {name: worker-2}\n')
    f.cfg['roster'] = str(path)
    f.tick()  # the roster is followed from now on
    assert f.advisor_online()
    return path


def test_a_worker_added_for_a_queued_job_is_not_held_and_the_job_starts(split_tree, tmp_path):
    t, f = split_tree, split_tree.f
    root_roster(f, tmp_path)
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='busy')
    jid = submit(f, t=t)['job_id']
    f.tick()
    assert jobs(f)[jid]['hot_slot'] == 'worker-3'
    f.tick()  # the roster reload starts it (spawn_ready)
    info = f.agent('worker-3')
    assert info.get('advice_hold') != '1' and info['launch_ready'] == '1', info  # not held for a sheet
    assert hook(t, f, 'worker-3', 'READY', prompt='startup')['status'] == 'ready'
    step(t)
    for _ in range(2):
        f.tick()
        step(t)
    key, rec = job_rec(f, jid)
    assert rec['slot'] == 'worker-3' and rec['status'] == 'delegated' and jobs(f)[jid]['state'] == 'running'
    assert f.agent('worker-3')['state'] == 'delegated'
    subfleet.stop_children(f, wait=0)


def test_an_ordinary_new_worker_is_still_held_for_its_first_sheet(split_tree, tmp_path):
    """Control: not every new worker skips the advisor; one the roster adds on its own waits for its sheet."""
    t, f = split_tree, split_tree.f
    path = root_roster(f, tmp_path)
    path.write_text(path.read_text() + '  - {name: worker-9}\n')
    f.tick()  # the roster reload starts it
    assert 'worker-9' in f.workers
    info = f.agent('worker-9')
    assert info['advice_hold'] == '1' and info['launch_ready'] == '0', info


@pytest.fixture
def attach_root(svc):
    """test_service_attach's root, with `advisor: on` in its roster (the reload that starts an added worker reads it)."""
    import os
    path = write_roster(svc, declare=False)
    path.write_text('advisor: on\n' + path.read_text())
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    svc.f.roster_tick()
    assert svc.f.advisor_online()
    return svc


def test_a_worker_added_for_an_attached_child_is_not_held(attach_root):
    """Check (4): the attached child's added worker is its supervisor_slot from the moment attach_tick picks the
    name (before the roster reload starts it), so spawn_ready's named_subfleet.reserved exemption covers it."""
    from test_service_attach import op
    t, f = attach_root, attach_root.f
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='busy')
    _, answer = op(t, 'attach')
    assert answer['status'] == 'accepted', answer
    subfleet.tick(f)
    assert named_subfleet.attached(f)['docs']['hot_slot'] == 'worker-3'
    assert named_subfleet.reserved(f, 'worker-3') == 'docs'
    f.roster_tick()  # the reload starts it
    assert f.advisor_online()
    info = f.agent('worker-3')
    assert info.get('advice_hold') != '1' and info['launch_ready'] == '1', info
    assert hook(t, f, 'worker-3', 'READY', prompt='startup')['status'] == 'ready'
    step(t)
    subfleet.tick(f)
    assert subfleet.record(f.run_dir / 'sub' / 'docs')['slot'] == 'worker-3'
    subfleet.stop_children(f, wait=0)
