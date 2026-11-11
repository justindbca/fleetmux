"""Worker kit (advisor plan §4.3): load/validate, domain catalog, clear-then-link, CLAUDE.md."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleetmux import kit as kitmod  # noqa: E402

# A reduced copy of a host project's config/context_domains.yaml (same fields, two domains + one without summary).
TABLE = """
domains:
  research_web:
    summary: 联网研究 / 抓取 / 社区检索
    triggers: [研究, 搜索, Reddit]
    rules: domains/research_web/RULES.md
    memory_query: 研究
    skills: [web-fetch-fallback]
    agents: [web-research-specialist]
    mcp: [tavily, reddit]
  us_options:
    summary: 前端 / React / CSS
    triggers: [前端, React, CSS]
    rules: domains/us_options/RULES.md
    skills: [option-tier-screen]
    agents: [finance-research-specialist]
    mcp: [tavily, reddit]
  fleet:
    triggers: [fleet, cmux, 监工, worker, daemon, extra]
    rules: domains/fleet/RULES.md
    skills: []
    agents: []
    mcp: []
"""


def make_kit(root, table=TABLE):
    root = Path(root)
    (root / 'core').mkdir(parents=True, exist_ok=True)
    (root / 'core' / 'CLAUDE.md').write_text('# Core\nNever use yfinance.\n')
    (root / 'context_domains.yaml').write_text(table)
    for domain, skill, agent in (('research_web', 'web-fetch-fallback', 'web-research-specialist'),
                                 ('us_options', 'option-tier-screen', 'finance-research-specialist'),
                                 ('fleet', None, None)):
        d = root / 'domains' / domain
        d.mkdir(parents=True, exist_ok=True)
        (d / 'RULES.md').write_text('RULES OF ' + domain + '\n')
        if skill:
            (d / 'skills' / skill).mkdir(parents=True, exist_ok=True)
            (d / 'skills' / skill / 'SKILL.md').write_text('skill ' + skill)
        if agent:
            (d / 'agents').mkdir(parents=True, exist_ok=True)
            (d / 'agents' / (agent + '.md')).write_text('agent ' + agent)
    (root / 'mcp.json').write_text(json.dumps({'mcpServers': {'tavily': {}, 'reddit': {}}}))
    return root


def test_load_and_catalog_uses_summary_then_triggers(tmp_path):
    k = kitmod.Kit(make_kit(tmp_path / 'kit'))
    catalog = {c['name']: c['summary'] for c in k.domains_catalog()}
    assert catalog['research_web'] == '联网研究 / 抓取 / 社区检索'
    assert catalog['fleet'] == 'fleet, cmux, 监工, worker, daemon'  # no summary: first five triggers
    assert 'RULES OF' not in json.dumps(k.domains_catalog(), ensure_ascii=False)  # catalog never carries rules text


@pytest.mark.parametrize('breakage, match', [
    (lambda r: (r / 'domains/us_options/RULES.md').unlink(), 'rules file missing'),
    (lambda r: (r / 'domains/research_web/skills/web-fetch-fallback/SKILL.md').unlink()
     or (r / 'domains/research_web/skills/web-fetch-fallback').rmdir(), 'skill not in the kit'),
    (lambda r: (r / 'domains/us_options/agents/finance-research-specialist.md').unlink(), 'agent not in the kit'),
    (lambda r: (r / 'context_domains.yaml').write_text('domains: [1, 2]'), 'domains'),
    (lambda r: (r / 'context_domains.yaml').write_text(TABLE.replace('domains/fleet/RULES.md', '../outside.md')),
     'outside the kit'),
    (lambda r: (r / 'context_domains.yaml').write_text(TABLE + '  Bad Name:\n    rules: x\n'), 'domain name'),
    (lambda r: (r / 'context_domains.yaml').unlink(), 'has no context_domains.yaml'),
])
def test_invalid_kit_is_refused_whole(tmp_path, breakage, match):
    root = make_kit(tmp_path / 'kit')
    (tmp_path / 'outside.md').write_text('x')
    breakage(root)
    with pytest.raises(kitmod.KitError, match=match):
        kitmod.Kit(root)


def test_install_clears_then_links_and_copies_full_mcp(tmp_path):
    k = kitmod.Kit(make_kit(tmp_path / 'kit'))
    cwd = tmp_path / 'worker-1'
    stale = cwd / '.claude' / 'skills' / 'old-skill'
    stale.mkdir(parents=True)
    (cwd / '.claude' / 'agents').mkdir()
    (cwd / '.claude' / 'agents' / 'old-agent.md').write_text('old')
    k.install(cwd, ['research_web'])
    assert sorted(p.name for p in (cwd / '.claude' / 'skills').iterdir()) == ['web-fetch-fallback']
    assert sorted(p.name for p in (cwd / '.claude' / 'agents').iterdir()) == ['web-research-specialist.md']
    assert (cwd / '.claude' / 'skills' / 'web-fetch-fallback').is_symlink()
    assert json.loads((cwd / 'mcp.json').read_text())['mcpServers'].keys() == {'tavily', 'reddit'}
    k.install(cwd, [])  # domains: [] (other) = both empty
    assert not (cwd / '.claude' / 'skills').exists() and not (cwd / '.claude' / 'agents').exists()


def test_hotload_link_adds_without_clearing(tmp_path):
    k = kitmod.Kit(make_kit(tmp_path / 'kit'))
    cwd = tmp_path / 'w'
    k.install(cwd, ['research_web'])
    linked = k.link_domains(cwd, ['us_options'])
    assert linked == {'skills': ['option-tier-screen'], 'agents': ['finance-research-specialist']}
    assert sorted(p.name for p in (cwd / '.claude' / 'skills').iterdir()) == ['option-tier-screen', 'web-fetch-fallback']


def test_claude_md_is_scope_core_and_spawn_domain_rules_without_memory(tmp_path):
    k = kitmod.Kit(make_kit(tmp_path / 'kit'))
    text = k.claude_md('# Worker scope\nreply format', ['us_options'])
    assert text.index('# Worker scope') < text.index('Never use yfinance') < text.index('RULES OF us_options')
    assert 'RULES OF research_web' not in text and 'MEMORY' not in text
    assert k.rules_text(['fleet', 'research_web']).count('## Domain rules:') == 2
