"""Toolchain requirements of a segment (`needs`, e.g. `rust`, `node>=20`) against a machine's measured
toolchains (`machine.toolchains`, e.g. {"rust": "1.83"}). Only compares; never installs anything."""
import re

_NEED_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9_.+-]*)\s*(?:(>=|<=|==|>|<|=)\s*(\S+))?\s*$')
_OPS = {'>=': lambda a, b: a >= b, '<=': lambda a, b: a <= b, '==': lambda a, b: a == b, '=': lambda a, b: a == b,
        '>': lambda a, b: a > b, '<': lambda a, b: a < b}


def parse_need(text):
    """'node>=20' -> ('node', '>=', (20,)); 'rust' -> ('rust', None, None). ValueError when malformed."""
    m = _NEED_RE.match(text or '') if isinstance(text, str) else None
    if not m:
        raise ValueError('toolchain requirement must look like name or name>=1.2: ' + repr(text))
    name, op, ver = m.groups()
    if op and not version(ver):
        raise ValueError('toolchain requirement must look like name or name>=1.2: ' + repr(text))
    return name, op, version(ver) if op else None


def version(text):
    """The numeric parts of a version string: '1.83.0-nightly' -> (1, 83, 0), 'go1.22' -> (1, 22)."""
    return tuple(int(x) for x in re.findall(r'\d+', str(text or '')))


def missing(toolchains, needs):
    """The requirements in `needs` that `toolchains` does not meet, in their original spelling."""
    have = {str(k): v for k, v in (toolchains or {}).items()}
    out = []
    for need in needs or []:
        name, op, want = parse_need(need)
        if name not in have:
            out.append(need)
        elif op and not _OPS[op](version(have[name]), want):
            out.append(need)
    return out
