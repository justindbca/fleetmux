"""A delegated worker's tab shows its sub-fleet supervisor's live session after a resume (docs/design.md section 17).

The slot runs in the fleet's own tmux (design section 44, worker-area mapping): the parent page's tab and the child
page's view are two clients of one session. A server restart kills both tabs but not the tmux server, so after a
resume:
- the parent opens a new client tab of that session (F1), but only while the child's current supervisor is still the
  adopted session; otherwise one audit record and no tab;
- the child keeps its adopted supervisor (no second generation) and opens a new view of the same session (F2).

The fake manager models what makes this go red: tabs die with the server while tmux sessions live on, and ``arrange``
forgets the registry entries of dead tabs (as backends/fleetmux.py ``_forget_dead_epochs``), so the old liveness
reattach cannot find the session of a pane id from before the restart.
"""
from pathlib import Path

import pytest

import test_subfleet as ts
from test_subfleet import die, start_tree
from fleetmux import fleet, subfleet
from fleetmux.backends import cmux


class TmuxWorld(ts.World):
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.sessions, self.client = set(), {}  # live tmux sessions; tab -> the session it is a client of

    def manager(self, cfg):
        name = cfg.get('session') or fleet.SESSION
        if name not in self.mgrs:
            self.mgrs[name] = TmuxMgr(self, name, cfg.get('agents_title') or 'Fleet agents')
        return self.mgrs[name]

    def restart_server(self):
        """The mux server restarts: every tab is gone, the tmux server and its sessions run on."""
        self.alive.clear()


class TmuxMgr(ts.WorldMgr):
    def __init__(self, world, session, title):
        super().__init__(world, session, title)
        self.registry = {}  # this manager's own record of its tmux client tabs

    def spawn_interactive_cli(self, **kw):
        res = super().spawn_interactive_cli(**kw)
        if kw.get('tmux_session'):
            self.client_of(res.pane_id, kw['tmux_socket'], kw['tmux_session'], 'owner')
        return res

    def client_of(self, pane, sock, session, role):
        self.w.sessions.add(session)
        self.w.client[pane] = session
        self.registry[pane] = {'tmux_socket': sock, 'tmux_session': session, 'tmux_role': role}

    def tmux_view_info(self, sid):
        tm = self.registry.get(sid)
        return {k: tm[k] for k in ('tmux_socket', 'tmux_session')} if tm else None

    def attach_view(self, info, peer):
        session = (info or {}).get('tmux_session')
        if not session or session not in self.w.sessions or not self.owns(peer):
            return None
        new = self._new('view')
        self.client_of(new, info.get('tmux_socket'), session, 'view')
        self.views = getattr(self, 'views', []) + [(new, session, peer)]
        return new

    def tmux_session_alive(self, sid):
        tm = self.registry.get(sid)
        return None if not tm else tm['tmux_session'] in self.w.sessions

    def reattach_client(self, sid, peer):
        tm = self.registry.get(sid)
        if not tm or not self.owns(peer) or self.owns(sid):
            return None
        new = self.attach_view(tm, peer)
        if new:
            self.registry[new] = self.registry.pop(sid)
        return new

    def arrange(self, sup, workers, url):
        for sid in [s for s in self.registry if s not in self.w.alive]:
            del self.registry[sid]  # backends/fleetmux.py _forget_dead_epochs: a dead tab's entry is gone
        return super().arrange(sup, workers, url)

    def _tag(self, sid, **info):
        super()._tag(sid, **info)
        if sid in self.registry:
            self.registry[sid].update({k: v for k, v in info.items() if isinstance(v, str)})


@pytest.fixture
def vtree(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, 'World', TmuxWorld)
    gen = start_tree(tmp_path, monkeypatch)
    t = next(gen)
    monkeypatch.setattr(cmux, 'tmux_sessions', lambda sock: sorted(t.world.sessions))
    yield t
    for _ in gen:
        pass


def into_tmux(t, slot):
    """``slot``'s current generation runs in the fleet's tmux (as after the approve's rotation, design section 44)."""
    f = t.f
    f.setf(slot, tmux_view=1)
    session = slot + '-g' + f.agent(slot)['generation']
    f.mgr.client_of(f.agent(slot)['pane'], subfleet.tmux_socket(f.cfg), session, 'owner')
    return session


def delegate(t):
    """worker-1 (in tmux) becomes the supervisor of sub-fleet M0; its child daemon maps the session on its page."""
    session = into_tmux(t, 'worker-1')
    rec, _ = ts.spawn_m0(t)
    assert rec['view'] == 1 and not rec['placeholder']  # the dual attach, no text placeholder
    cf = ts.child_daemon(t, rec)
    assert t.world.client[cf.agent('supervisor')['pane']] == session
    return rec, cf, session


def resume_parent(t):
    t.f.v2.close()
    again = fleet.Fleet(t.f.path, t.f.r, t.f.mgr, sessions=t.sessions)
    t.fleets[0] = again
    t.f = again
    again.resume_requested = True
    again.v2.resume_run()
    return again


def resume_child(t, rec):
    path = Path(rec['run']) / 'config.json'
    cfg = fleet.load(path)
    cf = fleet.Fleet(path, fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)
    t.fleets.append(cf)
    cf.resume_requested = True
    cf.v2.resume_run()
    return cf


def audits(f, event):
    out = []
    for _, fields in f.r.xrange(f.v2.bus.audit_key):
        fields = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                  for k, v in fields.items()}
        if fields.get('event') == event:
            out.append(fields)
    return out


def supervisor_spawns(mgr):
    return [kw for kw in getattr(mgr, 'spawned', []) if (kw.get('metadata') or {}).get('type') == 'supervisor']


# ------------------------------------------------------------------ brief item 6, first test (both sides)
def test_after_resume_the_delegated_slots_tab_shows_the_subfleet_supervisors_live_session(vtree):
    t = vtree
    rec, cf, session = delegate(t)
    t.world.restart_server()
    die(cf)
    f = resume_parent(t)
    child = resume_child(t, rec)
    tab = f.agent('worker-1')['pane']
    sup = child.agent('supervisor')
    assert tab in t.world.alive and f.agent('worker-1')['state'] == 'delegated'
    # the session behind the parent tab is the session of the child's current supervisor
    assert t.world.client.get(tab) == session == t.world.client.get(sup['pane'])
    assert sup['pane'] in t.world.alive and sup['generation'] == '0'
    assert subfleet.record(Path(rec['run']))['pane'] == tab  # collect looks there
    assert f.mgr.registry[tab]['tmux_role'] == 'owner'  # its retirement ends the session, as the first tab's
    assert len(audits(f, 'resume.delegated_view_reattached')) == 1
    # deliveries still reach it: the session's turn hook (the parent's worker-1, same generation) routes to the
    # child's supervisor generation 0, which the resume left idle (reattached), not a new generation
    route = subfleet.delegated_route(f.cfg, 'worker-1', int(f.agent('worker-1')['generation']))
    assert route[1:] == ('supervisor', 0) and sup['state'] == 'idle'


# ------------------------------------------------------------------ F2: the child keeps its adopted supervisor
def test_child_resume_keeps_the_adopted_supervisor_session_and_reopens_its_view(vtree):
    t = vtree
    rec, cf, session = delegate(t)
    child_mgr = cf.mgr
    before = len(supervisor_spawns(child_mgr))
    t.world.restart_server()
    die(cf)
    child = resume_child(t, rec)
    sup = child.agent('supervisor')
    assert sup['generation'] == '0' and child.label('supervisor') == fleet.SUPERVISOR_LABEL
    assert len(supervisor_spawns(child_mgr)) == before  # no second supervisor generation
    assert session in t.world.sessions  # the whole-page branch's leftover kill never ended it
    assert sup['pane'] in t.world.alive and t.world.client[sup['pane']] == session
    assert child_mgr.arranged[0] == sup['pane']  # the child's page is laid out around it
    assert sup['state'] == 'idle'  # reattached: deliverable again (runtime_v2 resume_run reattach branch)


def test_child_whose_adopted_session_ended_starts_its_next_supervisor_as_before(vtree):
    t = vtree
    rec, cf, session = delegate(t)
    t.world.restart_server()
    t.world.sessions.discard(session)
    die(cf)
    child = resume_child(t, rec)
    assert child.agent('supervisor')['generation'] == '1'


# ------------------------------------------------------------------ F1 condition (a)
def test_parent_opens_no_tab_when_the_child_supervisor_was_replaced(vtree):
    t = vtree
    rec, cf, session = delegate(t)
    cf.setf('supervisor', generation=1)  # the child already runs a plain-pane generation of its own
    old = t.f.agent('worker-1')['pane']
    t.world.restart_server()
    die(cf)
    f = resume_parent(t)
    assert f.agent('worker-1')['pane'] == old and old not in t.world.alive
    assert not getattr(f.mgr, 'views', [])
    lost = audits(f, 'resume.delegated_view_lost')
    assert len(lost) == 1 and 'no longer the adopted session' in lost[0]['reason']


def test_parent_opens_no_tab_when_the_session_ended(vtree):
    t = vtree
    rec, cf, session = delegate(t)
    t.world.restart_server()
    t.world.sessions.discard(session)
    die(cf)
    f = resume_parent(t)
    assert not getattr(f.mgr, 'views', [])
    assert len(audits(f, 'resume.delegated_view_lost')) == 1


def test_resume_without_delegated_slots_opens_no_view(vtree):
    t = vtree
    t.world.restart_server()
    f = resume_parent(t)
    assert not getattr(f.mgr, 'views', []) and not audits(f, 'resume.delegated_view_lost')


# ------------------------------------------------------------------ brief item 6, second test (guard)
def test_a_job_started_after_a_resume_maps_the_delegated_slots_tab_the_same_way(vtree):
    t = vtree
    t.f.setf('worker-1', tmux_view=1)  # approved before the restart: its next generations run in tmux
    t.world.restart_server()
    f = resume_parent(t)
    tab = f.agent('worker-1')['pane']
    session = t.world.client.get(tab)
    assert session == 'worker-1-g1'
    for slot in f.names:  # the resumed generations answer READY
        assert ts.hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready'
    rec, _ = ts.spawn_m0(t)
    assert rec['view'] == 1 and not rec['placeholder'] and rec['pane'] == tab
    assert f.agent('worker-1')['pane'] == tab and tab in t.world.alive
    cf = ts.child_daemon(t, rec)
    assert t.world.client[cf.agent('supervisor')['pane']] == session  # the child shows the same live session
