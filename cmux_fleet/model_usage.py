"""Which model every worker slot runs now, and the card limits checked before a slot takes a card.

Counting rules (docs/design.md §12 模型特点卡; interface for the night switch and the brake):
- The unit is the *model*: a profile card counts under its ``model_card`` (claude-coder and
  claude-reviewer both count as ``opus``); a card without ``model_card`` counts under its own id.
- One slot counts once, whatever its generation: a handoff successor or a profile switch inside
  the same slot never doubles the count (same-slot reuse is free).
- A slot runs the card frozen in its ``runtime_launch`` (set before the successor is spawned, so a
  switch decided earlier in the same tick is already counted); without one it runs the fleet
  default, mapped to a model card only by an exact (cli, model string) match. No match = unmapped,
  never guessed, never counted.
- An old generation holds its model until its tab is confirmed gone: during a profile switch the
  slot holds the old model (``retired_pane`` + ``retired_launch``) and the new one, so a switch never
  frees the old model's ticket early. A failed switch rolls ``runtime_launch`` back once the new tab
  is gone, so the failed generation holds nothing.
- Dead slots hold nothing, unless a tab of theirs is still alive (``alive`` callback). Retiring slots
  still hold their model until they are gone.
- The fleet default is capped like a card: ``default_problem`` refuses a start / roster / sub-fleet
  that puts more slots on the default model than its card allows; ``Fleet.card_gate(slot, None)``
  gates a switch back to the default.
- The supervisor is not counted (its Codex profile name does not say which model card it is).
"""
import json
from typing import Callable, Dict, Mapping, Optional, Tuple

from cmux_fleet.agent_registry import AgentCard, AgentRegistry
from cmux_fleet.pi_adapter import for_agent


def limit_key(card) -> str:
    """The id max_concurrent is counted under."""
    return getattr(card, 'model_card', None) or card.id


def default_model(cfg: Mapping, registry: AgentRegistry) -> Optional[str]:
    """Model card id of the fleet's default worker runtime, or None (unknown: not counted).
    Pass ``for_agent(cfg, slot)`` for one slot: a pi worker may have its own pi_model (roster)."""
    cli = cfg.get('worker_cli') or 'codex'
    model = {'pi': cfg.get('pi_model'), 'claude': cfg.get('worker_model'),
             'codex': cfg.get('codex_model'), 'opencode': cfg.get('opencode_model'),
             'dsh': cfg.get('dsh_model')}.get(cli)
    if not model:
        return None
    matches = [m.id for m in registry.models if m.cli == cli and m.model == model]
    return matches[0] if len(matches) == 1 else None


def slot_card(info: Mapping, cfg: Mapping, registry: AgentRegistry) -> Tuple[Optional[str], Optional[str]]:
    """(profile id or None, model key or None) for one worker slot's current generation."""
    try:
        launch = json.loads(info.get('runtime_launch') or '{}')
    except ValueError:
        launch = {}
    profile = launch.get('profile_id') or None
    if not profile:
        return None, default_model(cfg, registry)
    try:
        return profile, limit_key(registry.get(profile))
    except KeyError:
        return profile, None  # a card removed after spawn: unknown, not guessed


def retired_model(info: Mapping, cfg: Mapping, registry: AgentRegistry) -> Optional[str]:
    """Model key the slot's not-yet-retired old generation runs (``retired_launch``; a handoff that
    keeps the launch has none, so the current launch is used)."""
    launch = info['retired_launch'] if 'retired_launch' in info else info.get('runtime_launch')
    return slot_card({'runtime_launch': launch or ''}, cfg, registry)[1]


class Usage:
    """Snapshot {slot: (profile, model key)} of the live worker slots, plus {slot: model key} of old
    generations not yet confirmed gone (``retiring``). A slot counts once per model it holds."""

    def __init__(self, slots: Mapping[str, Tuple[Optional[str], Optional[str]]],
                 retiring: Optional[Mapping[str, str]] = None):
        self.slots = dict(slots)
        self.retiring = dict(retiring or {})

    @classmethod
    def from_agents(cls, agents: Mapping[str, Mapping], cfg: Mapping, registry: AgentRegistry,
                    alive: Optional[Callable[[str], bool]] = None) -> 'Usage':
        """``alive(pane)`` (None = not checked): a dead slot whose tab is still alive keeps its model,
        and an old generation's tab that is gone no longer holds one."""
        slots, retiring = {}, {}
        for slot, info in agents.items():
            pane = info.get('pane')
            if info.get('state') != 'dead' or (alive and pane and alive(pane)):
                slots[slot] = slot_card(info, for_agent(cfg, slot), registry)
            old = info.get('retired_pane')
            if old and (alive is None or alive(old)):
                key = retired_model(info, for_agent(cfg, slot), registry)
                if key:
                    retiring[slot] = key
        return cls(slots, retiring)

    def holders(self, key: str, exclude: Optional[str] = None):
        held = {slot for slot, (_, model) in self.slots.items() if model == key}
        held |= {slot for slot, model in self.retiring.items() if model == key}
        return sorted(held - {exclude})

    def counts(self) -> Dict[str, int]:
        keys = {model for _, model in self.slots.values() if model} | set(self.retiring.values())
        return {key: len(self.holders(key)) for key in keys}

    def for_planning(self, reusable) -> Dict[str, int]:
        """Router occupancy for a plan that may reuse ``reusable`` slots: a model already running on
        one of them can take the work there without a new slot, so that slot is not counted."""
        reusable = set(reusable)
        out = self.counts()
        for key in list(out):
            if any(self.slots[s][1] == key and self.retiring.get(s, key) == key
                   for s in reusable if s in self.slots):
                out[key] -= 1
        return out

    def admit(self, slot: str, card: AgentCard) -> Optional[str]:
        """None if ``slot`` may run ``card`` now; otherwise why not (wait for a slot to free up)."""
        if card.max_concurrent is None:
            return None
        key = limit_key(card)
        others = self.holders(key, exclude=slot)
        if len(others) + 1 > card.max_concurrent:
            return ('max_concurrent %d for model %s reached (running on %s)'
                    % (card.max_concurrent, key, ', '.join(others)))
        return None


def over_cap(usage: Usage, registry: AgentRegistry):
    """Models running on more slots than their model card's max_concurrent (fleet default included)."""
    out = []
    for key, n in sorted(usage.counts().items()):
        try:
            cap = registry.model(key).max_concurrent
        except KeyError:
            continue
        if cap is not None and n > cap:
            out.append('model %s runs on %d slots (%s), max_concurrent %d'
                       % (key, n, ', '.join(usage.holders(key)), cap))
    return out


def host_problem(card: AgentCard, host: Optional[str]) -> Optional[str]:
    """None if the card may run on ``host`` (unset host or card without hosts: no limit)."""
    from cmux_fleet.router import host_matches
    if host_matches(card, host):
        return None
    return 'profile %s runs only on %s, this fleet is on %s' % (card.id, ', '.join(card.hosts), host)


def default_card(cfg: Mapping, registry: AgentRegistry):
    """The model card of the fleet default worker runtime, or None (unmapped: not capped, not guessed)."""
    key = default_model(cfg, registry)
    return registry.model(key) if key else None


def default_problem(cfg: Mapping, slots, registry: AgentRegistry) -> Optional[str]:
    """None if ``slots`` (worker slot names) may all run the fleet default; else why not.

    The default runtime is capped by its model card like any card: a fleet may not start, add roster
    slots or delegate a sub-fleet onto more slots of one model than its max_concurrent (design v2 §3
    并发 = 本 fleet 票数; §4.13 M ≤ 算力闸票数), and never on a host the card excludes."""
    groups = {}  # card id -> (card, slots); a slot's own pi_model picks its card
    for slot in slots:
        card = default_card(for_agent(cfg, slot), registry)
        if card is not None:
            groups.setdefault(card.id, (card, []))[1].append(slot)
    for card, group in groups.values():
        problem = host_problem(card, cfg.get('host') or None)
        if problem:
            return 'fleet default worker model: ' + problem
        if card.max_concurrent is not None and len(group) > card.max_concurrent:
            return ('fleet default worker model %s: %d worker slots (%s) exceed max_concurrent %d'
                    % (card.id, len(group), ', '.join(group), card.max_concurrent))
    return None


def check_slots(cfg: Mapping, workers, profiles: Mapping[str, str], registry: AgentRegistry):
    """Errors for a whole roster: ``profiles`` {slot: card id}, other slots run the fleet default.

    Each slot holds one model; per model, the slots must fit the smallest max_concurrent of the cards
    on them. Default (profile-less) slots count when their model maps, and the default model's own
    card caps them too (no bypass through the default). Host limits apply to every card."""
    errors, slots, caps = [], {}, {}
    host = cfg.get('host') or None
    defaults = {slot: default_model(for_agent(cfg, slot), registry) for slot in workers if not profiles.get(slot)}
    for default in dict.fromkeys(d for d in defaults.values() if d):
        card = registry.model(default)
        problem = host_problem(card, host)
        if problem:
            errors.append('fleet default worker model: ' + problem)
        if card.max_concurrent is not None:
            caps.setdefault(default, []).append((None, card.max_concurrent))
    for slot in workers:
        profile = profiles.get(slot)
        if not profile:
            slots[slot] = (None, defaults[slot])
            continue
        try:
            card = registry.get(profile)
        except KeyError:
            errors.append(slot + ': unknown agent profile ' + profile)
            continue
        problem = host_problem(card, host)
        if problem:
            errors.append(slot + ': ' + problem)
        slots[slot] = (profile, limit_key(card))
        if card.max_concurrent is not None:
            caps.setdefault(limit_key(card), []).append((slot, card.max_concurrent))
    usage = Usage(slots)
    for key, entries in caps.items():
        cap = min(n for _, n in entries)
        holders = usage.holders(key)
        if len(holders) > cap:
            errors.append('model %s: %d slots (%s) exceed max_concurrent %d'
                          % (key, len(holders), ', '.join(holders), cap))
    return errors
