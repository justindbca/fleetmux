"""Deterministic, offline agent-profile routing."""

from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from cmux_fleet.agent_registry import AgentCard, AgentRegistry


@dataclass(frozen=True)
class RouteDecision:
    agent_id: str
    capability: str
    reason: str
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_RULES = (
    ("code-edit", re.compile(
        r"\b(code|coding|edit|refactor|fix|implement|implementation|feature|bug|patch|develop|write)\b"
        r"|代码|编码|修改|编辑|重构|修复|实现|功能", re.IGNORECASE)),
    ("test-fix", re.compile(
        r"\b(test|tests|testing|pytest|unittest|failing|failure|fix|bug)\b|测试|修复|失败",
        re.IGNORECASE)),
    ("code-review", re.compile(
        r"\b(review|audit|inspect|inspection)\b|复核|审查|评审|审核", re.IGNORECASE)),
    ("safety-audit", re.compile(
        r"\b(review|audit|safety|security|risk)\b|复核|审查|安全|风险|审核", re.IGNORECASE)),
    ("web-research", re.compile(
        r"\b(search|web|research|source|sources|browse|citation|online)\b|资料|网页|搜索|调研|研究",
        re.IGNORECASE)),
    ("planning", re.compile(
        r"\b(plan|planning|design|architecture|architect|proposal)\b|计划|设计|架构|方案",
        re.IGNORECASE)),
)


def classify_task(text: str) -> Tuple[str, ...]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("task text must be a non-empty string")
    return tuple(capability for capability, pattern in _RULES if pattern.search(text))


def _rank(value: Optional[int]) -> int:
    return value if value is not None else 1_000_000


def _primary_capability(hints: Sequence[str]) -> str:
    for capability in ("code-review", "safety-audit", "web-research", "test-fix", "code-edit", "planning"):
        if capability in hints:
            return capability
    return "planning"


def host_matches(card: AgentCard, host: Optional[str]) -> bool:
    """A card without ``hosts`` runs anywhere; otherwise case-insensitive match on the full
    host name or its first label (``studio.local`` matches ``studio``)."""
    if card.hosts is None or host is None:
        return True
    name = host.strip().lower()
    allowed = {item.lower() for item in card.hosts}
    return name in allowed or name.split(".", 1)[0] in allowed


def _excluded(card: AgentCard, host: Optional[str], role: Optional[str],
              occupancy: Optional[Mapping[str, int]]) -> Optional[str]:
    """Why a card may not take this task (None: eligible). Cards without the fields are unrestricted."""
    if not host_matches(card, host):
        return "host %s not in %s" % (host, ",".join(card.hosts))
    if role is not None:
        if card.roles_denied and role in card.roles_denied:
            return "role %s denied" % role
        if card.roles_allowed is not None and role not in card.roles_allowed:
            return "role %s not allowed" % role
    if occupancy is not None and card.max_concurrent is not None:
        key = card.model_card or card.id  # counted per model (model_usage.limit_key)
        if int(occupancy.get(key, 0)) >= card.max_concurrent:
            return "max_concurrent %d reached for model %s" % (card.max_concurrent, key)
    return None


def _sort_key(card: AgentCard, overlap: int, primary: str,
              preferred_cli: Optional[str] = None) -> Tuple[Any, ...]:
    if primary in ("code-review", "safety-audit"):
        tie_breakers = (_rank(card.risk_rank), _rank(card.latency_rank), _rank(card.cost_rank))
    elif primary in ("code-edit", "test-fix"):
        tie_breakers = (_rank(card.latency_rank), _rank(card.cost_rank), _rank(card.risk_rank))
    else:
        tie_breakers = (_rank(card.latency_rank), _rank(card.cost_rank), _rank(card.risk_rank))
    return ((-int(primary in card.capabilities), -int(card.cli == preferred_cli), -overlap)
            + tie_breakers + (card.id,))


def route_task(text: str, registry: Optional[AgentRegistry] = None, *,
               required_capability: Optional[str] = None,
               preferred_cli: Optional[str] = None, host: Optional[str] = None,
               role: Optional[str] = None,
               occupancy: Optional[Mapping[str, int]] = None) -> RouteDecision:
    """Route by keywords, or exclusively by an explicitly required capability.

    A required capability must be supported by the selected card; unrelated task
    keywords do not affect eligibility or ranking. Omitting it preserves legacy routing.
    ``preferred_cli`` ranks cards on that CLI first among equally capable cards; None keeps legacy order.
    Filters (each None = off, the legacy behaviour): ``host`` keeps cards whose ``hosts`` include it;
    ``role`` drops cards that deny it or whose ``roles_allowed`` omit it; ``occupancy`` ({model id:
    slots that would hold it}, see model_usage.Usage.for_planning) drops cards at their ``max_concurrent``. Filtering happens before ranking.
    """
    registry = registry or AgentRegistry()
    hints = classify_task(text)
    if required_capability is not None:
        if not isinstance(required_capability, str) or not required_capability.strip():
            raise ValueError("required_capability must be a non-empty string")
        primary = required_capability.strip()
        hints = (primary,)
    else:
        primary = _primary_capability(hints)
    effective_hints = hints or (primary,)
    if role is not None and (not isinstance(role, str) or not role.strip()):
        raise ValueError("role must be a non-empty string")
    if host is not None and (not isinstance(host, str) or not host.strip()):
        raise ValueError("host must be a non-empty string")
    candidates, excluded = [], []
    for card in registry:
        overlap = len(set(effective_hints).intersection(card.capabilities))
        if overlap:
            why = _excluded(card, host, role and role.strip(), occupancy)
            if why:
                excluded.append("%s (%s)" % (card.id, why))
            else:
                candidates.append((card, overlap))
    if not candidates:
        if excluded:
            raise ValueError("no eligible agent for task capabilities %s; filtered out: %s"
                             % (", ".join(effective_hints), "; ".join(excluded)))
        raise ValueError("no registered agent supports task capabilities: %s" % ", ".join(effective_hints))
    card, overlap = min(candidates, key=lambda item: _sort_key(item[0], item[1], primary, preferred_cli))
    confidence = .25 if not hints else round(overlap / len(effective_hints), 2)
    reason = (
        "%s matched %s; selected %s using %s tie-breakers"
        % (card.id, ", ".join(sorted(set(effective_hints).intersection(card.capabilities))),
           card.display_name, "risk" if primary in ("code-review", "safety-audit") else "latency/cost")
    )
    return RouteDecision(card.id, primary, reason, confidence)
