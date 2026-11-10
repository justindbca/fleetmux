"""Cross-session board + collision notices (cmux_fleet/session_board.py, docs/contracts.md §7).

Every test uses its own board directory (FLEETMUX_BOARD_DIR) and throwaway `git init` repos;
the real ~/.fleetmux/board is never touched."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from cmux_fleet import session_board as sb

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'cmux_fleet/session_board.py'
GIT = ['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-c', 'init.defaultBranch=main']


def git(*args, cwd):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def make_repo(path):
    path.mkdir(parents=True)
    git('init', '-q', cwd=path)
    (path / 'a.py').write_text('a\n')
    (path / 'b.py').write_text('b\n')
    git('add', '.', cwd=path)
    git('commit', '-qm', 'init', cwd=path)
    return path.resolve()


@pytest.fixture
def board(tmp_path, monkeypatch):
    d = tmp_path / 'board'
    monkeypatch.setenv('FLEETMUX_BOARD_DIR', str(d))
    monkeypatch.delenv('FLEETMUX_BOARD_TTL_S', raising=False)
    monkeypatch.delenv('FLEETMUX_PAGE', raising=False)
    return d


def touch(agent, sid, path, cwd, **kw):
    return sb.handle(agent, {'event': 'tool', 'session_id': sid, 'cwd': str(cwd), 'path': str(path)}, **kw)


def check(agent, sid, cwd, **kw):
    return sb.handle(agent, {'event': 'check', 'session_id': sid, 'cwd': str(cwd)}, **kw)


# ------------------------------------------------------------------ rules
def test_same_file_each_side_told_once_with_its_role(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    sb.handle('claude', {'event': 'start', 'session_id': 'aaaa1111', 'cwd': str(repo)})
    sb.handle('claude', {'event': 'prompt', 'session_id': 'aaaa1111', 'cwd': str(repo), 'prompt': '给侧栏加滚动条'})
    assert touch('claude', 'aaaa1111', repo / 'a.py', repo) == []          # alone on the board
    got = touch('pi', 'bbbb2222', repo / 'a.py', repo)
    assert len(got) == 1
    text = got[0]                                                           # bbbb2222 came second: it waits
    assert 'aaaa1111' in text and 'proj' in text and 'a.py' in text
    assert '先开始动' in text and '按先到先得由它继续' in text and '你先别再改这些文件' in text
    assert '监工协调' in text and '分钟前' in text and '给侧栏加滚动条' in text
    assert '同工作树' in text                                               # background only
    assert '只提醒不拦' not in text and '两边商量' not in text
    back = check('claude', 'aaaa1111', repo)
    assert len(back) == 1 and 'bbbb2222' in back[0] and 'a.py' in back[0]
    assert '你先开始，由你继续' in back[0] and '对方会等你完成' in back[0] and '监工' in back[0]
    assert '先开始动' not in back[0] and '只提醒不拦' not in back[0]
    assert check('claude', 'aaaa1111', repo) == []                          # once per side
    assert touch('pi', 'bbbb2222', repo / 'a.py', repo) == []


def test_same_worktree_different_files_do_not_collide(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    touch('claude', 's1', repo / 'a.py', repo)
    assert touch('pi', 's2', repo / 'b.py', repo) == []                     # same worktree, same branch alone: silent
    assert check('claude', 's1', repo) == []


def test_two_worktrees_same_relative_path_collide(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    wt = tmp_path / 'proj-wt'
    git('worktree', 'add', '-q', '-b', 'feat', str(wt), cwd=repo)
    wt = wt.resolve()
    touch('claude', 's1', repo / 'a.py', repo)
    got = touch('claude', 's2', wt / 'a.py', wt)
    assert len(got) == 1 and 'a.py' in got[0] and '先开始动' in got[0]
    assert '同工作树' not in got[0] and '同分支' not in got[0]
    assert 'proj-wt' not in got[0] and 'feat' not in got[0]                 # the other side's worktree is shown
    assert '~' in got[0] or str(repo) in got[0]


def test_two_worktrees_different_files_do_not_collide(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    wt = tmp_path / 'proj-wt'
    git('worktree', 'add', '-q', '-b', 'feat', str(wt), cwd=repo)
    touch('claude', 's1', repo / 'a.py', repo)
    assert touch('claude', 's2', wt.resolve() / 'b.py', wt) == []


def test_different_repos_do_not_collide(board, tmp_path):
    one, two = make_repo(tmp_path / 'one'), make_repo(tmp_path / 'two')
    touch('claude', 's1', one / 'a.py', one)
    assert touch('pi', 's2', two / 'a.py', two) == []


def test_repo_is_the_edited_files_not_the_sessions_cwd(board, tmp_path):
    repo, elsewhere = make_repo(tmp_path / 'proj'), make_repo(tmp_path / 'other')
    touch('claude', 's1', repo / 'a.py', repo)
    got = touch('claude', 's2', repo / 'a.py', elsewhere)  # session sits in `other`, edits `proj`
    assert len(got) == 1 and 'proj' in got[0]
    # and a session merely sitting in proj's worktree without touching it is not on proj
    assert check('pi', 's3', repo) == []


def test_ended_session_is_not_reported(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    touch('claude', 's1', repo / 'a.py', repo)
    sb.handle('claude', {'event': 'end', 'session_id': 's1', 'cwd': str(repo)})
    assert touch('pi', 's2', repo / 'a.py', repo) == []


def test_session_past_ttl_is_not_reported(board, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / 'proj')
    t0 = time.time()
    touch('claude', 's1', repo / 'a.py', repo, now=t0)
    assert touch('pi', 's2', repo / 'a.py', repo, now=t0 + 7201) == []      # default TTL 2 h
    other = make_repo(tmp_path / 'other')   # s2 above is still online at t0 + 61: use a repo it never touched
    monkeypatch.setenv('FLEETMUX_BOARD_TTL_S', '60')
    touch('claude', 's3', other / 'b.py', other, now=t0)
    assert touch('pi', 's4', other / 'b.py', other, now=t0 + 61) == []
    assert any('#s3' in n for n in touch('pi', 's5', other / 'b.py', other, now=t0 + 59))   # still inside 60 s


def test_dead_pid_is_offline(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    gone = subprocess.Popen(['true'])
    gone.wait()
    sb.handle('pi', {'event': 'start', 'session_id': 's1', 'cwd': str(repo), 'pid': gone.pid})
    touch('pi', 's1', repo / 'a.py', repo)
    assert touch('claude', 's2', repo / 'a.py', repo) == []


def test_new_common_file_notifies_again(board, tmp_path):
    one = make_repo(tmp_path / 'proj')
    wt = tmp_path / 'proj-wt'
    git('worktree', 'add', '-q', '-b', 'feat', str(wt), cwd=one)
    wt = wt.resolve()
    touch('claude', 's1', one / 'a.py', one)
    assert len(touch('claude', 's2', wt / 'a.py', wt)) == 1
    assert touch('claude', 's2', wt / 'a.py', wt) == []
    touch('claude', 's1', one / 'b.py', one)
    again = touch('claude', 's2', wt / 'b.py', wt)
    assert len(again) == 1 and 'b.py' in again[0]


def test_merge_into_same_target_within_window(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    git('branch', 'x', cwd=repo)
    git('branch', 'y', cwd=repo)
    sb.handle('claude', {'event': 'tool', 'session_id': 's1', 'cwd': str(tmp_path), 'command': f'git -C {repo} merge x'})
    got = sb.handle('pi', {'event': 'tool', 'session_id': 's2', 'cwd': str(repo), 'command': 'git merge y'})
    assert len(got) == 1 and '合并' in got[0] and 'main' in got[0]
    assert '由它继续' in got[0]                                              # s1 merged first


def test_commit_alone_on_the_same_worktree_does_not_collide(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    sb.handle('claude', {'event': 'tool', 'session_id': 's1', 'cwd': str(tmp_path),
                         'command': f'cd {repo} && git add a.py && git commit -m "x y"'})
    assert touch('pi', 's2', repo / 'b.py', repo) == []


# ------------------------------------------------------------------ who came first
def test_later_notice_counts_minutes_from_when_the_first_started(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    t0 = time.time()
    touch('claude', 'aaaa1111', repo / 'a.py', repo, now=t0)
    got = touch('pi', 'bbbb2222', repo / 'a.py', repo, now=t0 + 600)
    assert len(got) == 1 and '（10 分钟前）' in got[0] and '先开始动' in got[0]


def test_first_session_stays_first_after_editing_the_file_again(board, tmp_path):
    """Re-editing a file must not move a session's start: precedence is per repository, fixed at the first edit."""
    repo = make_repo(tmp_path / 'proj')
    t0 = time.time()
    touch('claude', 'A', repo / 'a.py', repo, now=t0)
    later = touch('claude', 'B', repo / 'a.py', repo, now=t0 + 60)
    assert len(later) == 1 and '先开始动' in later[0]
    first = touch('claude', 'A', repo / 'a.py', repo, now=t0 + 120)          # A edits a.py again
    assert len(first) == 1 and '你先开始，由你继续' in first[0]
    touch('claude', 'B', repo / 'b.py', repo, now=t0 + 180)                  # B is first on b.py, not on the repo
    again = touch('claude', 'A', repo / 'b.py', repo, now=t0 + 240)
    assert len(again) == 1 and 'b.py' in again[0] and '你先开始，由你继续' in again[0]


def test_same_start_time_breaks_ties_by_session_id(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    t0 = time.time()
    touch('claude', 'zz', repo / 'a.py', repo, now=t0)
    got = touch('claude', 'aa', repo / 'a.py', repo, now=t0)
    assert len(got) == 1 and '你先开始，由你继续' in got[0]


def test_a_session_back_after_a_quiet_ttl_starts_over(board, tmp_path, monkeypatch):
    monkeypatch.setenv('FLEETMUX_BOARD_TTL_S', '100')
    repo, other = make_repo(tmp_path / 'proj'), make_repo(tmp_path / 'other')
    t0 = time.time()
    touch('claude', 'A', repo / 'a.py', repo, now=t0)
    touch('claude', 'A', other / 'a.py', other, now=t0 + 90)                 # A stays online elsewhere
    assert touch('claude', 'B', repo / 'a.py', repo, now=t0 + 105) == []     # A's proj edit is past the TTL
    got = touch('claude', 'A', repo / 'a.py', repo, now=t0 + 110)            # A comes back: B was there first
    assert len(got) == 1 and '先开始动' in got[0] and '#B' in got[0]


def test_merge_only_collision_orders_by_the_merge(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    git('branch', 'x', cwd=repo)
    git('branch', 'y', cwd=repo)
    t0 = time.time()
    sb.handle('claude', {'event': 'tool', 'session_id': 's1', 'cwd': str(repo), 'command': 'git merge x'}, now=t0)
    got = sb.handle('pi', {'event': 'tool', 'session_id': 's2', 'cwd': str(repo), 'command': 'git merge y'},
                    now=t0 + 60)
    assert len(got) == 1 and '#s1' in got[0] and '由它继续' in got[0] and 'main' in got[0] and '合并' in got[0]


def test_page_shown_when_set(board, tmp_path, monkeypatch):
    repo = make_repo(tmp_path / 'proj')
    monkeypatch.setenv('FLEETMUX_PAGE', 'bash-3')
    touch('pi', 's1', repo / 'a.py', repo)
    monkeypatch.delenv('FLEETMUX_PAGE')
    got = touch('claude', 's2', repo / 'a.py', repo)
    assert 'bash-3' in got[0]


# ------------------------------------------------------------------ bash parsing
def test_parse_git_dash_c_and_cd_prefix(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    ops = sb.parse_git(f'git -C {a} commit -m "msg; with && stuff"', str(tmp_path))
    assert ops == [(str(a), 'commit', None)]
    ops = sb.parse_git(f'cd {b} && git status && git merge --no-ff feat', str(tmp_path))
    assert ops == [(str(b), 'merge', None)]
    ops = sb.parse_git('cd sub && git -C inner checkout -b topic', str(tmp_path))
    assert ops == [(str(tmp_path / 'sub' / 'inner'), 'checkout', 'topic')]
    ops = sb.parse_git('git worktree add -b feat ../wt main', str(a))
    assert ops == [(str(a), 'worktree_add', 'feat')]
    assert sb.parse_git('git checkout -- a.py', str(a)) == []
    assert sb.parse_git('git merge --abort', str(a)) == []
    assert sb.parse_git('echo git commit', str(a)) == []
    assert sb.parse_git('ls -la', str(a)) == []
    assert sb.parse_git('git commit -m "unterminated', str(a)) == []   # unparseable: no crash


# ------------------------------------------------------------------ Claude hook, end to end
def run_hook(payload, board_dir, raw=None):
    env = {**os.environ, 'FLEETMUX_BOARD_DIR': str(board_dir)}
    env.pop('FLEETMUX_PAGE', None)
    data = raw if raw is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(SCRIPT), 'hook', 'claude'], input=data,
                          capture_output=True, text=True, env=env, timeout=30)


def test_claude_hook_stdin_to_additional_context(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    base = {'transcript_path': '/x.jsonl', 'cwd': str(repo)}
    for sid in ('c1', 'c2'):
        out = run_hook({**base, 'session_id': sid, 'hook_event_name': 'SessionStart', 'source': 'startup'}, board)
        assert out.returncode == 0 and out.stdout == ''
    out = run_hook({**base, 'session_id': 'c1', 'hook_event_name': 'UserPromptSubmit', 'prompt': '重写侧栏'}, board)
    assert out.stdout == ''
    out = run_hook({**base, 'session_id': 'c1', 'hook_event_name': 'PostToolUse', 'tool_name': 'Edit',
                    'tool_input': {'file_path': str(repo / 'a.py'), 'old_string': 'a', 'new_string': 'b'},
                    'tool_response': {}}, board)
    assert out.returncode == 0 and out.stdout == ''
    out = run_hook({**base, 'session_id': 'c2', 'hook_event_name': 'PostToolUse', 'tool_name': 'Write',
                    'tool_input': {'file_path': str(repo / 'a.py'), 'content': 'x'}, 'tool_response': {}}, board)
    assert out.returncode == 0
    spec = json.loads(out.stdout)['hookSpecificOutput']
    assert spec['hookEventName'] == 'PostToolUse'
    assert 'c1' in spec['additionalContext'] and 'a.py' in spec['additionalContext']
    assert '重写侧栏' in spec['additionalContext']
    out = run_hook({**base, 'session_id': 'c1', 'hook_event_name': 'UserPromptSubmit', 'prompt': '继续'}, board)
    spec = json.loads(out.stdout)['hookSpecificOutput']
    assert spec['hookEventName'] == 'UserPromptSubmit' and 'c2' in spec['additionalContext']
    out = run_hook({**base, 'session_id': 'c1', 'hook_event_name': 'UserPromptSubmit', 'prompt': '再继续'}, board)
    assert out.stdout == ''                                                   # told once
    out = run_hook({**base, 'session_id': 'c2', 'hook_event_name': 'SessionEnd', 'reason': 'other'}, board)
    assert out.returncode == 0 and out.stdout == ''
    out = run_hook({**base, 'session_id': 'c3', 'hook_event_name': 'PostToolUse', 'tool_name': 'NotebookEdit',
                    'tool_input': {'notebook_path': str(repo / 'a.py'), 'new_source': ''}}, board)
    spec = json.loads(out.stdout)['hookSpecificOutput']
    assert 'c1' in spec['additionalContext'] and 'c2' not in spec['additionalContext']   # c2 ended


def test_claude_hook_bash_git_merge(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    git('branch', 'x', cwd=repo)
    for sid in ('m1', 'm2'):
        out = run_hook({'session_id': sid, 'cwd': str(tmp_path), 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                        'tool_input': {'command': f'cd {repo} && git merge x'}, 'tool_response': {}}, board)
        assert out.returncode == 0
    assert 'm1' in json.loads(out.stdout)['hookSpecificOutput']['additionalContext']


@pytest.mark.parametrize('raw', ['', 'not json', '[1, 2]', '{"hook_event_name": "PostToolUse"}',
                                 '{"session_id": "s", "hook_event_name": "PostToolUse", "tool_input": "x"}',
                                 '{"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",'
                                 ' "tool_input": {"command": 7}, "cwd": "/nonexistent/dir"}'])
def test_claude_hook_bad_input_is_silent(board, raw):
    out = run_hook(None, board, raw=raw)
    assert out.returncode == 0 and out.stdout == '' and out.stderr == ''


def test_claude_hook_error_goes_to_errors_log(board):
    out = run_hook(None, board, raw='not json')
    assert out.returncode == 0
    assert (board / 'errors.log').read_text().strip()


def test_unwritable_board_still_exits_zero(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('')
    out = run_hook({'session_id': 's', 'hook_event_name': 'SessionStart', 'cwd': str(tmp_path)}, blocker / 'board')
    assert out.returncode == 0 and out.stdout == ''


def test_pi_cli_prints_notices(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    touch('claude', 's1', repo / 'a.py', repo)
    env = {**os.environ, 'FLEETMUX_BOARD_DIR': str(board)}
    out = subprocess.run([sys.executable, str(SCRIPT), 'hook', 'pi',
                          json.dumps({'event': 'tool', 'session_id': 'p1', 'cwd': str(repo), 'path': 'a.py',
                                      'name': 'bash-3', 'pid': os.getpid()})],
                         capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0 and 's1' in out.stdout and 'a.py' in out.stdout
    rec = json.loads((board / 'pi-p1.json').read_text())
    assert rec['agent'] == 'pi' and rec['name'] == 'bash-3' and rec['touches'][0]['path'] == 'a.py'


def test_claude_hook_read_is_not_a_touch(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    run_hook({'session_id': 'r1', 'cwd': str(repo), 'hook_event_name': 'PostToolUse', 'tool_name': 'Edit',
              'tool_input': {'file_path': str(repo / 'a.py')}}, board)
    out = run_hook({'session_id': 'r2', 'cwd': str(tmp_path), 'hook_event_name': 'PostToolUse', 'tool_name': 'Read',
                    'tool_input': {'file_path': str(repo / 'a.py')}}, board)
    assert out.returncode == 0 and out.stdout == ''                          # reading is not editing


def test_script_runs_through_a_symlink(board, tmp_path):
    """launchers/install-session-board.sh links the script next to the pi extension."""
    repo = make_repo(tmp_path / 'proj')
    touch('claude', 's1', repo / 'a.py', repo)
    link = tmp_path / 'ext' / 'session_board.py'
    link.parent.mkdir()
    link.symlink_to(SCRIPT)
    out = subprocess.run([sys.executable, str(link), 'hook', 'pi',
                          json.dumps({'event': 'tool', 'session_id': 'p1', 'cwd': str(repo), 'path': 'a.py'})],
                         capture_output=True, text=True, env={**os.environ, 'FLEETMUX_BOARD_DIR': str(board)}, timeout=30)
    assert out.returncode == 0 and 's1' in out.stdout
    assert not (board / 'errors.log').exists()


# ------------------------------------------------------------------ list / note (the supervisor's side)
def board_cli(board_dir, *args):
    env = {**os.environ, 'FLEETMUX_BOARD_DIR': str(board_dir)}
    env.pop('FLEETMUX_PAGE', None)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def board_json(board_dir):
    out = board_cli(board_dir, 'list', '--json')
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def collide(repo):
    """A (claude aaaa1111, goal set) edits a.py first, then B (pi bbbb2222, named bash-3) edits it too."""
    sb.handle('claude', {'event': 'prompt', 'session_id': 'aaaa1111', 'cwd': str(repo), 'prompt': '重写侧栏'})
    touch('claude', 'aaaa1111', repo / 'a.py', repo)
    sb.handle('pi', {'event': 'prompt', 'session_id': 'bbbb2222', 'cwd': str(repo), 'name': 'bash-3',
                     'prompt': '修滚动条'})
    assert len(touch('pi', 'bbbb2222', repo / 'a.py', repo)) == 1


def test_list_shows_sessions_and_a_running_collision(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    collide(repo)
    before = {p.name: p.read_bytes() for p in board.glob('*.json')}
    got = board_json(board)
    assert {p.name: p.read_bytes() for p in board.glob('*.json')} == before          # read-only
    sessions = {s['session_id']: s for s in got['sessions']}
    assert set(sessions) == {'aaaa1111', 'bbbb2222'}
    assert sessions['bbbb2222']['name'] == 'bash-3' and sessions['bbbb2222']['agent'] == 'pi'
    assert sessions['aaaa1111']['goal'] == '重写侧栏' and 'updated_at' in sessions['aaaa1111']
    pair, = got['collisions']                                                        # one pair, both sides' views merged
    assert pair['repo_name'] == 'proj' and pair['files'] == ['a.py']
    assert pair['first']['session_id'] == 'aaaa1111' and pair['later']['session_id'] == 'bbbb2222'
    assert pair['first_done'] is False and pair['released'] == []
    assert got['pending'] == []


def test_list_pending_after_the_first_ends_until_a_note_releases_it(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    collide(repo)
    sb.handle('claude', {'event': 'end', 'session_id': 'aaaa1111', 'cwd': str(repo)})
    got = board_json(board)
    pair, = got['collisions']
    assert pair['first_done'] is True and pair['released'] == []
    pending, = got['pending']
    assert pending['first']['session_id'] == 'aaaa1111' and pending['later']['session_id'] == 'bbbb2222'
    human = board_cli(board, 'list')
    assert human.returncode == 0
    for word in ('在线会话', '撞车', '待协调', 'bash-3', '先到', '后到', 'a.py', 'proj', '已完成'):
        assert word in human.stdout, word

    out = board_cli(board, 'note', 'bash-3', '先到者已完成，先 rebase 到最新 main 再继续改 a.py', '--from', 'sup-1')
    assert out.returncode == 0, out.stderr
    got = board_json(board)
    pair, = got['collisions']
    assert got['pending'] == [] and [r['from'] for r in pair['released']] == ['sup-1']
    assert 'sup-1' in board_cli(board, 'list').stdout

    told = check('pi', 'bbbb2222', repo)                                             # delivered once
    assert len(told) == 1 and 'sup-1' in told[0] and '先 rebase 到最新 main 再继续改 a.py' in told[0]
    assert check('pi', 'bbbb2222', repo) == []
    assert [r['from'] for r in board_json(board)['collisions'][0]['released']] == ['sup-1']


def test_a_note_before_the_first_finished_is_not_a_release(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    collide(repo)
    assert board_cli(board, 'note', 'bbbb2222', '再等等').returncode == 0
    sb.handle('claude', {'event': 'end', 'session_id': 'aaaa1111', 'cwd': str(repo)})
    pending, = board_json(board)['pending']
    assert pending['later']['session_id'] == 'bbbb2222'


def test_list_first_done_when_its_edits_in_the_repo_pass_the_ttl(board, tmp_path, monkeypatch):
    monkeypatch.setenv('FLEETMUX_BOARD_TTL_S', '100')
    repo, other = make_repo(tmp_path / 'proj'), make_repo(tmp_path / 'other')
    t0 = time.time()
    touch('claude', 'A', repo / 'a.py', repo, now=t0)
    assert len(touch('claude', 'B', repo / 'a.py', repo, now=t0 + 10)) == 1
    touch('claude', 'A', other / 'a.py', other, now=t0 + 90)                         # A still online, elsewhere
    touch('claude', 'B', repo / 'b.py', repo, now=t0 + 90)
    got = sb.board_state(now=t0 + 105)
    pair, = got['collisions']
    assert pair['first']['session_id'] == 'A' and pair['first_done'] is True
    assert [p['later']['session_id'] for p in got['pending']] == ['B']
    assert sb.board_state(now=t0 + 50)['pending'] == []


def test_list_drops_a_pair_whose_later_session_ended(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    collide(repo)
    sb.handle('pi', {'event': 'end', 'session_id': 'bbbb2222', 'cwd': str(repo)})
    sb.handle('claude', {'event': 'end', 'session_id': 'aaaa1111', 'cwd': str(repo)})
    got = board_json(board)
    assert got['collisions'] == [] and got['pending'] == [] and got['sessions'] == []


def test_list_on_an_empty_board(board):
    got = board_json(board)
    assert got == {'sessions': [], 'collisions': [], 'pending': []}
    human = board_cli(board, 'list')
    assert human.returncode == 0 and '待协调' in human.stdout


def test_note_target_resolution(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    sb.handle('pi', {'event': 'start', 'session_id': 'p1', 'cwd': str(repo), 'name': 'twin'})
    sb.handle('pi', {'event': 'start', 'session_id': 'p2', 'cwd': str(repo), 'name': 'twin'})
    out = board_cli(board, 'note', 'twin', 'hi')
    assert out.returncode != 0 and 'p1' in out.stderr and 'p2' in out.stderr               # ambiguous: no guess
    out = board_cli(board, 'note', 'nobody', 'hi')
    assert out.returncode != 0 and 'nobody' in out.stderr
    assert board_cli(board, 'note', 'p2', 'hi there').returncode == 0                       # by session id
    rec = json.loads((board / 'pi-p2.json').read_text())
    item, = rec['inbox']
    assert item['text'] == 'hi there' and item['from'] and item['ts'] and not item.get('delivered_at')
    assert 'inbox' not in json.loads((board / 'pi-p1.json').read_text())


def test_note_reaches_a_claude_session_only_on_an_event_that_shows_it(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    base = {'cwd': str(repo), 'session_id': 'c1'}
    run_hook({**base, 'hook_event_name': 'SessionStart', 'source': 'startup'}, board)
    assert board_cli(board, 'note', 'c1', '可以继续改 a.py 了', '--from', 'sup-2').returncode == 0
    out = run_hook({**base, 'hook_event_name': 'Stop'}, board)                         # not shown: not consumed
    assert out.returncode == 0 and out.stdout == ''
    out = run_hook({**base, 'hook_event_name': 'UserPromptSubmit', 'prompt': '继续'}, board)
    text = json.loads(out.stdout)['hookSpecificOutput']['additionalContext']
    assert '可以继续改 a.py 了' in text and 'sup-2' in text
    out = run_hook({**base, 'hook_event_name': 'UserPromptSubmit', 'prompt': '再继续'}, board)
    assert out.stdout == ''                                                               # once


def test_note_reaches_a_pi_session_on_its_next_check(board, tmp_path):
    repo = make_repo(tmp_path / 'proj')
    sb.handle('pi', {'event': 'start', 'session_id': 'p1', 'cwd': str(repo), 'name': 'bash-3'})
    assert board_cli(board, 'note', 'bash-3', '先拉最新 main 再继续').returncode == 0
    env = {**os.environ, 'FLEETMUX_BOARD_DIR': str(board)}
    ev = json.dumps({'event': 'check', 'session_id': 'p1', 'cwd': str(repo)})
    out = subprocess.run([sys.executable, str(SCRIPT), 'hook', 'pi', ev], capture_output=True, text=True, env=env,
                         timeout=30)
    assert out.returncode == 0 and '先拉最新 main 再继续' in out.stdout
    out = subprocess.run([sys.executable, str(SCRIPT), 'hook', 'pi', ev], capture_output=True, text=True, env=env,
                         timeout=30)
    assert out.stdout == ''


def test_machine_prompts_do_not_replace_the_goal(board, tmp_path):
    """Claude feeds background-task / teammate notices through UserPromptSubmit wrapped in a tag
    (`<task-notification>…`); they are not the user's words, so the last real goal stays (2026-11-08 user: 改)."""
    repo = make_repo(tmp_path / 'proj')
    ev = {'event': 'prompt', 'session_id': 'aaaa1111', 'cwd': str(repo)}
    sb.handle('claude', dict(ev, prompt='重写侧栏'))
    for machine in ('<task-notification> <task-id>bo00h0jfu</task-id> done</task-notification>',
                    '\n  <teammate-message from="x">hi</teammate-message>', '<system-reminder>x</system-reminder>',
                    '   '):
        sb.handle('claude', dict(ev, prompt=machine))
    sessions = {s['session_id']: s for s in board_json(board)['sessions']}
    assert sessions['aaaa1111']['goal'] == '重写侧栏'
    sb.handle('claude', dict(ev, prompt='<div> 这个标签要改成 span'))  # user text that merely starts with a tag
    assert {s['session_id']: s for s in board_json(board)['sessions']}['aaaa1111']['goal'] == '<div> 这个标签要改成 span'
    sb.handle('claude', dict(ev, prompt='把 <task-notification> 过滤掉'))
    sessions = {s['session_id']: s for s in board_json(board)['sessions']}
    assert sessions['aaaa1111']['goal'] == '把 <task-notification> 过滤掉'
