"""Resolve a card to an immutable worker launch description (never a slot id)."""
import json
import re

from cmux_fleet.profile_runtime import ProfileRuntimeError


def resolve_launch(card, registry, cfg, extras=None):
    if card.cli not in ('pi', 'claude', 'codex', 'opencode'):
        raise ProfileRuntimeError('unsupported profile CLI: ' + card.cli)
    if card.profile and card.cli != 'codex':
        raise ProfileRuntimeError('profile launch field is supported only by Codex; use model for ' + card.cli)
    overrides = {'worker_cli': card.cli}
    if card.cli == 'pi':
        overrides['pi_model'] = card.model or cfg.get('pi_model')
        if not overrides['pi_model']:
            raise ProfileRuntimeError('pi profile requires a model or configured pi_model')
    elif card.cli == 'claude':
        overrides['worker_model'] = card.model or cfg.get('worker_model') or 'sonnet'
    elif card.cli == 'opencode':
        # The model name comes from configuration (card or --opencode-model), never a code default.
        overrides['opencode_model'] = card.model or cfg.get('opencode_model')
        if not overrides['opencode_model']:
            raise ProfileRuntimeError('opencode profile requires a model or configured opencode_model')
    else:
        profile = card.profile or cfg.get('worker_profile') or cfg.get('profile') or 'fleet'
        if not re.fullmatch(r'[A-Za-z0-9_-]+', profile):
            raise ProfileRuntimeError('Codex profile must be a simple config profile name')
        overrides['worker_profile'] = profile
        overrides['codex_model'] = card.model or cfg.get('codex_model')
    role_text = ''
    if card.role_file:
        root = registry.directory.resolve()
        path = (root / card.role_file).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ProfileRuntimeError('profile role_file must exist inside its registry: ' + card.role_file)
        role_text = path.read_text()
    launch = {'profile_id': card.id, 'overrides': overrides, 'role_text': role_text}
    if extras:  # advisor launch fields (e.g. Claude --effort); absent keys keep the old encoding byte-identical
        launch['extras'] = dict(extras)
    return launch


def encode_launch(launch):
    return json.dumps(launch, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
