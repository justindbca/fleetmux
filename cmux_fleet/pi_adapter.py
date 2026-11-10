"""Pi launch/configuration helpers. No model endpoints or credentials are embedded here."""
import json
from pathlib import Path


UNSANDBOXED_SCOPE = ('You run without an OS sandbox. The directory restrictions above are instructions, '
                     'not an isolation boundary. Do not drive the user\'s browser directly; use only '
                     'the broker command below if the operator enabled it.')
THINKING = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')


def for_agent(cfg, name, overrides=None):
    """cfg as seen by one agent: its own pi model / thinking (config ``pi_models`` /
    ``pi_thinkings``, from --supervisor-pi-model or a roster row) over the run-wide
    ``pi_model`` / ``pi_thinking`` fallback; profile-launch ``overrides`` win over both.
    Idempotent, so a saved per-generation snapshot resolves to the same values again."""
    out, overrides = dict(cfg), overrides or {}
    for key, table in (('pi_model', 'pi_models'), ('pi_thinking', 'pi_thinkings')):
        own = (cfg.get(table) or {}).get(name)
        if own:
            out[key] = own
        if key in overrides and own:  # a card's own value replaces this slot's entry for good
            out[table] = {k: v for k, v in cfg[table].items() if k != name}
    out.update(overrides)
    return out


def context_models(data, pi_context):
    """models.json ``data`` with fleet.yaml#pi_context applied (a new dict; ``data`` is not changed)."""
    data = json.loads(json.dumps(data or {}))
    default = (pi_context or {}).get('default_window')
    per_model = dict((pi_context or {}).get('per_model') or {})
    for value in [default] + list(per_model.values()):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError('pi_context windows must be positive integers')
    providers = data.setdefault('providers', {})
    for provider, entry in providers.items():
        for model in entry.get('models') or []:
            key = provider + '/' + str(model.get('id'))
            if key in per_model:
                model['contextWindow'] = per_model.pop(key)
            elif default and 'contextWindow' not in model:
                model['contextWindow'] = default
    for key, window in per_model.items():  # built-in / extension models: providers.<p>.modelOverrides
        provider, sep, model_id = key.partition('/')
        if not sep or not provider or not model_id:
            raise ValueError('pi_context per_model keys are <provider>/<model id>: ' + key)
        override = providers.setdefault(provider, {}).setdefault('modelOverrides', {}).setdefault(model_id, {})
        override['contextWindow'] = window
    return data


def prepare_home(home, source, pi_context=None):
    """A fresh per-run Pi home: share only authentication and model definitions.

    Do not copy the operator's settings, extensions, MCP, skills or context files.
    A new directory is required so stale executable configuration cannot leak in.
    With fleet.yaml#pi_context set, models.json is written as a copy with its windows applied.
    """
    home, source = Path(home), Path(source).expanduser()
    home.mkdir(parents=True, exist_ok=False)
    applies = bool(pi_context) and (pi_context.get('default_window') is not None or bool(pi_context.get('per_model')))
    for name in ('auth.json', 'models.json'):
        src = source / name
        if name == 'models.json' and applies:
            data = json.loads(src.read_text()) if src.is_file() else {}
            (home / name).write_text(json.dumps(context_models(data, pi_context), ensure_ascii=False, indent=2) + '\n')
        elif src.is_file():
            (home / name).symlink_to(src.resolve())
    (home / 'settings.json').write_text(json.dumps({
        'quietStartup': True, 'compaction': {'enabled': True},
    }) + '\n')
    return home


def session_dir(cfg, name, generation):
    return Path(cfg['run']) / 'pi-sessions' / name / ('g' + str(generation))


def argv(cfg, name, generation, prompt, cwd, *, label):
    if not cfg.get('allow_unsandboxed_pi'):
        raise ValueError('pi has no sandbox: --allow-unsandboxed-pi is required')
    if not cfg.get('pi_model'):
        raise ValueError('--pi-model provider/model is required for pi agents')
    # Use fleet's existing label verbatim; do not invent a second naming scheme in Pi.
    return [cfg.get('pi') or 'pi', '--model', cfg['pi_model'], '--name', label,
            '--thinking', cfg.get('pi_thinking') or 'off', '--tui-mode', 'regular',
            '--no-extensions', '--extension', str(Path(__file__).with_name(
                'pi_extension_v2.ts' if cfg.get('transport_version') == 2 else 'pi_extension.ts')),
            '--no-mcp', '--no-skills', '--no-prompt-templates', '--no-themes',
            '--no-context-files', '--no-approve', '--offline',
            '--append-system-prompt', str(Path(cwd) / 'AGENTS.md'),
            '--tools', 'read,bash,edit,write' + (',fleet_decision' if name == 'supervisor' else ',fleet_result')
                if cfg.get('transport_version') == 2 else 'read,bash,edit,write',
            '--session-dir', str(session_dir(cfg, name, generation)), '--', prompt]


DAEMON_WATCH = {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}


def daemon_watch():
    """§36 knobs of the supervisor's daemon watchdog: fleet.yaml#daemon_watch over the defaults, seconds > 0."""
    from cmux_fleet import fleet_config
    knobs = dict(DAEMON_WATCH, **(fleet_config.block('daemon_watch') or {}))
    if set(knobs) != set(DAEMON_WATCH) or any(type(v) not in (int, float) or v <= 0 for v in knobs.values()):
        raise ValueError('fleet.yaml#daemon_watch takes ' + ', '.join(DAEMON_WATCH) + ' as seconds > 0')
    return knobs


def environment(cfg_path, cfg, name, generation):
    if cfg.get('transport_version') == 2:
        env = {'PI_CODING_AGENT_DIR': str(cfg['pi_home']), 'PI_SKIP_VERSION_CHECK': '1',
               'CMUX_FLEET_PI_BRIDGE': json.dumps([cfg['python'],
                   str(Path(__file__).with_name('agent_bridge.py')), str(cfg_path), name, str(generation)]),
               'CMUX_FLEET_PI_SLOT': name, 'CMUX_FLEET_PI_MODE': cfg.get('mode', 'task')}
        if name == 'supervisor' and not cfg.get('parent_config'):  # a sub-fleet's daemon is its parent's (§29)
            env['CMUX_FLEET_DAEMON_WATCH'] = json.dumps(daemon_watch())
        return env
    return {'PI_CODING_AGENT_DIR': str(cfg['pi_home']),
            'CMUX_FLEET_PI_CALLBACK': json.dumps([
                cfg['python'], str(Path(__file__).with_name('fleet.py')),
                'pi-event', str(cfg_path), name, str(generation)]),
            'PI_SKIP_VERSION_CHECK': '1'}


def event(cfg, name, generation, data):
    """Validate the adapter wire format before anything reaches the private Redis."""
    if not isinstance(data, dict) or data.get('kind') not in ('pi_session', 'turn', 'operator_input'):
        raise ValueError('invalid pi event kind')
    transcript = data.get('transcript')
    if not isinstance(transcript, str) or not transcript:
        raise ValueError('pi event requires a persisted transcript')
    path = Path(transcript).resolve()
    if path.suffix != '.jsonl' or not path.is_relative_to(session_dir(cfg, name, generation).resolve()):
        raise ValueError('pi transcript must be inside this agent generation session directory')
    if not isinstance(data.get('thread'), str) or not data['thread']:
        raise ValueError('pi event requires a session id')
    for key in ('ctx_used', 'ctx_window'):
        value = data.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('invalid pi context usage')
    if data['kind'] == 'operator_input':
        if name != 'supervisor' or not isinstance(data.get('text'), str) or not 1 <= len(data['text'].strip()) <= 32000:
            raise ValueError('operator input requires supervisor and 1-32000 chars of text')
    if data['kind'] == 'turn':
        if not isinstance(data.get('text'), str) or len(data['text']) > 200000:
            raise ValueError('invalid pi reply text')
        if not isinstance(data.get('msg_id'), str):
            raise ValueError('pi turn requires a delivery id (empty only for READY)')
        if type(data.get('aborted')) is not bool or type(data.get('failed')) is not bool:
            raise ValueError('pi turn requires outcome flags')
    fields = ('kind', 'thread', 'text', 'transcript', 'msg_id', 'aborted', 'failed', 'ctx_used', 'ctx_window')
    return dict({k: data[k] for k in fields if k in data}, agent=name, generation=generation)
