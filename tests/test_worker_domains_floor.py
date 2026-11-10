"""start --worker-domains: domains every Claude worker always gets; the advisor only adds to them."""
import json
import sys

import pytest

from test_advisor import Runner, fake_start, item, send_decision
from test_fleet import FakeMgr, FakeRedis, turn
from test_kit import make_kit
from cmux_fleet import fleet, roster


def floor_fleet(tmp_path, monkeypatch, advisor_on=True):
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', tmp_path / 'claude.json')
    kit = make_kit(tmp_path / 'kit')
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/cmux-fleet-test.sock', 'python': sys.executable,
           'workers': ['worker-1', 'worker-2'], 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'codex_home': str(tmp_path / 'codex-home'), 'worker_cli': 'claude', 'worker_kit': str(kit),
           'worker_domains': ['us_options'],
           'claude': '/bin/claude', 'claude_window': 200000, 'worker_effort': 'medium', 'token': 'abcd1234'}
    (run / 'config.json').write_text(json.dumps(cfg))
    if advisor_on:
        (run / 'roster.yaml').write_text('workers:\n' + ''.join('  - name: ' + w + '\n' for w in cfg['workers']))
        cfg['roster'] = str(run / 'roster.yaml')
        (run / 'config.json').write_text(json.dumps(cfg))
        roster.write_advisor(run / 'roster.yaml', True, cfg['workers'])
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=tmp_path / 'sessions')
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': []})
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: f.r)
    f.start_all()
    for name in f.names:
        f.handle(turn(name, 'READY'))
    f.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'research the sector rotation'}})
    return f


def linked(f, name, kind):
    path = fleet.agent_cwd(f.cfg, name) / '.claude' / kind
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


@pytest.mark.parametrize('advisor_on', [True, False])
def test_default_layout_links_the_floor(tmp_path, monkeypatch, advisor_on):
    f = floor_fleet(tmp_path, monkeypatch, advisor_on)
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert linked(f, 'worker-1', 'agents') == ['finance-research-specialist.md']
    assert linked(f, 'worker-1', 'skills') == ['option-tier-screen']
    text = (cwd / 'CLAUDE.md').read_text()
    assert 'RULES OF us_options' in text and 'Never use yfinance' in text and 'RULES OF research_web' not in text
    assert (cwd / 'mcp.json').is_file()
    # advisor on: the first generation is laid out advised (record = the floor); off: the default layout ('')
    assert f.agent('worker-1')['installed_domains'] == ('["us_options"]' if advisor_on else '')


def test_sheet_domains_are_added_to_the_floor_on_respawn(tmp_path, monkeypatch):
    f = floor_fleet(tmp_path, monkeypatch)
    # the advisor echoes the pinned domain too: dropped before validation, never a reason to reject the sheet
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': ['us_options', 'research_web']})
    send_decision(f, [item()])
    f.pump()
    assert f.advisor_runner.calls[0]['slot']['pinned_domains'] == ['us_options']
    info = f.agent('worker-1')
    audit = f.r.hgetall(fleet.ADVICE + info['advice_ticket'])
    assert audit['status'] == 'ok' and json.loads(audit['output'])['domains'] == ['research_web']
    assert info['generation'] == '1'
    assert json.loads(info['installed_domains']) == ['us_options', 'research_web']
    assert linked(f, 'worker-1', 'agents') == ['finance-research-specialist.md', 'web-research-specialist.md']
    text = (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()
    assert 'RULES OF us_options' in text and 'RULES OF research_web' in text


def test_card_whitelist_never_rejects_an_echoed_pinned_domain(tmp_path, monkeypatch):
    f = floor_fleet(tmp_path, monkeypatch)
    raw = {'model': 'sonnet', 'effort': 'medium', 'domains': ['us_options'], 'reason': 'x'}
    assert f.drop_floor(raw)['domains'] == [] and raw['domains'] == ['us_options']  # caller's dict untouched


def test_fallback_and_inherited_records_install_the_floor(tmp_path, monkeypatch):
    f = floor_fleet(tmp_path, monkeypatch)
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    f.setf('worker-1', advice_domains='[]')  # e.g. the "advisor unavailable: domains []" sheet
    f.install_advised_kit('worker-1', cwd, 'scope')
    assert json.loads(f.agent('worker-1')['installed_domains']) == ['us_options']
    assert linked(f, 'worker-1', 'skills') == ['option-tier-screen']
    f.setf('worker-1', installed_domains='["research_web"]')  # an older record without the floor
    f.install_advised_kit('worker-1', cwd, 'scope')
    assert json.loads(f.agent('worker-1')['installed_domains']) == ['us_options', 'research_web']


def test_without_floor_a_domain_kit_default_layout_links_nothing(tmp_path, monkeypatch):
    f = floor_fleet(tmp_path, monkeypatch, advisor_on=False)
    cwd = fleet.agent_cwd(f.cfg, 'worker-2')
    f.cfg.pop('worker_domains')
    fleet.kitmod.Kit.clear(cwd)
    f.install_default_kit(cwd, 'scope')
    assert linked(f, 'worker-2', 'skills') == [] and (cwd / 'mcp.json').is_file()


def test_parse_worker_domains(tmp_path):
    kit = make_kit(tmp_path / 'kit')
    assert fleet.parse_worker_domains(None, None) == []
    assert fleet.parse_worker_domains(' us_options, research_web,us_options ', str(kit)) == ['us_options', 'research_web']
    with pytest.raises(SystemExit, match='needs --worker-kit'):
        fleet.parse_worker_domains('us_options', None)
    old = tmp_path / 'old-kit'
    (old / '.claude' / 'skills').mkdir(parents=True)
    with pytest.raises(SystemExit, match='needs a domain kit'):
        fleet.parse_worker_domains('us_options', str(old))
    with pytest.raises(SystemExit, match='not in the domain table.*: nope .*the table has: research_web'):
        fleet.parse_worker_domains('us_options,nope', str(kit))
    with pytest.raises(SystemExit, match='empty'):
        fleet.parse_worker_domains(' , ', str(kit))


def test_start_writes_worker_domains_and_refuses_unknown_first(monkeypatch, tmp_path):
    argv = fake_start(monkeypatch, tmp_path)
    kit = make_kit(tmp_path / 'kit')
    fleet.main(argv + ['--advisor', '--worker-kit', str(kit), '--worker-domains', 'us_options,research_web'])
    cfg = fleet.load(fleet.load(tmp_path / 'latest.json')['config'])
    assert cfg['worker_domains'] == ['us_options', 'research_web']
    fleet.main(argv + ['--worker-kit', str(kit)])
    assert 'worker_domains' not in fleet.load(fleet.load(tmp_path / 'latest.json')['config'])
    from cmux_fleet import cmux_manager
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: pytest.fail('refused before provisioning'))
    with pytest.raises(SystemExit, match='not in the domain table'):
        fleet.main(argv + ['--worker-kit', str(kit), '--worker-domains', 'futures'])
