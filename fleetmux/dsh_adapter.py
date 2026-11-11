"""dsh (DeepSeek Harness) worker: launch helpers and wire checks (design §41, v1).

The pane runs the third-party dsh-TUI (`dsh --profile dsh-tui`, DSH in the same process) from one shared,
pre-installed DSH_HOME. Fleet adds two patch overlays: the model rows (one source: pi's models.json, so a pi
worker and a dsh worker use the same endpoint and model entry) and the fleet rows (this generation's session /
storage roots, and the in-process bridge plugin dsh_bridge.mjs, which spawns agent_bridge.py like the pi
extension). Each pane gets its own HOME, so dsh-TUI's ~/.dsh-tui state never touches the user's. The API key
never enters a file, the spawn command line or argv: the actor puts it into the pane's environment only.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

import yaml

from fleetmux import paths

KIND = 'dsh_session'                  # bridge_session identity kind for dsh slots
VERSION = '0.2.1-rc.5'                # exact: rc, no version negotiation
TUI_VERSION = '0.18.0'                # exact: the dsh-TUI the bridge plugin was measured against
PROFILE = 'dsh-tui'
TUI_PACKAGE = '@deepseek-harness-tui/dsh-tui'
NODE_MIN = (22, 19, 0)
KEY_ENV = 'FLEETMUX_DSH_API_KEY'    # the patch's apiKeyEnv; set only in the pane env
PLUGIN = Path(__file__).with_name('dsh_bridge.mjs')
PLUGIN_ID = 'fleetmux-bridge'
DEFAULT_HOME = None   # None: paths.default_home() / 'dsh-home', the shared DSH_HOME with the dsh-tui profile
HOME_LINKS = ('.gitconfig', '.npmrc')  # the only files of the real home a pane sees (user decision; never ~/.ssh)
DEFAULT_MAX_BUSY = 2                  # FlashNext is a shared server: at most 2 busy agents
_SID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

# Replaces pi_adapter.UNSANDBOXED_SCOPE in a dsh worker's AGENTS.md.
SCOPE = ('You run inside dsh\'s workspace-write file sandbox: writes outside your directory stop for an '
         'approval, which the fleet routes (deletes and moves go to the user). The sandbox does not block the '
         'network. Do not drive the user\'s browser directly; use only the broker command below if the '
         'operator enabled it.')


def model(cfg):
    """(provider, model id) from config `dsh_model` = "provider/model". Never a default."""
    value = cfg.get('dsh_model')
    if not isinstance(value, str) or '/' not in value or value != value.strip():
        raise ValueError('dsh_model provider/model is required for dsh workers')
    provider, _, ident = value.partition('/')
    if not provider or not ident:
        raise ValueError('dsh_model must be provider/model')
    return provider, ident


def default_home():
    return DEFAULT_HOME or paths.default_home() / 'dsh-home'


def dsh_home(cfg):
    """The shared DSH_HOME holding the pre-installed dsh-tui profile (read by every pane, never per run)."""
    return Path(cfg.get('dsh_home') or default_home()).expanduser()


def pane_home(cfg, name, generation):
    """This generation's HOME: dsh-TUI's ~/.dsh-tui, the patches and DSH's session / storage roots."""
    return Path(cfg['run']) / 'dsh-tui-home' / name / ('g' + str(generation))


def session_dir(cfg, name, generation):
    """Where the bridge plugin keeps its transcript `<session id>.jsonl` (bridge identity, watchdog activity).
    DSH's own session logs are not used as a signal."""
    return Path(cfg['run']) / 'dsh-sessions' / name / ('g' + str(generation))


def models_json(cfg):
    return Path(cfg.get('pi_agent_dir') or Path.home() / '.pi' / 'agent').expanduser() / 'models.json'


def provider_entry(cfg):
    """(provider entry, model entry) for dsh_model from pi's models.json, with fleet.yaml#pi_context
    windows applied the way the pi home gets them. Missing either is an error: no fallback."""
    from fleetmux import fleet_config, pi_adapter
    provider, ident = model(cfg)
    path = models_json(cfg)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('cannot read pi models.json for dsh_model: ' + str(exc)) from exc
    data = pi_adapter.context_models(data, fleet_config.block('pi_context'))
    entry = (data.get('providers') or {}).get(provider)
    if not isinstance(entry, dict):
        raise ValueError('dsh_model provider ' + repr(provider) + ' is not in ' + str(path))
    found = [m for m in entry.get('models') or [] if isinstance(m, dict) and m.get('id') == ident]
    if len(found) != 1:
        raise ValueError('dsh_model ' + repr(ident) + ' is not a model of provider ' + repr(provider) + ' in ' + str(path))
    if not entry.get('baseUrl') or not entry.get('api'):
        raise ValueError('pi provider ' + repr(provider) + ' needs baseUrl and api for dsh')
    return entry, found[0]


def api_key(cfg):
    """The provider's key from models.json ('' if it has none). Read at launch, kept out of every file."""
    entry, _ = provider_entry(cfg)
    return str(entry.get('apiKey') or '')


def model_patch(cfg):
    """The model rows (no secret): the pi-ai route for this one model, the default model, and the dsh-tui
    row pinned to it. A patch replaces a row's whole config: the dsh-tui row is restated with a complete
    provider + model pin (a half pin is ignored), the dsh backend (config outranks a remembered /kernel pick;
    `--backend dsh` on argv would also become a startup prompt) and no seeded upstream retry policy."""
    provider, ident = model(cfg)
    entry, item = provider_entry(cfg)
    route = {'displayName': provider, 'api': entry['api'], 'baseURL': entry['baseUrl'],
             'models': [{k: item[k] for k in ('id', 'name', 'contextWindow', 'maxTokens') if k in item}]}
    if entry.get('apiKey'):
        route['apiKeyEnv'] = KEY_ENV
    chosen = {'provider': provider, 'model': ident}
    return [{'id': 'llm-pi-ai', 'config': {'providers': {provider: route}}},
            {'id': 'agent-default-model', 'config': dict(chosen)},
            {'id': 'dsh-tui', 'config': dict(chosen, backend='dsh', upstreamRetry=False)}]


def fleet_patch(cfg, name, generation):
    """The fleet rows: DSH's session and storage roots inside this generation (not the shared DSH_HOME;
    dsh-tui-storage-json is the TUI's own copy of the storage row), and the bridge plugin."""
    state = pane_home(cfg, name, generation) / 'dsh'
    return [{'id': 'session-persistence-jsonl', 'config': {'root': str(state / 'sessions')}},
            {'id': 'storage-json', 'config': {'root': str(state / 'storages')}},
            {'id': 'dsh-tui-storage-json', 'config': {'root': str(state / 'storages')}},
            {'insert': [{'id': PLUGIN_ID, 'name': str(PLUGIN)}]}]


def patch_paths(cfg, name, generation):
    home = pane_home(cfg, name, generation)
    return home / 'model.patch.yml', home / 'fleet.patch.yml'


def prepare(cfg, name, generation, real_home=None):
    """A fresh pane HOME for this generation: dsh-TUI's first-run markers (no onboarding, no home screen),
    links to the real ~/.gitconfig and ~/.npmrc only, the empty DSH_AGENTS_HOME and both patches."""
    home = pane_home(cfg, name, generation)
    home.mkdir(parents=True, exist_ok=False)
    marks = home / '.dsh-tui'
    marks.mkdir()
    (marks / 'onboarding.json').write_text(json.dumps({'completed': True, 'version': 1}) + '\n')
    (marks / 'home.json').write_text(json.dumps({'seen': True}) + '\n')
    real = Path(real_home or Path.home())
    for item in HOME_LINKS:
        if (real / item).exists():
            (home / item).symlink_to(real / item)
    (home / 'agents').mkdir(mode=0o700)
    session_dir(cfg, name, generation).mkdir(parents=True, exist_ok=True)
    for path, rows in zip(patch_paths(cfg, name, generation), (model_patch(cfg), fleet_patch(cfg, name, generation))):
        path.write_text(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True), encoding='utf-8')
    return home


def environment(cfg_path, cfg, name, generation, *, label, startup_prompt):
    """The pane's environment (actor only; never the spawn command line). pi's models.json is read here,
    before HOME points at the pane home."""
    if not isinstance(startup_prompt, str) or not startup_prompt.strip():
        raise ValueError('dsh startup prompt is required')
    provider, ident = model(cfg)
    _, item = provider_entry(cfg)
    home = pane_home(cfg, name, generation)
    env = {'HOME': str(home), 'DSH_HOME': str(dsh_home(cfg)), 'DSH_AGENTS_HOME': str(home / 'agents'),
           'DSH_TELEMETRY_MODE': 'DISABLED', 'DSH_TELEMETRY_DISABLED': '1', 'DSH_TUI_NO_LAUNCHPAD': '1',
           'DSH_PERMISSION_MODE': 'workspace-write',
           'FLEETMUX_DSH_BRIDGE': json.dumps([cfg['python'], str(Path(__file__).with_name('agent_bridge.py')),
                                                str(cfg_path), name, str(generation)]),
           'FLEETMUX_DSH_SLOT': name, 'FLEETMUX_DSH_GEN': str(generation),
           'FLEETMUX_DSH_MODE': cfg.get('mode', 'task'), 'FLEETMUX_DSH_MODEL': provider + '/' + ident,
           'FLEETMUX_DSH_WINDOW': str(item.get('contextWindow') or ''), 'FLEETMUX_DSH_LABEL': label or name,
           'FLEETMUX_DSH_SESSION_DIR': str(session_dir(cfg, name, generation)),
           'FLEETMUX_DSH_LOG': str(home / 'dsh_bridge.log'), 'FLEETMUX_DSH_USER_HOME': str(Path.home()),
           'FLEETMUX_DSH_STARTUP': startup_prompt}
    key = api_key(cfg)
    if key:
        env[KEY_ENV] = key
    return env


def argv(cfg, name, generation):
    """Launcher flags only: anything after the first unknown flag reaches the app, and app positionals are
    a startup prompt (the startup turn is the plugin's own, by id)."""
    model_path, fleet_path = patch_paths(cfg, name, generation)
    return [cfg['dsh_node'], cfg['dsh'], '--profile', PROFILE, '--patch', str(model_path), '--patch', str(fleet_path)]


def event(cfg, name, generation, data):
    """Validate the dsh identity before anything reaches the private Redis."""
    if not isinstance(data, dict) or data.get('kind') != KIND:
        raise ValueError('invalid dsh event kind')
    thread = data.get('thread')
    if not isinstance(thread, str) or not _SID.fullmatch(thread):
        raise ValueError('dsh event requires the DSH session id')
    transcript = data.get('transcript')
    if not isinstance(transcript, str) or not transcript:
        raise ValueError('dsh event requires the bridge transcript')
    path = Path(transcript).resolve()
    if (path.suffix != '.jsonl' or path.stem != thread
            or not path.is_relative_to(session_dir(cfg, name, generation).resolve())):
        raise ValueError('dsh transcript must be <session>.jsonl in this agent generation directory')
    for key in ('ctx_used', 'ctx_window'):
        value = data.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('invalid dsh context usage')
    fields = ('kind', 'thread', 'transcript', 'ctx_used', 'ctx_window')
    return dict({k: data[k] for k in fields if k in data}, agent=name, generation=generation)


def endpoint_problem(cfg, timeout=10):
    """Why the provider's endpoint does not serve dsh_model, or None: GET <baseUrl>/models must list the
    model id (the key, if any, as a bearer token; it is never printed)."""
    provider, ident = model(cfg)
    entry, _ = provider_entry(cfg)
    url = entry['baseUrl'].rstrip('/') + '/models'
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + entry['apiKey']} if entry.get('apiKey') else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8', 'replace'))
    except (OSError, ValueError) as exc:
        return 'the ' + provider + ' endpoint ' + url + ' did not answer a model list: ' + str(exc)[:300]
    ids = [m.get('id') for m in (data.get('data') if isinstance(data, dict) else None) or [] if isinstance(m, dict)]
    if ident not in ids:
        return 'the ' + provider + ' endpoint ' + url + ' does not list ' + repr(ident) + ' (' + str(len(ids)) + ' models listed)'
    return None


def _version(argv):
    out = subprocess.run(argv, text=True, capture_output=True, timeout=30, check=True).stdout.strip()
    return out.split()[-1] if out.split() else ''


def tui_version(home):
    """The dsh-tui version installed in the shared DSH_HOME's dsh-tui profile, or None."""
    manifest = Path(home) / 'profiles' / PROFILE / 'node_modules' / TUI_PACKAGE / 'package.json'
    try:
        return json.loads(manifest.read_text()).get('version')
    except (OSError, ValueError):
        return None


def install_command(home):
    return 'DSH_HOME=' + str(home) + ' dsh plugin --profile ' + PROFILE + ' add ' + TUI_PACKAGE + '@' + TUI_VERSION


def start_problem(cfg):
    """Why `start` must refuse dsh workers, or None: transport, model, binaries, exact versions and the
    pre-installed dsh-tui profile (never installed per run)."""
    if cfg.get('supervisor_cli') == 'dsh':
        return 'dsh runs only as a worker; pick pi, codex or claude for --supervisor-cli'
    if cfg.get('transport_version') != 2:
        return 'dsh workers run only on transport v2 (dsh-TUI + bridge plugin + agent_bridge.py)'
    try:
        provider_entry(cfg)
    except ValueError as exc:
        return '--dsh-model: ' + str(exc)
    if not cfg.get('dsh') or not cfg.get('dsh_node'):
        return 'dsh workers need the dsh binary and node on PATH (or --dsh)'
    try:
        node = _version([cfg['dsh_node'], '--version'])
        found = _version([cfg['dsh_node'], cfg['dsh'], '--version'])
    except (OSError, subprocess.SubprocessError) as exc:
        return 'dsh / node --version failed: ' + str(exc)
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', node)
    if not match or tuple(map(int, match.groups())) < NODE_MIN:
        return 'node >= ' + '.'.join(map(str, NODE_MIN)) + ' is required for dsh; found: ' + node
    if found != VERSION:
        return 'dsh == ' + VERSION + ' is required (rc, no version negotiation); found: ' + found
    home = dsh_home(cfg)
    tui = tui_version(home)
    if tui != TUI_VERSION:
        return ('dsh-tui == ' + TUI_VERSION + ' must be installed in the shared DSH_HOME ' + str(home) + ' (--dsh-home); '
                + ('found: ' + str(tui) if tui else 'not installed') + '. Install it once (about 2.5 minutes): '
                + install_command(home))
    return endpoint_problem(cfg)


def resolve_binaries(dsh, node):
    """Real paths of dsh's entry script and node: fnm puts both behind per-shell symlink dirs."""
    return (os.path.realpath(dsh) if dsh else None), (os.path.realpath(node) if node else None)
