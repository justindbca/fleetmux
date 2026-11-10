"""opencode launch/configuration helpers and wire checks (fourth worker CLI).

Measured (worker-3 preflight, opencode 2.0.29): the plugin creates the session itself with an
opencode-generated id (a self-made session id is refused by Zen), the TUI shows it with
`--standalone --continue` on a fresh per-generation data dir, and the isolation that keeps only
the fleet role in the session instructions is "full2": every XDG dir + OPENCODE_CONFIG_DIR +
OPENCODE_TEST_HOME + OPENCODE_DISABLE_PROJECT_CONFIG, role in $OPENCODE_CONFIG_DIR/AGENTS.md.

The free tier only works from inside the opencode process (a direct Zen call answers
FreeTierError), so fleet runs the opencode TUI itself with an in-process plugin
(opencode_plugin/) that talks JSONL to agent_bridge.py, exactly like the Pi extension.
No model endpoint, credential or model name is embedded here: the model is configuration
(`opencode_model`, the free list changes often) and is checked on every settled turn.
"""
import json
import math
import os
from pathlib import Path
import re

KIND = 'opencode_session'                 # bridge_session identity kind for opencode slots
DEFAULT_WINDOW = 200000                   # free-tier context window (paid tier is 1M)
PLUGIN_DIR = Path(__file__).with_name('opencode_plugin')
ERROR_CLASSES = ('free_tier_rejected', 'rate_limited', 'model_unavailable', 'model_lock', 'aborted', 'other')

# Deletes / moves / renames: the permission request is the user's call only (design §24, user 2026-10-14).
# These rules make opencode stop and ask; nothing in fleet ever answers them.
DELETE_SHELL = ('rm *', 'rmdir *', 'unlink *', 'mv *', 'shred *', 'trash *', 'rimraf *', 'truncate *',
                'git rm *', 'git mv *', 'git clean*', 'git reset --hard*', 'git checkout --*', 'git restore*',
                'git branch -D*', 'git branch -d*', 'git push --force*', 'git push -f*', 'find * -delete*',
                'dd *')
_DELETE_PREFIXES = (' ', '/', '"', "'", '(', ')', ';', '&', '|', '`', '$', '{', '.', '-', '\t', '\n')

_FREE_TIER = re.compile(r'freetiererror|free tier can only be used', re.I)
_RATE = re.compile(r'rate[ -]?limit|too many requests|ratelimit', re.I)  # bare "429" in text is not used: token counts hit it
_UNAVAILABLE = re.compile(r'provider\.no-route|model unavailable|model not found|unknown model|no such model', re.I)
_ABORTED = re.compile(r'\baborted\b|\binterrupted\b|AbortError', re.I)


def model(cfg):
    """(providerID, modelID) from config `opencode_model` = "provider/model". Never a default."""
    value = cfg.get('opencode_model')
    if not isinstance(value, str) or value.count('/') < 1:
        raise ValueError('opencode_model provider/model is required for opencode agents')
    provider, _, ident = value.partition('/')
    if not provider or not ident or value != value.strip():
        raise ValueError('opencode_model must be provider/model')
    return provider, ident


def for_agent(cfg, name, overrides=None):
    """cfg as one agent sees it: `opencode_models[name]` over run-wide `opencode_model`;
    profile-launch overrides win. Idempotent like pi_adapter.for_agent."""
    out, overrides = dict(cfg), overrides or {}
    own = (cfg.get('opencode_models') or {}).get(name)
    if own:
        out['opencode_model'] = own
    if 'opencode_model' in overrides and own:
        out['opencode_models'] = {k: v for k, v in cfg['opencode_models'].items() if k != name}
    out.update(overrides)
    return out


def window(cfg):
    value = cfg.get('opencode_window', DEFAULT_WINDOW)
    if type(value) is not int or value <= 0:
        raise ValueError('opencode_window must be a positive integer')
    return value


INSTANCE_DIRS = ('cfg', 'xdgcfg', 'xdgstate', 'xdgcache', 'xdgdata', 'home')


def instance_dir(cfg, name, generation):
    """Per-generation opencode home: config, state, cache, data (its own opencode.db) and test home."""
    return Path(cfg['run']) / 'opencode' / (name + '-g' + str(generation))


def launch_cwd(cfg, name, generation):
    """The opencode process cwd and argv directory: a fresh EMPTY directory per generation, outside
    every project / output directory (worker-3 isolation-layout-probe, supervisor 2026-10-16).
    opencode's Read loads nested AGENTS.md only for files inside this directory tree; project files
    are external directories, for which Read skips that loading (source fact of 2.0.29, not a
    documented guarantee: re-run the nested-AGENTS regression after any opencode upgrade).
    This stops automatic instruction injection only; it is not a shell sandbox."""
    return instance_dir(cfg, name, generation) / 'launch'


def trusted_dirs(cfg, name, generation, task_dir):
    """Exact directories opencode may use without asking (external_directory allow): this slot's
    task directory plus the run's configured roots `opencode_trusted_dirs` (absolute paths, e.g. the
    run's shared output root, the task's project checkout). Everything else asks. Never global:
    '/', the home directory, wildcards, relative paths and any directory that contains, or lies
    inside, the launch cwd are refused."""
    launch = launch_cwd(cfg, name, generation).resolve()
    extra = cfg.get('opencode_trusted_dirs', [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise ValueError('opencode_trusted_dirs must be a list of absolute paths')
    out = []
    for raw in [str(task_dir)] + extra:
        if not raw or not Path(raw).is_absolute() or any(c in raw for c in '*?[]{}'):
            raise ValueError('opencode trusted directory must be an absolute path without wildcards: ' + repr(raw))
        path = Path(raw).resolve()
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError('opencode trusted directory too broad: ' + str(path))
        if path == launch or launch.is_relative_to(path) or path.is_relative_to(launch):
            raise ValueError('opencode trusted directory overlaps the launch cwd: ' + str(path))
        for form in (os.path.normpath(raw), str(path)):  # as given and symlink-resolved (/tmp -> /private/tmp)
            if form not in out:
                out.append(form)
    return out


def layout_instructions(task_dir, trusted):
    """Fleet's layout section, written FIRST into $OPENCODE_CONFIG_DIR/AGENTS.md."""
    lines = ['# Fleet layout (opencode worker) - read first', '',
             '- Your real task directory is `' + str(task_dir) + '`. Every relative path in the instructions below '
             '(`./<file>`, "your directory", "current directory") means a path inside it; write outputs there with '
             'absolute paths.',
             '- The process working directory is an empty launch directory owned by fleet. Do not create, edit or '
             'read files there.',
             '- For shell commands set `workdir` to the task / project directory or start with `cd <absolute dir> &&`. '
             'For git always name the repository: `git -C <absolute project dir> ...`; do not rely on built-in '
             'VCS / worktree shortcuts (they point at the launch directory).',
             '- Directories you may use without asking: ' + ', '.join('`' + d + '`' for d in trusted) + '. Any other '
             'directory asks for permission first.', '']
    return '\n'.join(lines) + '\n'


def prepare_instance(cfg, name, generation, role_text, *, task_dir):
    """A fresh instance directory per generation (never reused: a new DB means --continue can only
    pick this generation's session), with its empty launch cwd. The role is the only instruction
    file: $OPENCODE_CONFIG_DIR/AGENTS.md = fleet layout section + role_text. Never write AGENTS.md
    into the launch cwd or the task directory tree the worker reads as its cwd."""
    if not isinstance(role_text, str) or not role_text.strip():
        raise ValueError('opencode role instructions are required')
    trusted = trusted_dirs(cfg, name, generation, task_dir)
    root = instance_dir(cfg, name, generation)
    root.mkdir(parents=True, exist_ok=False)
    for sub in INSTANCE_DIRS + ('launch',):
        (root / sub).mkdir(mode=0o700)
    (root / 'cfg' / 'AGENTS.md').write_text(layout_instructions(Path(trusted[0]), trusted) + '\n' + role_text,
                                            encoding='utf-8')
    return root


def isolation_env(cfg, name, generation):
    root = instance_dir(cfg, name, generation)
    return {'XDG_CONFIG_HOME': str(root / 'xdgcfg'), 'XDG_STATE_HOME': str(root / 'xdgstate'),
            'XDG_CACHE_HOME': str(root / 'xdgcache'), 'XDG_DATA_HOME': str(root / 'xdgdata'),
            'OPENCODE_CONFIG_DIR': str(root / 'cfg'), 'OPENCODE_TEST_HOME': str(root / 'home'),
            'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_AUTOUPDATE': '1'}


def session_dir(cfg, name, generation):
    """Per-generation directory where the plugin keeps its activity log `<session>.jsonl`.
    opencode stores sessions in its own database; this log is fleet's transcript (bridge
    identity, watchdog last_activity)."""
    return Path(cfg['run']) / 'opencode-sessions' / name / ('g' + str(generation))


def delete_shell_patterns():
    """Shell resource globs that must ask. opencode splits a shell call into sub-commands and checks
    each one as a resource ("ls && rm x" -> ["ls", "rm x"], "(rm x)" -> ["rm x"]); "*" matches
    anything, matching is case-sensitive (measured, worker-1 api-contract-supplement.md §8).
    Every watchdog.DELETE_MARKERS word asks at the start of a resource or after a space, path,
    quote, bracket, separator, "." or "-": "sudo rm x", "/bin/rm x", 'bash -c "rm x"',
    "find . -delete", 'python3 -c "os.remove(...)"'. Lower and upper case. Other markers anywhere.
    Strict on purpose: an extra question only reaches the user once more."""
    from cmux_fleet import watchdog
    markers = list(watchdog.DELETE_MARKERS) + ['git branch -D']
    out = list(DELETE_SHELL)
    for m in markers:
        if re.fullmatch(r'[a-z0-9][a-z0-9 =\-]*', m, re.I):
            for w in dict.fromkeys((m, m.upper())):
                out.append(w)                                            # whole resource
                out.extend(w + q + '*' for q in (' ', '\t', ';', '(', '"', "'"))   # command word ("mvn" is not "mv")
                out.extend('*' + p + w + '*' for p in _DELETE_PREFIXES)  # inside: "x rm", ".rmtree(", "-delete" 
        else:
            out.append('*' + m + '*')
    return list(dict.fromkeys(out))


def permission_rules(cfg, trusted=()):
    """opencode `permission` config (v1 shape, parsed into v2 rules in key order; the LAST matching
    rule wins, nothing matching = ask). Only the `shell` key: opencode renames `bash` to `shell`, so
    writing both would append a second "*": allow after the delete asks and undo them (measured
    mapping, worker-3 preflight-supplement §2.1). Delete-class commands ask (held for the user);
    everything else is allowed. No global --auto.
    external_directory: "*" ask FIRST (the built-in build agent starts with *:*:allow), then allow only
    the exact trusted directories and their trees (`<dir>/*` covers subdirectories, measured)."""
    shell = {'*': 'allow'}
    shell.update({pattern: 'ask' for pattern in delete_shell_patterns()})
    external = {'*': 'ask'}
    for d in trusted:
        external[d] = 'allow'
        external[d.rstrip('/') + '/*'] = 'allow'
    return {'shell': shell, 'edit': 'allow', 'webfetch': 'allow',
            'external_directory': external, 'doom_loop': 'ask'}


def config_content(cfg, trusted=()):
    """OPENCODE_CONFIG_CONTENT: only the fleet plugin, the locked model, permission rules."""
    provider, ident = model(cfg)
    return json.dumps({'plugin': [str(PLUGIN_DIR)], 'model': provider + '/' + ident,
                       'autoupdate': False, 'share': 'disabled', 'permission': permission_rules(cfg, trusted)},
                      ensure_ascii=False)


def argv(cfg, name, generation, prompt, cwd, *, label):
    """Interactive opencode TUI with its own private server; the plugin lives in this process,
    creates the session and injects the startup message itself (CMUX_FLEET_OC_STARTUP), so the
    prompt is not on argv. The plugin directory's TUI half (tui.js) opens the plugin's session by id;
    --continue only picks the newest session that exists when the TUI starts (P1-01).
    `cwd` is the slot's TASK directory (checked against the layout); opencode itself runs in the
    generation's empty launch_cwd(), which must also be the process cwd (the plugin verifies it)."""
    model(cfg)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('opencode startup prompt is required')
    trusted_dirs(cfg, name, generation, cwd)
    return [cfg.get('opencode') or 'opencode', '--standalone', '--continue', str(launch_cwd(cfg, name, generation))]


def environment(cfg_path, cfg, name, generation, *, label=None, startup_prompt=None, task_dir=None):
    """Environment for the opencode process: full2 isolation (prepare_instance first), the fleet
    plugin and its bridge argv. startup_prompt is required: the plugin injects it and binds the
    startup turn by the inbox id prompt() returns. The plugin waits (bounded, catalogue_wait_s)
    until the locked model is in the fresh DB's model catalogue before the startup turn.
    task_dir (required) = the slot's task directory; with opencode_trusted_dirs it sets the
    external_directory allow list."""
    provider, ident = model(cfg)
    if task_dir is None:
        raise ValueError('opencode task_dir is required (external_directory allow list)')
    trusted = trusted_dirs(cfg, name, generation, task_dir)
    if not isinstance(startup_prompt, str) or not startup_prompt.strip():
        raise ValueError('opencode startup prompt is required')
    wait = cfg.get('opencode_catalog_wait_s', 60)
    if type(wait) is not int or not 1 <= wait <= 600:
        raise ValueError('opencode_catalog_wait_s must be an integer 1-600')
    env = dict(isolation_env(cfg, name, generation))
    env.update({
        'OPENCODE_CONFIG_CONTENT': config_content(cfg, trusted),
        'CMUX_FLEET_OC_LAUNCH_CWD': str(launch_cwd(cfg, name, generation)),
        'CMUX_FLEET_OC_BRIDGE': json.dumps([cfg['python'], str(Path(__file__).with_name('agent_bridge.py')),
                                            str(cfg_path), name, str(generation)]),
        'CMUX_FLEET_OC_SLOT': name, 'CMUX_FLEET_OC_GEN': str(generation),
        'CMUX_FLEET_OC_MODE': cfg.get('mode', 'task'),
        'CMUX_FLEET_OC_MODEL': provider + '/' + ident,
        'CMUX_FLEET_OC_WINDOW': str(window(cfg)),
        'CMUX_FLEET_OC_SESSION_DIR': str(session_dir(cfg, name, generation)),
        'CMUX_FLEET_OC_STARTUP': startup_prompt,
        'CMUX_FLEET_OC_CATALOG_WAIT_S': str(wait),
        'CMUX_FLEET_OC_LABEL': label or name,
    })
    return env


def event(cfg, name, generation, data):
    """Validate the opencode adapter identity before anything reaches the private Redis."""
    if not isinstance(data, dict) or data.get('kind') != KIND:
        raise ValueError('invalid opencode event kind')
    thread = data.get('thread')
    if not isinstance(thread, str) or not re.fullmatch(r'ses_[A-Za-z0-9]{6,64}', thread):
        raise ValueError('opencode event requires an opencode session id (ses_...)')
    transcript = data.get('transcript')
    if not isinstance(transcript, str) or not transcript:
        raise ValueError('opencode event requires the plugin activity log')
    path = Path(transcript).resolve()
    if (path.suffix != '.jsonl' or path.stem != thread
            or not path.is_relative_to(session_dir(cfg, name, generation).resolve())):
        raise ValueError('opencode activity log must be <session>.jsonl in this agent generation directory')
    for key in ('ctx_used', 'ctx_window'):
        value = data.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('invalid opencode context usage')
    fields = ('kind', 'thread', 'transcript', 'ctx_used', 'ctx_window')
    return dict({k: data[k] for k in fields if k in data}, agent=name, generation=generation)


def _cost_ok(cost):
    return type(cost) in (int, float) and math.isfinite(cost) and cost == 0


def model_problem(cfg, info):
    """None when the assistant message really used the locked free model; otherwise the reason.
    info = {'model': {'providerID', 'id'}, 'cost'} (assistant message fields; flat providerID/modelID
    accepted too). Strict: cost must be a finite number equal to 0 (negative / NaN / missing fail)."""
    provider, ident = model(cfg)
    if not isinstance(info, dict):
        return 'model_lock: no assistant message to check'
    used = info.get('model') if isinstance(info.get('model'), dict) else {}
    got_provider = used.get('providerID', info.get('providerID'))
    got_id = used.get('id', used.get('modelID', info.get('modelID')))
    if got_provider != provider or got_id != ident:
        return 'model_lock: expected %s/%s, got %s/%s' % (provider, ident, got_provider, got_id)
    cost = info.get('cost')
    if type(cost) is bool or not _cost_ok(cost):
        return 'cost_nonzero: expected cost 0, got %r' % (cost,)
    return None


def classify_error(text, status=None):
    """Error class for a failed turn. The plugin never retries or switches models on any of them;
    free-tier refusals and 429s are provider-side and reported apart from task failures."""
    text = str(text or '')
    if _FREE_TIER.search(text):
        return 'free_tier_rejected'
    if status == 429 or _RATE.search(text):
        return 'rate_limited'
    if _UNAVAILABLE.search(text):
        return 'model_unavailable'
    if text.startswith('model_lock:') or text.startswith('cost_nonzero:'):
        return 'model_lock'
    if _ABORTED.search(text):
        return 'aborted'
    return 'other'


def classify_failure(error):
    """execution.failed data.error = {type, message, status, response: {body}} (measured:
    provider.auth 403 with a FreeTierError body; provider.no-route "Model unavailable")."""
    if not isinstance(error, dict):
        return classify_error(error)
    body = (error.get('response') or {}).get('body') if isinstance(error.get('response'), dict) else None
    text = ' | '.join(str(x) for x in (error.get('type'), error.get('status'), error.get('message'), body)
                      if x not in (None, ''))
    status = error.get('status') if type(error.get('status')) is int else None
    return classify_error(text, status)


def permission_text(req):
    """All text of a permission request that may name what it does. opencode 2.0.29 requests are
    {id, sessionID, action, resources[], metadata?, message?} (measured); older names kept."""
    if not isinstance(req, dict):
        return ''
    parts = [str(req.get(k) or '') for k in ('action', 'message', 'permission', 'type', 'title', 'command')]
    for key in ('resources', 'patterns', 'pattern'):
        value = req.get(key)
        parts.extend(str(x) for x in value) if isinstance(value, list) else parts.append(str(value or ''))
    meta = req.get('metadata')
    if isinstance(meta, dict):
        parts.extend(str(v) for v in meta.values() if isinstance(v, (str, int, float)))
    return '\n'.join(p for p in parts if p)


def permission_class(req):
    """'delete' | 'other'. Uses the watchdog delete word list (deletes, moves, renames). Unreadable
    requests count as delete: strict on purpose, a wrong 'delete' only asks the user once more."""
    from cmux_fleet import watchdog
    text = permission_text(req).lower()
    if not text.strip():
        return 'delete'
    return 'delete' if any(watchdog._marker_re(x).search(text) for x in watchdog.DELETE_MARKERS) else 'other'
