"""Relay parameters: block `relay` of fleetmux/config/fleet.yaml.

The shipped block holds every value; the code has none. A private block (the operator's fleet.yaml) is laid over
the shipped one key by key, and `hot` and `coordinator` key by key. Hot keys are read through a HotStore on every
use: a dict in tests and one-shot commands, the root fleet's Redis hash `fx:relay:params` in the running relay. A
hot value that breaks a rule is not used; the last good value stays. `write_back` puts changed good hot values into the private
file only (never the shipped one)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

import yaml

from .errors import ConfigError
from .refs import check_node

HOT_KEYS = ("beat_s", "lost_after_s", "snapshot_s", "poll_s", "git_timeout_s", "max_tasks_per_node",
            "max_heavy_per_node", "disk_floor_heavy_gb", "disk_floor_any_gb", "score", "offline_grace_s",
            "claim_stagger_s", "wake_gap_s", "settle_s", "budget_pause_gap_s", "write_back_seconds")
COLD_KEYS = ("enabled", "state_remote", "snapshot_timeout_s", "takeover_md_max_lines", "text_max", "secret_globs",
             "coordinator", "probe_timeout_s", "notify_max", "reason_max")
COORDINATOR_KEYS = ("enabled", "prefer", "takeover")
SCORE_KEYS = ("cores", "mem_gb", "disk_free_gb")
REDIS_KEY = "fx:relay:params"

_POSITIVE = {"beat_s", "lost_after_s", "snapshot_s", "poll_s", "git_timeout_s", "wake_gap_s",
             "budget_pause_gap_s", "write_back_seconds", "snapshot_timeout_s", "probe_timeout_s"}
_NONNEG = {"offline_grace_s", "claim_stagger_s", "settle_s", "disk_floor_heavy_gb", "disk_floor_any_gb"}
_COUNT = {"max_tasks_per_node", "max_heavy_per_node", "takeover_md_max_lines", "text_max", "notify_max",
          "reason_max"}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def value_problem(key: str, v) -> str | None:
    """Why `v` is not a valid value of `key` alone, or None."""
    if key in _POSITIVE and not (_number(v) and v > 0):
        return f"relay.{key} must be a number > 0, got {v!r}"
    if key in _NONNEG and not (_number(v) and v >= 0):
        return f"relay.{key} must be a number >= 0, got {v!r}"
    if key in _COUNT and not (isinstance(v, int) and not isinstance(v, bool) and v >= 1):
        return f"relay.{key} must be an integer >= 1, got {v!r}"
    if key == "score":
        if not isinstance(v, dict) or set(v) != set(SCORE_KEYS) or not all(_number(x) for x in v.values()):
            return f"relay.score must map exactly {', '.join(SCORE_KEYS)} to numbers, got {v!r}"
    if key == "enabled" and not isinstance(v, bool):
        return f"relay.enabled must be true or false, got {v!r}"
    if key == "state_remote" and not isinstance(v, str):
        return f"relay.state_remote must be a string, got {v!r}"
    if key == "coordinator":
        return _coordinator_problem(v)
    if key == "secret_globs" and not (isinstance(v, list) and all(isinstance(x, str) and x for x in v)):
        return f"relay.secret_globs must be a list of file name patterns, got {v!r}"
    return None


def _coordinator_problem(v) -> str | None:
    if not isinstance(v, dict):
        return f"relay.coordinator must be a mapping of {', '.join(COORDINATOR_KEYS)}, got {v!r}"
    extra = sorted(set(v) - set(COORDINATOR_KEYS))
    if extra:
        return f"relay.coordinator.{extra[0]} is not a coordinator key"
    for key in COORDINATOR_KEYS:
        if key not in v:
            return f"relay.coordinator.{key} is missing"
    for key in ("enabled", "takeover"):
        if not isinstance(v[key], bool):
            return f"relay.coordinator.{key} must be true or false, got {v[key]!r}"
    prefer = v["prefer"]
    if not isinstance(prefer, str):
        return f"relay.coordinator.prefer must be a node name or \"\", got {prefer!r}"
    if prefer:
        try:
            check_node(prefer)
        except ConfigError as exc:
            return f"relay.coordinator.prefer: {exc}"
    return None


def relation_problems(hot: dict) -> list:
    """Rules between hot values."""
    out = []
    if hot["disk_floor_any_gb"] > hot["disk_floor_heavy_gb"]:
        out.append("relay.disk_floor_any_gb must not exceed relay.disk_floor_heavy_gb")
    if hot["lost_after_s"] <= hot["beat_s"] + hot["git_timeout_s"]:
        out.append("relay.lost_after_s must exceed beat_s + git_timeout_s (one beat may take a whole git timeout)")
    if hot["poll_s"] >= hot["lost_after_s"]:
        out.append("relay.poll_s must be below relay.lost_after_s")
    return out


def check_block(block) -> list:
    """Problems of a whole relay block; [] = valid."""
    if not isinstance(block, dict):
        return ["relay block must be a mapping"]
    problems = []
    hot = block.get("hot")
    if not isinstance(hot, dict):
        return ["relay.hot must be a mapping"]
    for key in sorted(set(block) - set(COLD_KEYS) - {"hot"}):
        problems.append(f"relay.{key} is not a relay key")
    for key in sorted(set(hot) - set(HOT_KEYS)):
        problems.append(f"relay.hot.{key} is not a relay hot key")
    for key in COLD_KEYS:
        if key not in block:
            problems.append(f"relay.{key} is missing")
        else:
            p = value_problem(key, block[key])
            if p:
                problems.append(p)
    for key in HOT_KEYS:
        if key not in hot:
            problems.append(f"relay.hot.{key} is missing")
        else:
            p = value_problem(key, hot[key])
            if p:
                problems.append(p)
    if not problems:
        problems += relation_problems(hot)
    return problems


def overlay(shipped: dict, private: dict | None) -> dict:
    """`private` laid over `shipped` key by key (and `hot` and `coordinator` key by key)."""
    out = copy.deepcopy(shipped or {})
    if not private:
        return out
    if not isinstance(private, dict):
        raise ConfigError("the private relay block must be a mapping")
    for key, value in private.items():
        if key in ("hot", "coordinator") and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(copy.deepcopy(value))
        else:
            out[key] = copy.deepcopy(value)
    return out


def file_block() -> dict:
    """The shipped relay block with the private one laid over it."""
    from fleetmux import fleet_config
    try:
        shipped = fleet_config.block("relay", fleet_config.SHIPPED)
        private = None
        user = fleet_config.user_path()
        if user is not None and user.is_file():
            data = fleet_config._read(user)
            private = data.get("relay") if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read the relay block: {exc}") from exc
    return overlay(shipped, private)


class HotStore(Protocol):
    def get(self, key: str): ...

    def set(self, key: str, value) -> None: ...


class DictHot:
    """Hot values in memory."""

    def __init__(self, values: dict):
        self.values = copy.deepcopy(dict(values))

    def get(self, key: str):
        return copy.deepcopy(self.values[key])

    def set(self, key: str, value) -> None:
        self.values[key] = copy.deepcopy(value)


class RedisHot:
    """Hot values in a Redis hash, JSON-encoded (the running relay; written by `seed` at start)."""

    def __init__(self, r, key: str = REDIS_KEY):
        self.r = r
        self.key = key

    def get(self, key: str):
        raw = self.r.hget(self.key, key)
        if raw is None:
            raise KeyError(key)
        return json.loads(raw)

    def set(self, key: str, value) -> None:
        self.r.hset(self.key, mapping={key: json.dumps(value)})


def seed(hot: HotStore, block: dict) -> None:
    """Copy the block's hot values into the store."""
    for key in HOT_KEYS:
        hot.set(key, block["hot"][key])


def changed(hot: HotStore, block: dict) -> dict:
    """Hot keys whose store value differs from the block (what a write-back would write)."""
    out = {}
    for key in HOT_KEYS:
        try:
            v = hot.get(key)
        except KeyError:
            continue
        if v != block["hot"][key]:
            out[key] = v
    return out


def write_back(hot: HotStore, block: dict, *, path=None) -> dict:
    """Write the hot values that differ from `block` (and break no rule) into the private file's `relay.hot`
    (default: the private file this process reads; created when absent). Never the shipped fleet.yaml. Returns
    what was written ({} = nothing, file untouched)."""
    from fleetmux import fleet_config
    target = fleet_config.user_path() if path is None else Path(path)
    if target is None:
        return {}
    target = Path(target).expanduser()
    if target.resolve() == Path(fleet_config.SHIPPED).resolve():
        raise ConfigError("hot values are written back only to the private fleet.yaml, never the shipped one")
    good = dict(block["hot"])
    out = {}
    for key, value in changed(hot, block).items():
        if value_problem(key, value) is None and not relation_problems(dict(good, **{key: value})):
            good[key] = value
            out[key] = value
    if not out:
        return {}
    try:
        with fleet_config.user_file_lock(target):  # the decompose write-back takes the same lock
            return _write_hot(target, out)
    except ConfigError:
        raise
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot write relay hot values back to {target}: {exc}") from exc


def _write_hot(target, out):
    """write_back under the private file's lock: re-read the file, merge ``out`` into relay.hot, replace it."""
    data = yaml.safe_load(target.read_text()) if target.is_file() else None
    data = data if isinstance(data, dict) else {}
    relay = data.setdefault("relay", {})
    if not isinstance(relay, dict):
        raise ConfigError(f"{target}: relay must be a mapping")
    relay_hot = relay.setdefault("hot", {})
    if not isinstance(relay_hot, dict):
        raise ConfigError(f"{target}: relay.hot must be a mapping")
    relay_hot.update(copy.deepcopy(out))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    os.replace(tmp, target)
    return out


class Settings:
    """The validated relay block. Cold keys are attributes; hot keys are read with `hot(key)` on every use."""

    def __init__(self, block: dict, store: HotStore):
        self.block = copy.deepcopy(block)
        for key in COLD_KEYS:
            setattr(self, key, copy.deepcopy(block[key]))
        self.store = store
        self._good = copy.deepcopy(block["hot"])

    def hot(self, key: str):
        if key not in HOT_KEYS:
            raise KeyError(key)
        try:
            v = self.store.get(key)
        except KeyError:
            return copy.deepcopy(self._good[key])
        if value_problem(key, v) is None:
            trial = dict(self._good, **{key: v})
            if not relation_problems(trial):
                self._good[key] = copy.deepcopy(v)
        return copy.deepcopy(self._good[key])

    def score_weights(self) -> dict:
        return dict(self.hot("score"))


def load(block: dict | None = None, *, hot: HotStore | None = None) -> Settings:
    """Validated Settings from `block` (default: the configuration files). ConfigError on any problem."""
    block = file_block() if block is None else block
    problems = check_block(block)
    if problems:
        raise ConfigError("; ".join(problems))
    return Settings(block, hot if hot is not None else DictHot(block["hot"]))
