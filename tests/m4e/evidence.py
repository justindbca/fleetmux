"""Evidence of one m4e run, written under ``<world>/ev/`` and never removed (e2e-1 plan r3 §5, §7).

``step(n, name, data)`` -> ``ev/NN-name.json``; ``files()`` lists the world tree (path, size, inode) so the run
can record what the product created, removed or moved (``lifecycle_changes``: evidence, not a gate, plan §7.2);
``manifest()`` writes ``ev/MANIFEST.json`` with the directories, sizes and a suggested manual removal command as
text only (the test never runs it).
"""
import json
import os
import subprocess
import time
from pathlib import Path

ATTRIBUTION = (  # (path test, who, design rule) for lifecycle_changes (plan §7.2 table)
    (lambda p: p.endswith(('ctl.sock', 'server.json', 'panes.json')), 'fleetmux server.shutdown / end_server',
     'PROTOCOL §4.16'),
    (lambda p: p.endswith(('redis.sock', 'redis.pid')), 'Redis shutdown', 'design §13'),
    (lambda p: p.endswith('.fleetmux.json'), 'subfleet.collect (child registry)', 'plan F14'),
    (lambda p: '/archive/' in p, 'archive_run', 'plan F14'),
    (lambda p: p.endswith('tmux.sock'), 'stop_tmux', 'subfleet.stop_tmux'),
    (lambda p: '/.claude/agents' in p or '/.claude/skills' in p, 'kit.install / link_domains', 'plan F23'),
    (lambda p: '.fleet-settings-g' in p, 'claude_argv', 'plan F24'),
    (lambda p: 'claude-input-' in p, 'claude_input.send', 'plan F19'),
    (lambda p: '/handoffs/' in p or '/.handoff/' in p, 'handoff note', 'design §29'),
)


class Evidence:
    def __init__(self, world):
        self.world = world
        self.dir = world.ev
        self.log = []

    def step(self, n, name, data):
        path = self.dir / ('%02d-%s.json' % (n, name))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str) + '\n', encoding='utf-8')
        self.log.append({'step': n, 'name': name, 'file': str(path), 'at': time.time()})
        return path

    def files(self):
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.world.dir):
            for name in dirnames + filenames:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                out[p] = {'size': st.st_size, 'inode': st.st_ino, 'dir': name in dirnames}
        return out

    def lifecycle_changes(self, before, after):
        by_inode = {v['inode']: p for p, v in after.items()}
        changes = []
        for p, v in before.items():
            if p in after:
                continue
            moved = by_inode.get(v['inode'])
            kind = 'moved' if moved and moved not in before else 'gone'
            who = next(((w, rule) for test, w, rule in ATTRIBUTION if test(p)), ('unattributed', ''))
            changes.append({'path': p, 'change': kind, 'to': moved if kind == 'moved' else None,
                            'by': who[0], 'rule': who[1]})
        added = sorted(p for p in after if p not in before)
        return {'removed_or_moved': changes, 'added_count': len(added), 'added_sample': added[:200]}

    def tails(self, lines=200):
        """Last lines of the product logs of the world, copied into ev/ (on failure)."""
        out = {}
        for pattern in ('**/server.log', '**/daemon.log', '**/lifecycle-close.log', '**/redis.log'):
            for path in self.world.dir.glob(pattern):
                if self.dir in path.parents:
                    continue
                try:
                    out[str(path)] = path.read_text(encoding='utf-8', errors='replace').splitlines()[-lines:]
                except OSError:
                    pass
        self.step(99, 'log-tails', out)
        return out

    def manifest(self, result):
        du = subprocess.run(['du', '-sk', str(self.world.dir)], capture_output=True, text=True, timeout=60).stdout
        data = {'world': str(self.world.dir), 'id': self.world.id, 'created': self.world.created,
                'marker': self.world.marker, 'result': result,
                'size_kb': du.split()[0] if du else None, 'steps': self.log,
                'kept': 'the test never removes this directory',
                'manual_removal_suggestion_text_only': 'rm -r ' + str(self.world.dir)}
        (self.dir / 'MANIFEST.json').write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        return data
