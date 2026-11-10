"""Offline registry of agent profiles used by the dynamic routing MVP.

Agent profile ids describe capabilities and launch preferences.  They are not fleet
slot ids: assigning ``flashnext-coder`` to ``worker-1`` must not rename the slot.

Model cards (the ``models`` block of config/fleet.yaml, or ``<registry>/models/*.json``; docs/design.md §12) carry the per-model
assessment: quota pool, hosts, context window, handoff ratio, concurrency, roles,
judgement, night use, measured values.  They are data, never routing candidates; a
profile card links one with ``model_card`` and inherits every field it leaves unset.
"""

from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from cmux_fleet import fleet_config

CARD_SUFFIXES = (".json", ".yaml", ".yml")
MODELS_SUBDIR = "models"
# Data files kept beside the model cards that are not model cards (advisor capability matrix).
MODELS_NON_CARDS = ("capability_matrix.yaml",)
JUDGEMENTS = ("high", "mid", "low-mid", "low")
LIST_FIELDS = ("hosts", "domains", "roles_allowed", "roles_denied", "unverified")
# Assessment fields shared by model cards and profile cards (profile value wins).
ASSESSMENT_FIELDS = ("quota_pool", "hosts", "domains", "context_window", "handoff_ratio", "max_concurrent",
                     "roles_allowed", "roles_denied", "judgement", "night_ok", "measured", "evidence", "unverified")
# `unverified` names the fields whose value still lacks an outside fact (shown, never guessed).
UNVERIFIABLE = ("model", "quota_pool", "hosts", "domains", "context_window", "handoff_ratio", "max_concurrent",
                "roles_allowed", "roles_denied", "judgement", "night_ok")
AGENT_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}$")
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


class AgentRegistryError(ValueError):
    """An agent card or registry directory is invalid."""


@dataclass(frozen=True)
class AgentCard:
    id: str
    display_name: str
    cli: str
    capabilities: Tuple[str, ...]
    role_file: Optional[str] = None
    cost_rank: Optional[int] = None
    latency_rank: Optional[int] = None
    risk_rank: Optional[int] = None
    model: Optional[str] = None
    profile: Optional[str] = None
    model_card: Optional[str] = None
    quota_pool: Optional[str] = None
    hosts: Optional[Tuple[str, ...]] = None
    domains: Optional[Tuple[str, ...]] = None
    context_window: Optional[int] = None
    handoff_ratio: Optional[float] = None
    max_concurrent: Optional[int] = None
    roles_allowed: Optional[Tuple[str, ...]] = None
    roles_denied: Optional[Tuple[str, ...]] = None
    judgement: Optional[str] = None
    night_ok: Optional[bool] = None
    measured: Optional[Mapping[str, Any]] = field(default=None, compare=False, hash=False)
    evidence: Optional[str] = None
    unverified: Optional[Tuple[str, ...]] = None
    # Advisor whitelist of worker-kit domains for this card; None = no restriction.
    kit_domains: Optional[Tuple[str, ...]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        for name in LIST_FIELDS:
            if data[name] is not None:
                data[name] = list(data[name])
        if self.kit_domains is not None:
            data["kit_domains"] = list(self.kit_domains)
        return data


@dataclass(frozen=True)
class ModelCard:
    """One model's assessment (design v2 §3); filled in by hand, ``measured`` by the bookkeeper."""
    id: str
    display_name: str
    cli: str
    model: Optional[str] = None
    quota_pool: Optional[str] = None
    hosts: Optional[Tuple[str, ...]] = None
    domains: Optional[Tuple[str, ...]] = None
    context_window: Optional[int] = None
    handoff_ratio: Optional[float] = None
    max_concurrent: Optional[int] = None
    roles_allowed: Optional[Tuple[str, ...]] = None
    roles_denied: Optional[Tuple[str, ...]] = None
    judgement: Optional[str] = None
    night_ok: Optional[bool] = None
    measured: Optional[Mapping[str, Any]] = field(default=None, compare=False, hash=False)
    evidence: Optional[str] = None
    unverified: Optional[Tuple[str, ...]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for name in LIST_FIELDS:
            if data[name] is not None:
                data[name] = list(data[name])
        return data


def _default_directory() -> Optional[Path]:
    """A checkout-level agents directory with card files, else None (cards from fleet.yaml)."""
    directory = PROJECT_DIR / "agents"
    if directory.is_dir() and any(
            path.is_file() and path.suffix.lower() in CARD_SUFFIXES for path in directory.iterdir()):
        return directory
    return None


def _read_card(path: Path) -> Mapping[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise AgentRegistryError(
                    "%s needs PyYAML; use JSON cards when PyYAML is unavailable" % path
                ) from exc
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except AgentRegistryError:
        raise
    except Exception as exc:
        raise AgentRegistryError("could not load agent card %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise AgentRegistryError("agent card %s must contain one object" % path)
    return data


def _required_string(data: Mapping[str, Any], field: str, path: Path) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AgentRegistryError("agent card %s needs a non-empty %s" % (path, field))
    return value.strip()


def _safe_role_file(value: Any, path: Path) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AgentRegistryError("agent card %s role_file must be a relative path" % path)
    value = value.strip()
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or ".." in posix.parts or value.startswith("~")):
        raise AgentRegistryError("agent card %s role_file must be relative and stay inside its registry" % path)
    return value


def _optional_rank(data: Mapping[str, Any], field: str, path: Path) -> Optional[int]:
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentRegistryError("agent card %s %s must be a non-negative integer" % (path, field))
    return value


def _optional_launch_string(data: Mapping[str, Any], field: str, path: Path) -> Optional[str]:
    value = data.get(field)
    if value is None:
        return None
    normalized = _required_string(data, field, path)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AgentRegistryError("agent card %s %s must not contain control characters" % (path, field))
    return normalized


def _optional_string(data: Mapping[str, Any], field_name: str, path: Path) -> Optional[str]:
    if data.get(field_name) is None:
        return None
    return _optional_launch_string(data, field_name, path)


def _optional_list(data: Mapping[str, Any], field_name: str, path: Path) -> Optional[Tuple[str, ...]]:
    value = data.get(field_name)
    if value is None:
        return None
    if (not isinstance(value, list) or not value
            or not all(isinstance(item, str) and item.strip() for item in value)):
        raise AgentRegistryError("agent card %s %s must be a non-empty string list or null" % (path, field_name))
    return tuple(dict.fromkeys(item.strip() for item in value))


def _optional_positive(data: Mapping[str, Any], field_name: str, path: Path) -> Optional[int]:
    value = data.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentRegistryError("agent card %s %s must be a positive integer" % (path, field_name))
    return value


def _assessment(data: Mapping[str, Any], path: Path) -> Dict[str, Any]:
    """The shared assessment fields, validated; absent and null both mean 'not set'."""
    ratio = data.get("handoff_ratio")
    if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
                              or not 0 < ratio <= 1):
        raise AgentRegistryError("agent card %s handoff_ratio must be a number in (0, 1]" % path)
    judgement = data.get("judgement")
    if judgement is not None and judgement not in JUDGEMENTS:
        raise AgentRegistryError("agent card %s judgement must be one of %s" % (path, ", ".join(JUDGEMENTS)))
    night_ok = data.get("night_ok")
    if night_ok is not None and type(night_ok) is not bool:
        raise AgentRegistryError("agent card %s night_ok must be a boolean" % path)
    measured = data.get("measured")
    if measured is not None and not isinstance(measured, dict):
        raise AgentRegistryError("agent card %s measured must be an object or null" % path)
    evidence = data.get("evidence")
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
        raise AgentRegistryError("agent card %s evidence must be a non-empty string" % path)
    out = {name: _optional_list(data, name, path) for name in LIST_FIELDS}
    unknown = sorted(set(out["unverified"] or ()) - set(UNVERIFIABLE))
    if unknown:
        raise AgentRegistryError("agent card %s unverified lists unknown fields: %s" % (path, ", ".join(unknown)))
    overlap = set(out["roles_allowed"] or ()).intersection(out["roles_denied"] or ())
    if overlap:
        raise AgentRegistryError("agent card %s lists roles both allowed and denied: %s"
                                 % (path, ", ".join(sorted(overlap))))
    out.update(quota_pool=_optional_string(data, "quota_pool", path),
               context_window=_optional_positive(data, "context_window", path),
               handoff_ratio=float(ratio) if ratio is not None else None,
               max_concurrent=_optional_positive(data, "max_concurrent", path),
               judgement=judgement, night_ok=night_ok,
               measured=dict(measured) if measured is not None else None,
               evidence=evidence.strip() if evidence is not None else None)
    return out


def _model_from_data(data: Mapping[str, Any], path: Path) -> ModelCard:
    model_id = _required_string(data, "id", path)
    if not AGENT_ID_RE.fullmatch(model_id):
        raise AgentRegistryError("model card %s id must match %s" % (path, AGENT_ID_RE.pattern))
    return ModelCard(id=model_id, display_name=_required_string(data, "display_name", path),
                     cli=_required_string(data, "cli", path), model=_optional_string(data, "model", path),
                     **_assessment(data, path))


def _optional_kit(data: Mapping[str, Any], path: Path) -> Optional[Tuple[str, ...]]:
    kit = data.get("kit")
    if kit is None:
        return None
    domains = kit.get("domains") if isinstance(kit, dict) and set(kit) <= {"domains"} else None
    if (not isinstance(domains, list)
            or not all(isinstance(item, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item) for item in domains)):
        raise AgentRegistryError("agent card %s kit must be {domains: [domain names]}" % path)
    return tuple(dict.fromkeys(domains))


def _card_from_data(data: Mapping[str, Any], path: Path) -> AgentCard:
    agent_id = _required_string(data, "id", path)
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise AgentRegistryError("agent card %s id must match %s" % (path, AGENT_ID_RE.pattern))
    capabilities = data.get("capabilities")
    if (not isinstance(capabilities, list) or not capabilities
            or not all(isinstance(item, str) and item.strip() for item in capabilities)):
        raise AgentRegistryError("agent card %s capabilities must be a non-empty string list" % path)
    normalized = tuple(dict.fromkeys(item.strip() for item in capabilities))
    return AgentCard(
        id=agent_id,
        display_name=_required_string(data, "display_name", path),
        cli=_required_string(data, "cli", path),
        capabilities=normalized,
        role_file=_safe_role_file(data.get("role_file"), path),
        cost_rank=_optional_rank(data, "cost_rank", path),
        latency_rank=_optional_rank(data, "latency_rank", path),
        risk_rank=_optional_rank(data, "risk_rank", path),
        model=_optional_launch_string(data, "model", path),
        profile=_optional_launch_string(data, "profile", path),
        model_card=_optional_string(data, "model_card", path),
        **_assessment(data, path),
        kit_domains=_optional_kit(data, path),
    )


def _inherit(card: AgentCard, models: Mapping[str, ModelCard], path: Path) -> AgentCard:
    """Fill the profile's unset assessment fields from its model card."""
    if card.model_card is None:
        return card
    model = models.get(card.model_card)
    if model is None:
        raise AgentRegistryError("agent card %s model_card %s not found in %s/"
                                 % (path, card.model_card, MODELS_SUBDIR))
    if model.cli != card.cli:
        raise AgentRegistryError("agent card %s cli %s differs from model card %s cli %s"
                                 % (path, card.cli, model.id, model.cli))
    filled = {name: getattr(model, name) for name in ASSESSMENT_FIELDS if getattr(card, name) is None}
    return replace(card, **filled) if filled else card


class AgentRegistry:
    """A deterministic, side-effect-free collection of validated agent cards."""

    def __init__(self, directory: Optional[Union[str, Path]] = None):
        """``directory``: per-file cards (``*.json`` / ``*.yaml`` + ``models/``); None: the ``models`` and
        ``profiles`` blocks of fleet.yaml, with role files from the packaged ``agents/`` directory."""
        found = Path(directory).expanduser() if directory is not None else _default_directory()
        if found is None or found.resolve() == (PACKAGE_DIR / "agents").resolve():  # a task stores the dir it routed with
            self.directory = PACKAGE_DIR / "agents"
            self._models = self._block_models()
            self._cards = self._block_cards(self._models)
        else:
            self.directory = found
            self._models = self._load_models(self.directory / MODELS_SUBDIR)
            self._cards = self._load(self.directory, self._models)

    @staticmethod
    def _block(name: str) -> Dict[str, Mapping[str, Any]]:
        data = fleet_config.block(name) or {}
        if not isinstance(data, dict):
            raise AgentRegistryError("%s: block %s must map card ids to cards" % (fleet_config.source(), name))
        for key, card in data.items():
            if not isinstance(card, dict) or card.get("id") != key:
                raise AgentRegistryError("%s: %s.%s must be a card whose id is %s" % (fleet_config.source(), name, key, key))
        return data

    @classmethod
    def _block_models(cls) -> Dict[str, ModelCard]:
        return dict(sorted((key, _model_from_data(data, Path("fleet.yaml#models." + key)))
                           for key, data in cls._block("models").items()))

    @classmethod
    def _block_cards(cls, models: Mapping[str, ModelCard]) -> Tuple[AgentCard, ...]:
        cards = []
        for key, data in cls._block("profiles").items():
            path = Path("fleet.yaml#profiles." + key)
            cards.append(_inherit(_card_from_data(data, path), models, path))
        if not cards:
            raise AgentRegistryError("%s: block profiles has no cards" % fleet_config.source())
        return tuple(sorted(cards, key=lambda card: card.id))

    @staticmethod
    def _paths(directory: Path) -> List[Path]:
        return sorted((path for path in directory.iterdir()
                       if path.is_file() and path.suffix.lower() in CARD_SUFFIXES), key=lambda path: path.name)

    @classmethod
    def _load_models(cls, directory: Path) -> Dict[str, ModelCard]:
        """Optional ``models/`` subdirectory; absent means no model cards."""
        models: Dict[str, ModelCard] = {}
        if not directory.is_dir():
            return models
        for path in cls._paths(directory):
            if path.name in MODELS_NON_CARDS:
                continue
            model = _model_from_data(_read_card(path), path)
            if model.id in models:
                raise AgentRegistryError("duplicate model card id %s in %s" % (model.id, directory))
            models[model.id] = model
        return dict(sorted(models.items()))

    @classmethod
    def _load(cls, directory: Path, models: Mapping[str, ModelCard]) -> Tuple[AgentCard, ...]:
        if not directory.is_dir():
            raise AgentRegistryError("agent registry directory not found: %s" % directory)
        paths = cls._paths(directory)
        if not paths:
            raise AgentRegistryError("agent registry contains no JSON or YAML cards: %s" % directory)
        cards: List[AgentCard] = []
        seen = set()
        for path in paths:
            card = _inherit(_card_from_data(_read_card(path), path), models, path)
            if card.id in seen:
                raise AgentRegistryError("duplicate agent id %s in %s" % (card.id, directory))
            seen.add(card.id)
            cards.append(card)
        return tuple(sorted(cards, key=lambda card: card.id))

    @property
    def cards(self) -> Tuple[AgentCard, ...]:
        return self._cards

    @property
    def models(self) -> Tuple[ModelCard, ...]:
        return tuple(self._models.values())

    def model(self, model_id: str) -> ModelCard:
        return self._models[model_id]

    def get(self, agent_id: str) -> AgentCard:
        for card in self._cards:
            if card.id == agent_id:
                return card
        raise KeyError(agent_id)

    def as_list(self) -> List[Dict[str, Any]]:
        return [card.to_dict() for card in self._cards]

    def __iter__(self) -> Iterable[AgentCard]:
        return iter(self._cards)
