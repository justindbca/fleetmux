"""Worker kit with project domains (advisor runs only; design doc §4.3 of the advisor plan).

Layout of a kit directory (built on the project side, consumed here read-only):

    DIR/core/CLAUDE.md                 project core rules (no domain index, no memory)
    DIR/context_domains.yaml           domain table: summary / triggers / rules / skills / agents / mcp
    DIR/domains/<domain>/RULES.md      rules file named by the table's ``rules`` (relative to DIR)
    DIR/domains/<domain>/skills/<name>/
    DIR/domains/<domain>/agents/<name>.md
    DIR/mcp.json                       full MCP set, started with every Claude worker
    DIR/fleets/<name>/roster.yaml      roster templates for start --kit-fleet (fleet.py copies them into the
                                       run; not read or checked here, design doc §30)

The kit never carries memory files. A Claude worker gets its domains linked into
``cwd/.claude/{skills,agents}``: a respawn clears both first and links the chosen domains,
a hot-load only adds links. MCP servers are not split by domain.
"""
import os
import re
import shutil
from pathlib import Path

DOMAIN_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}$')
NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
TABLE = 'context_domains.yaml'


class KitError(ValueError):
    """The kit directory is invalid; the whole kit is refused (like a bad roster)."""


def _inside(root, path):
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


class Kit:
    def __init__(self, directory):
        self.dir = Path(directory).expanduser()
        self.domains = self._load()

    def _load(self):
        if not (self.dir / TABLE).is_file():
            raise KitError('worker kit ' + str(self.dir) + ' has no ' + TABLE)
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - PyYAML is a fleet dependency for rosters too
            raise KitError('reading ' + TABLE + ' needs PyYAML') from exc
        try:
            data = yaml.safe_load((self.dir / TABLE).read_text(encoding='utf-8'))
        except (OSError, yaml.YAMLError) as exc:
            raise KitError(TABLE + ': ' + str(exc)) from exc
        table = (data or {}).get('domains') if isinstance(data, dict) else None
        if not isinstance(table, dict):
            raise KitError(TABLE + ': top level needs a `domains:` mapping')
        bad, out = [], {}
        for name, entry in table.items():
            where = TABLE + ' ' + str(name)
            if not isinstance(name, str) or not DOMAIN_RE.fullmatch(name):
                bad.append(where + ': domain name must match ' + DOMAIN_RE.pattern)
                continue
            if not isinstance(entry, dict):
                bad.append(where + ': entry must be a mapping')
                continue
            rules = entry.get('rules')
            if not isinstance(rules, str) or not rules.strip():
                bad.append(where + ': rules path is required')
            elif Path(rules).is_absolute() or not _inside(self.dir, self.dir / rules) or not (self.dir / rules).is_file():
                bad.append(where + ': rules file missing or outside the kit: ' + rules)
            lists = {}
            for field in ('skills', 'agents', 'triggers', 'mcp'):
                value = entry.get(field) or []
                if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                    bad.append(where + ': ' + field + ' must be a list of names')
                    value = []
                lists[field] = value
            for skill in lists['skills']:
                path = self.dir / 'domains' / name / 'skills' / skill
                if not NAME_RE.fullmatch(skill) or not path.is_dir() or not _inside(self.dir, path):
                    bad.append(where + ': skill not in the kit: domains/' + name + '/skills/' + skill + '/')
            for agent in lists['agents']:
                path = self.dir / 'domains' / name / 'agents' / (agent + '.md')
                if not NAME_RE.fullmatch(agent) or not path.is_file() or not _inside(self.dir, path):
                    bad.append(where + ': agent not in the kit: domains/' + name + '/agents/' + agent + '.md')
            summary = entry.get('summary')
            if summary is not None and not isinstance(summary, str):
                bad.append(where + ': summary must be text')
            out[name] = dict(summary=(summary or '').strip(), rules=rules, **lists)
        if bad:
            raise KitError('worker kit ' + str(self.dir) + ' rejected:\n' + '\n'.join('- ' + b for b in bad))
        return out

    # -- what the advisor sees
    def domains_catalog(self):
        """Name + one line per domain (``summary``; else the first triggers). No rules text."""
        return [{'name': n, 'summary': e['summary'] or ', '.join(e['triggers'][:5])}
                for n, e in self.domains.items()]

    def rules_text(self, domains):
        return ''.join('\n## Domain rules: ' + d + '\n\n' + (self.dir / self.domains[d]['rules']).read_text().strip() + '\n'
                       for d in domains)

    def core_text(self):
        core = self.dir / 'core' / 'CLAUDE.md'
        return core.read_text().strip() if core.is_file() else ''

    def mcp_path(self):
        path = self.dir / 'mcp.json'
        return path if path.is_file() else None

    # -- what a worker gets
    def claude_md(self, scope, domains):
        """fleet role scope + project core rules + the domain rules installed at spawn."""
        text = scope.rstrip() + '\n'
        core = self.core_text()
        if core:
            text += '\n# Project core rules\n\n' + core + '\n'
        return text + self.rules_text(domains)

    @staticmethod
    def clear(cwd):
        for sub in ('agents', 'skills'):
            path = Path(cwd) / '.claude' / sub
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

    def link_domains(self, cwd, domains):
        """Link each domain's skills / agents into cwd/.claude (adds; never removes). Returns names linked."""
        linked = {'skills': [], 'agents': []}
        for d in domains:
            entry = self.domains[d]
            for kind, names, suffix in (('skills', entry['skills'], ''), ('agents', entry['agents'], '.md')):
                target_dir = Path(cwd) / '.claude' / kind
                target_dir.mkdir(parents=True, exist_ok=True)
                for name in names:
                    link = target_dir / (name + suffix)
                    if link.is_symlink() or link.is_file():
                        link.unlink()
                    elif link.is_dir():
                        shutil.rmtree(link)
                    os.symlink((self.dir / 'domains' / d / kind / (name + suffix)).resolve(), link)
                    linked[kind].append(name)
        return linked

    def install(self, cwd, domains):
        """Respawn / first spawn: clear the old links, link these domains, copy the full MCP set."""
        self.clear(cwd)
        linked = self.link_domains(cwd, domains)
        mcp = self.mcp_path()
        if mcp:
            shutil.copy(mcp, Path(cwd) / 'mcp.json')
        return linked


def load(directory):
    return Kit(directory)
