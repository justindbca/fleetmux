"""The opening summary of the decomposition tree (docs/design.md §62): sent once, on the tick after the first tree is
accepted, through the routes a held slot's notice takes: a managed root's lifecycle channels, a root's own channels,
a sub-fleet's notice file that its parents pass up. Tests inject the notifier; nothing leaves the machine."""
import json

from fleetmux import fleet, notify_route
from test_decompose_fleet import hand_in_tree
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed  # noqa: F401


def managed(f, sent):
    f.cfg['lifecycle'] = 'dev_task'  # lifecycle_close.managed: a v2 root with a lifecycle
    f.notifier = lambda rec: sent.append(rec) or 'sent'


def test_a_managed_root_sends_the_summary_once_through_its_lifecycle_channels(mixed, decompose_on):
    f, sent = mixed, []
    hand_in_tree(f)
    managed(f, sent)
    f.tree_summary_tick()
    f.tree_summary_tick()
    texts = {r['text'] for r in sent if r['event'] == 'decompose_summary'}
    assert len(texts) == 1
    text, = texts
    assert 'Longest chain: M0 -> M1 -> M3 (45 min); max parallel width 2.' in text
    assert '- M1 implement the check (25 min, longest chain)' in text
    assert 'D2 cancel or just not send: just not send [your call, conservative pick]' in text
    assert json.loads(f.r.get('fx:tree:summary'))['sent_at'] > 0


def test_a_root_without_a_lifecycle_uses_its_own_channels(mixed, decompose_on, monkeypatch):
    f, sent = mixed, []
    monkeypatch.setattr(notify_route, 'unmanaged_send', lambda fl, event, text: sent.append((event, text)) or 'sent')
    hand_in_tree(f)
    f.tree_summary_tick()
    assert [e for e, _ in sent] == ['decompose_summary']


def test_a_sub_fleet_passes_its_summary_up_and_the_top_sends_it(mixed, decompose_on, tmp_path):
    f, sent = mixed, []
    hand_in_tree(f)
    parent_run = tmp_path / 'parent-run'
    parent_run.mkdir()
    fleet.save(tmp_path / 'parent.json', {'run': str(parent_run)})
    f.cfg.update(parent_config=str(tmp_path / 'parent.json'), task_id='M1')
    f.tree_summary_tick()
    note, = (parent_run / 'subfleet-notices').glob('tree-*.json')
    assert fleet.load(note)['kind'] == 'tree' and 'Longest chain' in fleet.load(note)['text']
    f.cfg.pop('parent_config')  # the same fleet now plays the top: it receives that notice
    managed(f, sent)
    (f.run_dir / 'subfleet-notices').mkdir(exist_ok=True)
    note.rename(f.run_dir / 'subfleet-notices' / note.name)
    f.relay_notices()
    assert [r['text'] for r in sent if r['event'] == 'decompose_summary'][0].startswith('[fleet ')
    assert '[sub-fleet M1] Decomposition of' in sent[0]['text']
