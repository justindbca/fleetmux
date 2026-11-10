"""Test stub of feature A's cmux_fleet.cli_pairs (A-interface.md v1.2, feat/cli-pairs @ ac06eec6).

Only used while A is not merged: the night-switch tests install it as cmux_fleet.cli_pairs when the
real module is missing, and use the real module otherwise, so the same tests check both.
Contract copied: load_pairs(path=None) re-reads DEFAULT_PATH each call; PairTable.lookup returns the
row (adapter_missing always a list) or None; pair_problem(cfg, table=None) applies, in order: adopt
claude+claude/task -> allowed; unlisted or forbidden -> refused; adapter_missing -> refused even
under smoke; unverified -> allowed only with lifecycle 'smoke'; verified -> allowed.
"""
from pathlib import Path

import yaml

DEFAULT_PATH = None  # tests point this at a tmp table
SUPPORTED = '(supported: all-Pi, or Codex supervisor + Claude workers)'


class PairTable:
    def __init__(self, data, path=None):
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError(f'{path}: cli pair table needs version: 1')
        self.default = dict(data.get('default') or {})
        self.rows = {}
        for row in data.get('pairs') or []:
            key = (row['supervisor_cli'], row['worker_cli'], row['mode'])
            if key in self.rows:
                raise ValueError(f'{path}: duplicate cli pair row {key}')
            self.rows[key] = dict(row, adapter_missing=list(row.get('adapter_missing') or []))

    def lookup(self, supervisor_cli, worker_cli, mode):
        return self.rows.get((supervisor_cli, worker_cli, mode))


def load_pairs(path=None):
    path = Path(path or DEFAULT_PATH)
    return PairTable(yaml.safe_load(path.read_text()), path)


def _unlisted_reason(sup, wrk, mode):
    if sup == 'claude':
        return f'transport v2 has no Claude supervisor adapter {SUPPORTED}'
    if 'pi' in (sup, wrk) and sup != wrk:
        return f'transport v2 mixed Pi/{"Codex" if "codex" in (sup, wrk) else "Claude"} fleets are unvalidated {SUPPORTED}'
    return f'transport v2 {sup} supervisor + {wrk} workers in {mode} mode is not in the cli pair table {SUPPORTED}'


def pair_problem(cfg, table=None):
    sup, wrk = cfg.get('supervisor_cli') or 'codex', cfg.get('worker_cli') or 'codex'
    mode = cfg.get('mode') or 'task'
    if (sup, wrk, mode) == ('claude', 'claude', 'task') and cfg.get('adopt'):
        return None
    row = (table or load_pairs()).lookup(sup, wrk, mode)
    reason = (row or {}).get('reason') or _unlisted_reason(sup, wrk, mode)
    if row is None or row['status'] == 'forbidden':
        return reason
    if row['adapter_missing']:
        return reason + f' [adapter missing: {", ".join(row["adapter_missing"])}]'
    if row['status'] == 'unverified':
        return None if cfg.get('lifecycle') == 'smoke' else reason + '; an unverified pair may run only with --lifecycle smoke'
    return None
