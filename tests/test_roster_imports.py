"""Roster ``import_claude_md`` / ``import_mcp`` (user 2026-11-11): a fleet whose workers run outside a project's
directory still gets that project's CLAUDE.md rules (copied into every agent's AGENTS.md / CLAUDE.md) and chosen
MCP servers (merged into each Claude agent's mcp.json). Without the keys nothing changes."""
import json

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from fleetmux import fleet, roster

W1 = 'workers:\n  - {name: worker-1}\n'


def project(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    (proj / 'CLAUDE.md').write_text('# Rules\n\nT-28: search the memory service before asking the user. {{NOT_A_PLACEHOLDER}}\n')
    (proj / '.mcp.json').write_text(json.dumps({'mcpServers': {
        'memory': {'command': '${HOME}/bin/memory-mcp', 'env': {'MEMORY_API_URL': 'http://127.0.0.1:9000'}},
        'tavily': {'command': 'tavily', 'env': {'TAVILY_API_KEY': 'secret'}}}}))
    return proj


def write(tmp_path, body):
    path = tmp_path / 'roster.yaml'
    path.write_text(body)
    return path


def test_roster_accepts_imports_relative_to_its_directory(tmp_path):
    project(tmp_path)
    path = write(tmp_path, W1 + 'import_claude_md: [proj/CLAUDE.md]\n'
                                'import_mcp:\n  - {from: proj/.mcp.json, servers: [memory]}\n')
    assert roster.load(path)[0] == ['worker-1']
    paths, servers = roster.imports(path.read_bytes(), path.parent)
    assert paths == [str((tmp_path / 'proj' / 'CLAUDE.md').resolve())]
    assert list(servers) == ['memory']  # only the named server: tavily (and its key) never leaves the file
    assert roster.imports(write(tmp_path, W1).read_bytes(), tmp_path) == ([], {})


def test_run_copy_keeps_relative_imports_pointing_at_the_source(tmp_path):
    """start --roster copies a roster outside the run into the run: relative import paths still name the
    files next to the source roster."""
    project(tmp_path)
    source = write(tmp_path, W1 + 'import_claude_md: [proj/CLAUDE.md]\n'
                                  'import_mcp:\n  - {from: proj/.mcp.json, servers: [memory]}\n')
    run = tmp_path / 'run'
    run.mkdir()
    copy = roster.copy_into_run(source, run)
    assert copy.parent == run.resolve() and roster.load(copy)[0] == ['worker-1']
    paths, servers = roster.imports(copy.read_bytes(), copy.parent)
    assert paths == [str((tmp_path / 'proj' / 'CLAUDE.md').resolve())]
    assert list(servers) == ['memory']


@pytest.mark.parametrize('body,why', [
    ('import_claude_md: proj/CLAUDE.md\n', 'non-empty list of file paths'),
    ('import_claude_md: []\n', 'non-empty list of file paths'),
    ('import_claude_md: [proj/MISSING.md]\n', 'file not found: proj/MISSING.md'),
    ('import_mcp: {from: proj/.mcp.json}\n', 'non-empty list'),
    ('import_mcp:\n  - {from: proj/.mcp.json}\n', r'import_mcp\[0\]: must be'),
    ('import_mcp:\n  - {from: proj/nope.json, servers: [memory]}\n', 'cannot read'),
    ('import_mcp:\n  - {from: proj/.mcp.json, servers: [memory, exa]}\n', 'mcpServers: exa'),
])
def test_roster_rejects_bad_imports(tmp_path, body, why):
    project(tmp_path)
    with pytest.raises(ValueError, match=why):
        roster.load(write(tmp_path, W1 + body))


def test_every_agent_gets_the_imported_rules_copied_not_imported(fx, tmp_path):  # noqa: F811
    f = fx
    project(tmp_path)
    f.cfg['roster'] = str(write(tmp_path, W1 + 'import_claude_md: [proj/CLAUDE.md]\n'))
    for name in ('worker-1', 'supervisor'):
        scope = f.role_scope(name, f.runtime_cfg(name), {})
        assert 'T-28: search the memory service before asking the user.' in scope
        assert '## Imported project rules' in scope and 'the fleet rules win' in scope
        assert '\n@' not in scope  # no @import line: Claude Code's external-import dialog is answered No
        assert scope.rstrip().endswith('{{NOT_A_PLACEHOLDER}}')  # appended after all fleet rules
    worker = f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})
    assert worker.index('Do not write outside your own directory') < worker.index('T-28')
    assert '{{NOT_A_PLACEHOLDER}}' in scope  # the copied text is not run through the scope placeholders
    (tmp_path / 'proj' / 'CLAUDE.md').write_text('T-29: edited\n')  # read again at each spawn
    assert 'T-29: edited' in f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})


def test_without_imports_the_scope_is_unchanged(fx, tmp_path):  # noqa: F811
    f = fx
    f.cfg['roster'] = str(write(tmp_path, W1))
    with_roster = f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})
    f.cfg.pop('roster', None)
    assert with_roster == f.role_scope('worker-1', f.runtime_cfg('worker-1'), {})
    assert 'Imported project rules' not in with_roster


def test_mcp_servers_merge_into_the_kit_copy_and_leave_with_the_key(tmp_path):
    project(tmp_path)
    cwd, kit = tmp_path / 'cwd', tmp_path / 'kit'
    cwd.mkdir()
    kit.mkdir()
    (kit / 'mcp.json').write_text(json.dumps({'mcpServers': {'memory': {'command': 'kit-own'},
                                                             'context7': {'command': 'c7'}}}))
    cfg = {'roster': str(write(tmp_path, W1 + 'import_mcp:\n  - {from: proj/.mcp.json, servers: [memory]}\n')),
           'worker_kit': str(kit)}
    fleet.install_kit(cfg['worker_kit'], cwd)
    fleet.install_imported_mcp(cfg, cwd)
    assert json.loads((cwd / 'mcp.json').read_text())['mcpServers']['memory'] == {'command': 'kit-own'}  # kit wins
    cfg.pop('worker_kit')
    (cwd / 'mcp.json').unlink()
    fleet.install_imported_mcp(cfg, cwd)  # no kit: the fleet writes the file alone
    got = json.loads((cwd / 'mcp.json').read_text())['mcpServers']
    assert list(got) == ['memory'] and got['memory']['command'] == '${HOME}/bin/memory-mcp'
    write(tmp_path, W1)  # key removed from the roster: the next spawn drops what the fleet wrote
    fleet.install_imported_mcp(cfg, cwd)
    assert not (cwd / 'mcp.json').exists() and not (cwd / fleet.IMPORTED_MCP_MARK).exists()


def test_without_the_key_a_foreign_mcp_json_is_left_alone(tmp_path):
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    (cwd / 'mcp.json').write_text('{"mcpServers": {"x": {"command": "x"}}}')
    fleet.install_imported_mcp({'roster': str(write(tmp_path, W1))}, cwd)
    fleet.install_imported_mcp({}, cwd)
    assert json.loads((cwd / 'mcp.json').read_text()) == {'mcpServers': {'x': {'command': 'x'}}}


# ------------------------------------------------------------------ global CLAUDE.md (~/.claude/CLAUDE.md)
def test_global_claude_md_is_copied_only_where_claude_code_would_not_load_it(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / 'CLAUDE.md').write_text('G-1: global rule\n')
    monkeypatch.setenv('HOME', str(home))
    cfg = {'roster': str(write(tmp_path, W1 + 'import_claude_md: [~/.claude/CLAUDE.md]\n'))}
    assert 'G-1: global rule' in fleet.imported_rules(cfg)  # advised slot (--setting-sources project), codex, pi
    assert fleet.imported_rules(cfg, native_user_memory=True) == ''  # plain Claude slot loads it itself


# ------------------------------------------------------------------ start --project-dir
def git_repo(path):
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    return path


def test_project_fleet_dir_is_excluded_once(tmp_path):
    repo = git_repo(tmp_path / 'repo')
    exclude = repo / '.git' / 'info' / 'exclude'
    exclude.write_text('# git ls-files --others --exclude-from=.git/info/exclude\n*.swp')  # no trailing newline
    assert fleet.exclude_project_fleet_dir(repo) == exclude.resolve()
    fleet.exclude_project_fleet_dir(repo)
    assert exclude.read_text().splitlines()[-2:] == ['*.swp', '.fleetmux/']
    assert exclude.read_text().count('.fleetmux') == 1
    sub = repo / 'pkg'
    sub.mkdir()
    fleet.exclude_project_fleet_dir(sub)  # a project inside the repository: the same exclude file
    assert exclude.read_text().count('.fleetmux') == 1
    other = git_repo(tmp_path / 'other')
    (other / '.git' / 'info' / 'exclude').write_text('/.fleetmux/\n')  # an equivalent line already there
    fleet.exclude_project_fleet_dir(other)
    assert (other / '.git' / 'info' / 'exclude').read_text() == '/.fleetmux/\n'
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert fleet.exclude_project_fleet_dir(plain) is None and not (plain / '.git').exists()


def test_project_dir_puts_runs_inside_the_project(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(fleet, 'start', seen.append)
    monkeypatch.setattr(fleet, 'pin_paths', lambda paths: None)
    project = tmp_path / 'proj'
    project.mkdir()
    fleet.main(['start', '--lifecycle', 'smoke', '--project-dir', str(project)])
    assert seen[0].runs_dir == str(project.resolve() / '.fleetmux' / 'runs')
    assert seen[0].project_dir == str(project.resolve())
    with pytest.raises(SystemExit, match='exclusive'):
        fleet.main(['start', '--lifecycle', 'smoke', '--project-dir', str(project), '--runs-dir', str(tmp_path)])
    with pytest.raises(SystemExit, match='not a directory'):
        fleet.main(['start', '--lifecycle', 'smoke', '--project-dir', str(tmp_path / 'missing')])
    fleet.main(['start', '--lifecycle', 'smoke'])  # without the flag: unchanged default
    assert seen[-1].runs_dir == str(fleet.HOME / 'runs') and seen[-1].project_dir is None
