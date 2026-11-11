"""Transport v2 supervisor/worker CLI pair table (config/fleet.yaml#cli_pairs).

Every check re-reads the table, so an edit takes effect at the next start. A pair the
table does not list is refused with its reason, never silently replaced by another
CLI/model or by the old file mailbox.
"""
from pathlib import Path

import yaml

from fleetmux import fleet_config

DEFAULT_PATH = None  # None = the cli_pairs block of fleet.yaml; a path = that file (blocks or a bare table)
CLIS = ('pi', 'codex', 'claude')  # supervisor CLIs
# opencode runs only as a worker (free OpenCode Zen model through the opencode client itself; brief
# fleet-opencode-worker-20261016): never a supervisor, so it is not in CLIS. dsh (DeepSeek Harness, design §41)
# is worker-only too.
WORKER_CLIS = CLIS + ('opencode', 'dsh')
MODES = ('task', 'service')
STATUSES = ('verified', 'unverified', 'forbidden')
SUPPORTED = '(supported: all-Pi, or Codex supervisor + Claude workers)'
DEFAULT_KEYS = ('supervisor_cli', 'worker_cli', 'mode', 'supervisor_profile',
                'supervisor_pi_model', 'supervisor_pi_thinking')
THINKING = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')  # = pi_adapter.THINKING
# Old Codex supervisor profiles a start may still name while the default supervisor is pi:
# mapped to the same model on pi (never a silent official Codex TUI). Others are refused.
LEGACY_SUPERVISOR_PROFILES = {'fleet-astra': ('openai-codex/gpt-6-astra', 'high')}


class PairTable:
    def __init__(self, data, path=None):
        self.path = path
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError(f'{path}: cli pair table needs version: 1')
        self.default = dict(data.get('default') or {})
        unknown = sorted(set(self.default) - set(DEFAULT_KEYS))
        if unknown:
            raise ValueError(f'{path}: unknown default key(s) {unknown}; allowed: {", ".join(DEFAULT_KEYS)}')
        if not isinstance(self.default.get('supervisor_profile', ''), str):
            raise ValueError(f'{path}: default.supervisor_profile must be a Codex profile name')
        model = self.default.get('supervisor_pi_model')
        if model is not None and (not isinstance(model, str) or '/' not in model.strip('/')):
            raise ValueError(f'{path}: default.supervisor_pi_model must be a pi provider/model string')
        if self.default.get('supervisor_pi_thinking') is False:
            self.default['supervisor_pi_thinking'] = 'off'  # YAML 1.1 reads a bare `off` as false
        if self.default.get('supervisor_pi_thinking', 'off') not in THINKING:
            raise ValueError(f'{path}: default.supervisor_pi_thinking must be one of {THINKING}')
        if (model or 'supervisor_pi_thinking' in self.default) and self.default.get('supervisor_cli') != 'pi':
            raise ValueError(f'{path}: default.supervisor_pi_* needs default.supervisor_cli: pi')
        if self.default.get('supervisor_profile') and self.default.get('supervisor_cli') != 'codex':
            raise ValueError(f'{path}: default.supervisor_profile is a Codex profile; it needs default.supervisor_cli: codex')
        self.rows = {}
        for row in data.get('pairs') or []:
            key = (row.get('supervisor_cli'), row.get('worker_cli'), row.get('mode'))
            if key[0] not in CLIS or key[1] not in WORKER_CLIS or key[2] not in MODES:
                raise ValueError(f'{path}: bad cli pair row {key}')
            if row.get('status') not in STATUSES:
                raise ValueError(f'{path}: {key} status must be one of {STATUSES}')
            if not isinstance(row.get('adapter_missing', []), list):
                raise ValueError(f'{path}: {key} adapter_missing must be a list')
            if key in self.rows:
                raise ValueError(f'{path}: duplicate cli pair row {key}')
            self.rows[key] = dict(row, adapter_missing=list(row.get('adapter_missing') or []))
        if self.default and self.lookup(self.default.get('supervisor_cli'), self.default.get('worker_cli'),
                                        self.default.get('mode')) is None:
            raise ValueError(f'{path}: default pair {self.default} is not in the table')

    def lookup(self, supervisor_cli, worker_cli, mode):
        return self.rows.get((supervisor_cli, worker_cli, mode))


def load_pairs(path=None):
    path = path or DEFAULT_PATH
    return PairTable(fleet_config.block('cli_pairs', path), fleet_config.source(path))


def legacy_supervisor_profile(profile):
    """(pi model, thinking) an old Codex supervisor profile maps to; ValueError with the new usage otherwise."""
    if profile in LEGACY_SUPERVISOR_PROFILES:
        return LEGACY_SUPERVISOR_PROFILES[profile]
    raise ValueError(
        f'--supervisor-profile/--profile {profile!r} names a Codex profile, but the default supervisor is now pi. '
        f'Use --supervisor-cli codex --supervisor-profile {profile} for the official Codex TUI, or '
        f'--supervisor-pi-model provider/model [--supervisor-pi-thinking LEVEL] for a pi supervisor '
        f'(mapped old profiles: {", ".join(sorted(LEGACY_SUPERVISOR_PROFILES))})')


def _unlisted_reason(sup, wrk, mode):
    if sup not in CLIS:
        return f'unknown supervisor CLI {sup!r}: the supervisor runs one of {", ".join(CLIS)} (opencode is worker-only, so is dsh)'
    if wrk not in WORKER_CLIS:
        return f'unknown worker CLI {wrk!r}: workers run one of {", ".join(WORKER_CLIS)}'
    if wrk in ('opencode', 'dsh'):
        return f'transport v2 {sup} supervisor + {wrk} workers in {mode} mode is not in the cli pair table'
    if sup == 'claude':
        return f'transport v2 has no Claude supervisor adapter {SUPPORTED}'
    if (sup, wrk) == ('codex', 'codex'):
        return f'transport v2 Codex workers are unvalidated {SUPPORTED}'
    if 'pi' in (sup, wrk) and sup != wrk:
        other = 'Codex' if 'codex' in (sup, wrk) else 'Claude'
        return f'transport v2 mixed Pi/{other} fleets are unvalidated {SUPPORTED}'
    return f'transport v2 {sup} supervisor + {wrk} workers in {mode} mode is not in the cli pair table {SUPPORTED}'


def pair_problem(cfg, table=None):
    """None when this supervisor/worker CLI pair and mode may run transport v2."""
    sup, wrk = cfg.get('supervisor_cli') or 'codex', cfg.get('worker_cli') or 'codex'
    mode = cfg.get('mode') or 'task'
    if (sup, wrk) == ('claude', 'claude') and mode in ('task', 'service') and cfg.get('adopt'):
        # Sub-fleet (docs/design.md §19; service: a named sub-fleet, §29): the supervisor is an
        # adopted official-Claude worker pane.
        return None
    table = table or load_pairs()
    row = table.lookup(sup, wrk, mode)
    if row is None or row['status'] == 'forbidden':
        return (row or {}).get('reason') or _unlisted_reason(sup, wrk, mode)
    if row['adapter_missing']:
        return (row.get('reason') or _unlisted_reason(sup, wrk, mode)) + \
            f' [adapter missing: {", ".join(row["adapter_missing"])}]'
    if row['status'] == 'unverified':
        if cfg.get('lifecycle') == 'smoke':
            return None
        return (row.get('reason') or _unlisted_reason(sup, wrk, mode)) + \
            '; an unverified pair may run only with --lifecycle smoke'
    return None
