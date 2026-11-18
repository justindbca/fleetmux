"""``fleetmux superpowers``: refresh and the command line (docs/distributed-development.md, "Commands").

Every test uses the temporary inventory of tests/conftest.py (FLEETMUX_SUPERPOWERS); no test probes, lists
processes or opens a network connection.
"""
import io
import json
import sys
import urllib.request

import pytest
import yaml

from fleetmux import fleet, fleet_superpowers as fs, fleet_superpowers_cli as cli, fleet_superpowers_refresh as rf
from test_fleet_superpowers_write import doc_example, WORKFLOW

NOW = '2030-01-03T08:00:00+00:00'
DISCOVERED = {
    'probed_at': '2030-01-03T07:00:00+00:00',
    'clis': {
        'claude': {'installed': True, 'version': '2.1.0', 'logged_in': True, 'models': ['opus', 'sonnet'],
                   'path': '/opt/claude'},
        'codex': {'installed': True, 'version': '0.164.0', 'logged_in': False},
        'pi': {'installed': True, 'version': '1.5.0', 'logged_in': True,
               'models': ['gw-up/deepseek-flash', 'gw-down/qwen']},
        'ollama': {'installed': False},
    },
    'gateways': {'gw-up': {'online': True}, 'gw-down': {'online': False}},
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError('a test opened a network connection')
    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', refuse)


@pytest.fixture
def inv():
    path = fs.file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc_example())
    return path


class Probe:
    """A stub of ``reach_api``: records (url, proxy), answers with an HTTP class or None (no answer)."""

    def __init__(self, answer='4xx'):
        self.calls, self.answer = [], answer

    def __call__(self, url, proxy=None):
        self.calls.append((url, proxy))
        return self.answer


def run_cli(argv, stdin=''):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin), out, err
    try:
        with pytest.raises(SystemExit) as stop:
            fleet.main(['superpowers'] + argv)
    finally:
        sys.stdin, sys.stdout, sys.stderr = old
    return stop.value.code, out.getvalue(), err.getvalue()


# -- refresh ---------------------------------------------------------------------------------------------

def refresh(probe, **kw):
    facts = dict(discovered=DISCOVERED, found={'instances': []}, now=NOW, hostname='field.example',
                 addresses=[{'kind': 'lan', 'address': '192.0.2.23'}], bin_=fs.DEFAULT_BIN, release='0123abc')
    facts.update(kw)
    return rf.refresh(probe=probe, **facts)


def test_refresh_writes_the_self_entry_and_nothing_else(inv):
    before = inv.read_text()
    probe = Probe('4xx')
    name, entry = refresh(probe)
    assert name == 'field'
    after = inv.read_text()
    a, b = before.index('  field:\n'), before.index('  home:\n')
    a2, b2 = after.index('  field:\n'), after.index('  home:\n')
    assert after[:a2] == before[:a] and after[b2:] == before[b:]  # other entries and comments, byte for byte
    stored = yaml.safe_load(after)['machines']['field']
    assert stored == entry
    assert stored['source'] == 'self' and stored['updated_at'] == NOW and stored['hostname'] == 'field.example'
    assert stored['reach'] == []  # kept from the hand-written value
    assert stored['fleetmux'] == {'bin': fs.DEFAULT_BIN, 'release': '0123abc'}
    assert stored['clis'] == {'claude': {'version': '2.1.0', 'logged_in': True},
                              'codex': {'version': '0.164.0', 'logged_in': False},
                              'pi': {'version': '1.5.0', 'logged_in': True}}
    assert probe.calls == [('https://api.anthropic.com', None)]  # one request per CLI with models, never per model
    assert [(m['id'], m['reachable']) for m in stored['models']] == [
        ('opus', True), ('sonnet', True), ('gw-up/deepseek-flash', True), ('gw-down/qwen', False)]
    assert stored['can_start_claude'] is True
    assert fs.validate(fs.read(inv), require_self=True) == []


def test_refresh_keeps_a_hand_written_reach(inv):
    text = inv.read_text().replace('    reach: []                       # no inbound ssh on a public network\n',
                                   '    reach:\n      - {via: overlay, ssh: user@field.example}\n')
    inv.write_text(text)
    refresh(Probe())
    assert fs.machine('field')['reach'] == [{'via': 'overlay', 'ssh': 'user@field.example'}]


def test_refresh_keeps_a_reach_entry_added_while_it_probes(inv):
    class Adding(Probe):
        def __call__(self, url, proxy=None):
            fs.add_copy('field', 'user@field.example', via='overlay')  # `add field` while the probe runs
            return super().__call__(url, proxy)
    refresh(Adding())
    assert fs.machine('field')['reach'] == [{'via': 'overlay', 'ssh': 'user@field.example'}]


def test_claude_unreachable_means_cannot_start_claude(inv):
    _, entry = refresh(Probe(None))
    assert entry['can_start_claude'] is False
    assert all(not m['reachable'] for m in entry['models'] if m['cli'] == 'claude')


def test_first_refresh_creates_the_self_entry(tmp_path):
    path = tmp_path / 'f.yaml'
    fs.add_copy('home', 'user@home.example', now=NOW, path=path)
    before = path.read_text()
    name, _ = refresh(Probe(), name='field', path=path, hostname='Node7.example')
    assert name == 'field' and fs.self_name(path) == 'field'
    assert path.read_text().startswith(before)
    name, _ = refresh(Probe(), path=path, hostname='Node7.example')  # later refreshes keep the stored name
    assert name == 'field' and list(fs.machines(path)) == ['home', 'field']


def test_first_refresh_without_a_name_is_refused_with_the_command(tmp_path):
    path = tmp_path / 'f.yaml'
    with pytest.raises(fs.InventoryError) as refused:
        refresh(Probe(), path=path, hostname='Node7.example')
    assert 'fleetmux superpowers refresh --name node7' in str(refused.value)
    assert not path.exists()


def test_cli_first_refresh_without_a_name_exits_1(monkeypatch):
    monkeypatch.setattr(rf, 'short_name', lambda hostname=None: 'node7')
    code, _, err = run_cli(['refresh'])
    assert code == 1 and err.startswith('superpowers: ') and '--name node7' in err


class Answer:
    status = 200

    def close(self):
        pass


@pytest.mark.parametrize('outcome, seen', [(401, '4xx'), (429, '4xx'), (503, '5xx'), (None, '2xx'),
                                           (urllib.error.URLError('no route'), None), (OSError('refused'), None)])
def test_reach_api_returns_the_http_class_seen(monkeypatch, outcome, seen):
    def answer(self, req, timeout=None):
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(req.full_url, outcome, 'status', {}, None)
        if isinstance(outcome, Exception):
            raise outcome
        return Answer()
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', answer)
    assert rf.reach_api('https://api.example') == seen


def test_reach_api_goes_through_the_given_proxy_and_never_the_environment(monkeypatch):
    built = []
    real = urllib.request.build_opener

    def build(*handlers):
        built.append([h.proxies for h in handlers if isinstance(h, urllib.request.ProxyHandler)])
        return real(*handlers)
    monkeypatch.setattr(urllib.request, 'build_opener', build)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', lambda self, req, timeout=None: Answer())
    monkeypatch.setenv('HTTPS_PROXY', 'http://192.0.2.99:1')
    assert rf.reach_api('https://api.example', proxy='http://127.0.0.1:7890') == '2xx'
    assert rf.reach_api('https://api.example') == '2xx'
    assert built == [[{'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}], [{}]]


def routed_fleet(tmp_path, running=True, parent=None):
    """A transport inventory with one fleet whose run config routes claude through an applet proxy."""
    cfg = tmp_path / 'run-routed' / 'config.json'
    cfg.parent.mkdir()
    data = {'run': str(cfg.parent), 'fleet_home': str(tmp_path / 'fh'), 'mode': 'service',
            'session_env': [{'applet': 'applet-proxy-1', 'port': 7890, 'clis': ['claude'],
                             'vars': {'HTTPS_PROXY': 'http://127.0.0.1:7890'}}]}
    if parent:
        data['parent_config'] = parent
    cfg.write_text(json.dumps(data))
    return {'instances': [{'run': str(cfg.parent), 'configs': [str(cfg)], 'mode': 'service',
                           'observed_running': running}]}


def test_a_routed_cli_is_probed_through_the_route_proxy(inv, tmp_path):
    probe = Probe('4xx')
    _, entry = refresh(probe, found=routed_fleet(tmp_path))
    assert probe.calls == [('https://api.anthropic.com', 'http://127.0.0.1:7890')]
    claude = [m for m in entry['models'] if m['cli'] == 'claude']
    assert {(m['via'], m['answered'], m['reachable']) for m in claude} == {('applet-proxy-1', '4xx', True)}
    assert entry['can_start_claude'] is True


def test_the_route_decides_can_start_claude(inv, tmp_path):
    _, entry = refresh(Probe(None), found=routed_fleet(tmp_path))
    claude = [m for m in entry['models'] if m['cli'] == 'claude']
    assert {(m['via'], m['answered'], m['reachable']) for m in claude} == {('applet-proxy-1', 'none', False)}
    assert entry['can_start_claude'] is False


@pytest.mark.parametrize('kw', [{'running': False}, {'parent': '/elsewhere/config.json'}])
def test_only_running_root_fleets_route_the_probe(inv, tmp_path, kw):
    probe = Probe('2xx')
    _, entry = refresh(probe, found=routed_fleet(tmp_path, **kw))
    assert probe.calls == [('https://api.anthropic.com', None)]
    assert {m['via'] for m in entry['models'] if m['cli'] == 'claude'} == {'direct'}


@pytest.mark.parametrize('auth, url', [('chatgpt', 'https://chatgpt.com/backend-api/codex'),
                                       (None, 'https://api.openai.com'), ('other', 'https://api.openai.com')])
def test_codex_is_probed_at_the_host_of_its_login(inv, auth, url):
    codex = {'installed': True, 'version': '0.164.0', 'logged_in': True, 'models': ['astra']}
    if auth:
        codex['auth_method'] = auth
    discovered = dict(DISCOVERED, clis=dict(DISCOVERED['clis'], codex=codex))
    probe = Probe('4xx')
    _, entry = refresh(probe, discovered=discovered)
    assert probe.calls == [('https://api.anthropic.com', None), (url, None)]
    rows = {m['cli']: m for m in entry['models']}
    assert rows['codex']['probe_host'] == url and rows['claude']['probe_host'] == 'https://api.anthropic.com'
    assert 'probe_host' not in rows['pi']


def test_model_rows_say_where_the_model_came_from(inv):
    probe = Probe('2xx')
    _, entry = refresh(probe)
    assert [(m['id'], m['source']) for m in entry['models']] == [
        ('opus', 'card'), ('sonnet', 'card'), ('gw-up/deepseek-flash', 'listed'), ('gw-down/qwen', 'listed')]
    assert probe.calls == [('https://api.anthropic.com', None)]  # one host request, no per-model probe


def test_a_route_without_https_proxy_is_probed_direct(inv, tmp_path):
    found = routed_fleet(tmp_path)
    cfg = json.loads(open(found['instances'][0]['configs'][0]).read())
    cfg['session_env'][0]['vars'] = {'ALL_PROXY': 'socks5://127.0.0.1:7891'}  # urllib cannot use it
    open(found['instances'][0]['configs'][0], 'w').write(json.dumps(cfg))
    probe = Probe('2xx')
    _, entry = refresh(probe, found=found)
    assert probe.calls == [('https://api.anthropic.com', None)]
    assert {m['via'] for m in entry['models'] if m['cli'] == 'claude'} == {'direct'}


def test_a_direct_probe_records_via_direct_and_the_class(inv):
    _, entry = refresh(Probe('5xx'))
    claude = [m for m in entry['models'] if m['cli'] == 'claude']
    assert {(m['via'], m['answered'], m['reachable']) for m in claude} == {('direct', '5xx', True)}
    assert all('via' not in m for m in entry['models'] if m['cli'] == 'pi')  # not probed


def test_fleets_come_from_the_transport_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'user'))
    root = tmp_path / 'user' / '.fleetmux'
    rows = []
    for home, run, running in ((root, 'r7', True), (root / 'fleets' / 'dev', 'r2', False),
                               (root / 'fleets' / 'dev', 'r1', False)):
        cfg = home / 'runs' / run / 'config.json'
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({'run': str(cfg.parent), 'fleet_home': str(home), 'mode': 'service',
                                   'python': str(tmp_path / 'fleetmux-releases' / '0123abc' / '.venv/bin/python')}))
        rows.append({'run': str(cfg.parent), 'configs': [str(cfg)], 'mode': 'service', 'observed_running': running})
    (root / 'fleets' / 'dev' / 'latest.json').write_text(json.dumps({'config': rows[1]['configs'][0]}))
    sub = root / 'runs' / 'r7' / 'sub' / 'M0' / 'config.json'  # a sub-fleet inherits the root's fleet home
    sub.parent.mkdir(parents=True)
    sub.write_text(json.dumps({'run': str(sub.parent), 'fleet_home': str(root), 'mode': 'task', 'task_id': 'M0',
                               'parent_config': rows[0]['configs'][0], 'root_config': rows[0]['configs'][0]}))
    rows.append({'run': str(sub.parent), 'configs': [str(sub)], 'mode': 'task', 'observed_running': True})
    found = rf.fleets_of({'instances': rows})
    assert found == [
        {'name': 'main', 'home': str(root), 'config': rows[0]['configs'][0], 'mode': 'service', 'running': True,
         'release': '0123abc', 'root': True},
        {'name': 'dev', 'home': str(root / 'fleets' / 'dev'), 'config': rows[1]['configs'][0], 'mode': 'service',
         'running': False, 'release': '0123abc', 'root': True},  # r1: neither running nor the latest run
        {'name': 'main', 'home': str(root), 'config': str(sub), 'mode': 'task', 'running': True, 'release': None,
         'root': False, 'parent': 'main', 'sub': 'M0'}]
    assert fs.validate_machine('x', {'source': 'self', 'fleets': found}) == []


# -- the command line -----------------------------------------------------------------------------------

def test_cli_refresh(inv, monkeypatch):
    monkeypatch.setattr(rf, 'refresh', lambda name=None: ('field', {'source': 'self'}))
    code, out, _ = run_cli(['refresh', '--json'])
    assert code == 0 and json.loads(out) == {'field': {'source': 'self'}}


def test_cli_add_and_show_machines(tmp_path):
    code, out, _ = run_cli(['add', 'home', '--ssh', 'user@home.example', '--via', 'overlay'])
    assert code == 0 and 'user@home.example' in out
    code, out, _ = run_cli(['show', '--machines'])
    assert code == 0 and out.startswith('home\tcopy\t-\tuser@home.example')
    code, _, err = run_cli(['add', 'home', '--ssh', 'user@home.example', '--via', 'overlay'])
    assert code == 1 and err.startswith('superpowers: ') and 'already' in err
    code, _, err = run_cli(['add', 'home', '--ssh', 'home-lan'])
    assert code == 0, err
    code, out, _ = run_cli(['show', '--machines'])
    assert out.startswith('home\tcopy\t-\tuser@home.example,home-lan')
    code, out, err = run_cli(['add', 'home', '--remove-reach', 'user@home.example'])
    assert code == 0, err
    assert fs.machine('home')['reach'] == [{'via': 'lan', 'ssh': 'home-lan'}]
    code, _, err = run_cli(['add', 'home', '--remove-reach', 'user@home.example'])
    assert code == 1 and 'no reach entry' in err
    code, _, err = run_cli(['add', 'home', '--ssh', 'a', '--remove-reach', 'b'])
    assert code == 2


def test_cli_add_checks_the_kind_and_accepts_this_machine(inv):
    code, _, err = run_cli(['add', 'home', '--ssh', 'home-x', '--via', 'pigeon'])
    assert code == 1 and 'thunderbolt, lan, overlay, tunnel, tunnel-overseas' in err
    code, _, err = run_cli(['add', 'field', '--ssh', 'user@field.example', '--via', 'thunderbolt'])
    assert code == 0, err
    assert fs.machine('field')['reach'] == [{'via': 'thunderbolt', 'ssh': 'user@field.example'}]
    assert fs.machine('field')['source'] == 'self'
    code, _, err = run_cli(['add', 'home', '--remove-reach', 'home-via-relay', '--via', 'tunnel'])
    assert code == 0, err


def test_cli_show_variants(inv):
    code, out, _ = run_cli(['show'])
    assert code == 0 and out == inv.read_text()
    code, out, _ = run_cli(['show', '--self', '--json'])
    assert list(json.loads(out)) == ['field'] and json.loads(out)['field']['source'] == 'self'
    code, out, _ = run_cli(['show', '--machine', 'home', '--json'])
    assert json.loads(out)['home']['hostname'] == 'home.example'
    code, out, _ = run_cli(['show', '--workflows'])
    assert out.startswith('claude-via-home\tRoute the field fleet')
    code, out, _ = run_cli(['show', '--workflow', 'claude-via-home'])
    assert yaml.safe_load(out)['claude-via-home']['kind'] == 'workflow'
    code, _, err = run_cli(['show', '--machine', 'nowhere'])
    assert code == 1 and 'nowhere' in err
    code, out, _ = run_cli(['show', '--json'])
    assert set(json.loads(out)) >= set(fs.BLOCKS)


def test_cli_import_round_trip_and_self_refusal(inv):
    _, section, _ = run_cli(['show', '--self', '--json'])
    before = inv.read_text()
    code, _, err = run_cli(['import', '-'], stdin=section)  # our own section: refused
    assert code == 1 and 'this machine' in err
    assert inv.read_text() == before
    home = json.dumps({'home': {'source': 'self', 'hostname': 'home.example',
                                'reach': [{'via': 'lan', 'ssh': 'user@home.example'}]}})
    code, out, _ = run_cli(['import', '-'], stdin=home)
    assert code == 0 and out.strip() == 'imported home'
    stored = fs.machine('home')
    assert stored['source'] == 'copy' and stored['copied_at'] and 'clis' not in stored
    code, _, err = run_cli(['import', '-'], stdin='{"a": {}, "b": {}}')
    assert code == 1 and 'exactly one' in err


def test_cli_save_and_check(inv, tmp_path):
    f = tmp_path / 'r.yaml'
    f.write_text(yaml.safe_dump({'route-b': WORKFLOW}))
    code, out, _ = run_cli(['save', str(f)])
    assert code == 0 and out.strip() == 'saved route-b' and fs.workflow('route-b') == WORKFLOW
    code, out, _ = run_cli(['check'])
    assert code == 0 and out.startswith('ok ')
    f.write_text(yaml.safe_dump({'bad': dict(WORKFLOW, saved_on='home')}))
    code, _, err = run_cli(['save', str(f)])
    assert code == 1 and 'saved_on' in err


def test_check_reports_a_missing_self(tmp_path):
    run_cli(['add', 'home', '--ssh', 'user@home.example'])
    code, out, _ = run_cli(['check'])
    assert code == 1 and 'source: self' in out and 'refresh --name <name>' in out
    code, _, err = run_cli(['show', '--self'])
    assert code == 1 and 'refresh --name <name>' in err
    code, _, err = run_cli(['load', 'anything'])
    assert code == 1 and 'refresh --name <name>' in err


def test_an_extension_module_registers_and_replaces_commands(monkeypatch, inv, tmp_path):
    """A module named in EXTENSIONS is imported before the parser is built; it adds sync and replaces save."""
    (tmp_path / 'ext_peer.py').write_text(
        'from fleetmux import fleet_superpowers_cli as cli\n'
        'CALLS = []\n'
        'def _name(p):\n'
        '    p.add_argument("name")\n'
        'def _save(p):\n'
        '    p.add_argument("file")\n'
        '    p.add_argument("--also")\n'
        'def sync(args):\n'
        '    CALLS.append(("sync", args.name))\n'
        '    return 0\n'
        'def save(args):\n'
        '    CALLS.append(("save", cli.save_file(args.file), args.also))\n'
        '    return 0\n'
        'cli.register("sync", "sync one machine", _name, sync)\n'
        'cli.register("save", "save here and on --also", _save, save)\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, 'ext_peer', raising=False)
    monkeypatch.setattr(cli, 'EXTENSIONS', ('ext_peer', 'fleetmux.no_such_extension'))
    monkeypatch.setattr(cli, 'COMMANDS', dict(cli.COMMANDS))
    f = tmp_path / 'r.yaml'
    f.write_text(yaml.safe_dump({'route-b': WORKFLOW}))
    assert run_cli(['sync', 'home'])[0] == 0
    assert run_cli(['save', str(f), '--also', 'home'])[0] == 0
    assert sys.modules['ext_peer'].CALLS == [('sync', 'home'), ('save', 'route-b', 'home')]
    assert fs.workflow('route-b') == WORKFLOW


def test_usage_error_exits_2():
    code, _, _ = run_cli(['nonsense'])
    assert code == 2


def test_show_has_one_name_for_workflows():
    import argparse
    sub = next(a for a in cli.parser()._actions if isinstance(a, argparse._SubParsersAction)).choices['show']
    assert sorted(o for a in sub._actions for o in a.option_strings) == [
        '--help', '--json', '--machine', '--machines', '--self', '--workflow', '--workflows', '-h']


# -- refresh from the root fleet daemon ------------------------------------------------------------------

def epoch(stamp):
    import datetime
    return datetime.datetime.fromisoformat(stamp).timestamp()


def test_a_fresh_probe_is_reused_and_a_stale_one_is_not(inv):
    probe = Probe('4xx')
    refresh(probe)
    assert len(probe.calls) == 1
    later = '2030-01-03T08:05:00+00:00'  # 300 s after NOW
    _, entry = refresh(probe, now=later, max_probe_age=600)
    assert len(probe.calls) == 1  # reused: no request
    assert {m['checked_at'] for m in entry['models'] if m['cli'] == 'claude'} == {NOW}
    assert entry['updated_at'] == later  # the local facts are new
    _, entry = refresh(probe, now='2030-01-03T08:20:00+00:00', max_probe_age=600)
    assert len(probe.calls) == 2


class Daemon:
    """daemon_tick with a recording refresh and a spawn that runs the work at once."""

    def __init__(self):
        self.state, self.calls = {}, []

    def run(self, **kw):
        self.calls.append(kw)

    def tick(self, now):
        return rf.daemon_tick(self.state, now=now, run=self.run, spawn=lambda work: work())


def test_daemon_refreshes_at_start_then_when_the_entry_is_older_than_the_interval(inv):
    d = Daemon()
    stored = epoch(fs.machine('field')['updated_at'])
    assert d.tick(stored + 10) == 'waiting'  # the start refresh waits START_DELAY for the applets to come up
    assert d.tick(stored + 10 + rf.START_DELAY) == 'started'  # even with a fresh entry: a new fleet must show up
    assert d.calls == [{'max_probe_age': 600}]
    assert d.tick(stored + 41) == 'waiting'
    assert d.tick(stored + 640) == 'started'  # the entry is now older than hot.refresh_seconds
    assert len(d.calls) == 2


def test_daemon_skips_when_another_daemon_refreshed_recently(inv):
    d = Daemon()
    stored = epoch(fs.machine('field')['updated_at'])
    d.tick(stored)
    assert d.tick(stored + 30) == 'started'
    entry = fs.machine('field')
    fs.write_self(dict(entry, updated_at='2030-01-01T10:09:00+00:00'))  # another root fleet refreshed at +540 s
    assert d.tick(stored + 630) == 'fresh' and len(d.calls) == 1
    assert d.tick(stored + 1000) == 'waiting'
    assert d.tick(stored + 1140) == 'started'


def test_only_one_daemon_per_machine_refreshes_at_a_time(inv):
    import fcntl
    import os
    fd = os.open(str(fs.lock_path()) + '.refresh', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another daemon is refreshing
    d = Daemon()
    try:
        d.tick(1e9)
        assert d.tick(1e9 + 30) == 'busy' and d.calls == []
    finally:
        os.close(fd)
    assert d.tick(1e9 + 30 + rf.BUSY_RETRY) == 'started'  # the start refresh is retried soon, not lost


def test_daemon_refresh_is_off_without_a_self_entry_or_at_zero(inv, tmp_path, monkeypatch):
    inv.write_text(inv.read_text() + 'hot:\n  refresh_seconds: 0\n')
    d = Daemon()
    d.tick(1e9)
    assert d.tick(1e9 + 30) == 'off' and d.calls == []
    monkeypatch.setenv(fs.ENV, str(tmp_path / 'empty.yaml'))
    d = Daemon()
    d.tick(1e9)
    assert d.tick(1e9 + 30) == 'no-self' and d.calls == []


def test_a_broken_inventory_is_reported_once_and_backed_off(inv):
    inv.write_text('machines: [\n')
    d = Daemon()
    d.tick(1e9)
    first = d.tick(1e9 + 30)
    assert first.startswith('failed: ') and 'not valid YAML' in first
    assert d.tick(1e9 + 31) == 'waiting'
    assert d.tick(1e9 + 30 + rf.NOT_SET_UP_RETRY) == 'waiting'  # the same problem again: not reported twice


def test_a_none_answer_is_never_reused(inv):
    probe = Probe(None)
    refresh(probe)
    probe.answer = '4xx'
    _, entry = refresh(probe, now='2030-01-03T08:01:00+00:00', max_probe_age=600)
    assert len(probe.calls) == 2 and {m['answered'] for m in entry['models'] if m['cli'] == 'claude'} == {'4xx'}


def test_refresh_seconds_setting(tmp_path):
    path = tmp_path / 'f.yaml'
    assert fs.refresh_seconds(path) == 600
    path.write_text('hot:\n  refresh_seconds: 120\n')
    assert fs.refresh_seconds(path) == 120
    path.write_text('hot:\n  refresh_seconds: -1\n')
    assert fs.validate(fs.read(path)) == ['hot.refresh_seconds must be a number, 0 or above']


def test_only_a_root_fleet_daemon_refreshes(monkeypatch):
    calls = []
    monkeypatch.setattr(rf, 'daemon_tick', lambda state, **kw: calls.append(state) or 'waiting')
    f = fleet.Fleet.__new__(fleet.Fleet)
    f.cfg, f.r, f.superpowers_refresh_state = {}, object(), {}
    f.superpowers_refresh()
    f.cfg = {'parent_config': '/run/config.json'}
    f.superpowers_refresh()
    assert len(calls) == 1
