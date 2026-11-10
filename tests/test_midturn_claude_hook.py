from cmux_fleet import fleet, midturn


class FakeRedis:
    def __init__(self, info):
        self.h = {fleet.key('agent', 'worker-1'): dict(info)}

    def hgetall(self, k):
        return dict(self.h.get(k, {}))

    def eval(self, script, nkeys, k):
        d = self.h[k]
        if d.get('midturn_send_pending') == '1':
            d['midturn_send_pending'] = '0'
            d['midturn_type_attempts'] = str(int(d.get('midturn_type_attempts') or 0) + 1)
            return 1
        return 0


def info(gen='2', pending='1'):
    hid = 'fx-0123456789abcdef'
    prompt = midturn.MIDTURN_PROMPT.format(midturn_id=hid, used=500, window=1000, path='/tmp/x/draft.md')
    return {'generation': gen, 'midturn_send_pending': pending, 'midturn_id': hid, 'midturn_typed_g': gen,
            'midturn_prompt': prompt}


def run(monkeypatch, r, generation='2'):
    monkeypatch.setattr(fleet, 'load', lambda path: {})
    monkeypatch.setattr(fleet, 'connect', lambda cfg, socket_timeout=5: r)
    return fleet.claude_posttool('cfg.json', 'worker-1', generation)


def test_sends_the_registered_prompt_once(monkeypatch):
    r = FakeRedis(info())
    first = run(monkeypatch, r)
    assert first and first.startswith('[FLEET MID-TURN HANDOFF fx-0123456789abcdef] ')
    assert run(monkeypatch, r) is None  # taken: the next tool call sends nothing
    assert r.h[fleet.key('agent', 'worker-1')]['midturn_type_attempts'] == '1'


def test_nothing_pending_or_stale_generation_sends_nothing(monkeypatch):
    assert run(monkeypatch, FakeRedis(info(pending='0'))) is None
    assert run(monkeypatch, FakeRedis(info(gen='3'))) is None  # hook of an older generation


def test_redis_down_is_silent(monkeypatch):
    def boom(cfg, socket_timeout=5):
        raise OSError('no socket')
    monkeypatch.setattr(fleet, 'load', lambda path: {})
    monkeypatch.setattr(fleet, 'connect', boom)
    assert fleet.claude_posttool('cfg.json', 'worker-1', '2') is None


def test_claude_pane_is_never_typed(monkeypatch):
    called = []
    f = fleet.Fleet.__new__(fleet.Fleet)
    monkeypatch.setattr(fleet, 'is_claude', lambda cfg, name: True)
    f.runtime_cfg = lambda name: {}
    f.agent = lambda name: info()
    f.mgr = type('M', (), {'send_prompt_to_idle': lambda *a, **k: called.append(a)})()
    fleet.Fleet.midturn_type(f, 'worker-1')
    assert called == []
