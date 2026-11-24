"""`fleetmux tree check <file>`: check a tree file before submitting it (docs/design.md §62).

The submitter's division of a task (the spec ``tree`` file, kept as the seed) goes through the same code the program
runs when the supervisor hands in a tree: ``decompose.seed_from`` reads it, ``decompose.apply`` (``shape_errors``,
the split check) refuses a bad shape with the same message, ``decompose.judge`` computes the longest chain, the
maximum parallel width and the blocks it would mark leaf=false with their dT upper / dC lower bounds. Nothing is
started or contacted.

Parameters: the shipped ``decompose`` block, or with ``--config`` a private fleet.yaml whose ``decompose`` block is
laid over it (as the daemon merges the private file). Exit 0: the shape is fine; 1: shape errors or an unreadable
file; 2: a bad command line or parameter block.
"""
import argparse
import json
import os
import sys

from fleetmux import decompose, fleet_config


def params(path=None):
    """The shipped decompose block, with ``path``'s block laid over it. ValueError on a bad block."""
    shipped = fleet_config.block('decompose', path=fleet_config.SHIPPED) or {}
    raw = dict(shipped)
    if path is not None:
        if not os.path.isfile(path):
            raise ValueError('--config: no such file: ' + path)
        private = fleet_config.block('decompose', path=path) or {}
        if not isinstance(private, dict):
            raise ValueError('--config: the decompose block must be a mapping')
        for k, v in private.items():
            raw[k] = dict(raw.get(k) or {}, **v) if k in ('split', 'cost') and isinstance(v, dict) else v
    errors = decompose.validate_block(raw)
    if errors:
        raise ValueError('; '.join(errors))
    return decompose.merged(raw)


def check(path, prefix='M', config=None):
    """{ok, errors, chain, width, leaf_false: [{id, dT, dC, reason}], seed: {by, blocks}, params} for tree file
    ``path``; ``params`` raises ValueError (a usage error), everything about the file goes into ``errors``."""
    p = params(config)
    root = 'root' if prefix == 'M' else prefix.rstrip('.')
    out = {'ok': False, 'errors': [], 'chain': None, 'width': None, 'leaf_false': [], 'seed': None,
           'params': {'source': config or 'shipped', 'split': p['split'], 'cost': p['cost']}}
    try:
        tree = decompose.seed_from(os.path.abspath(path), root, '', prefix)
    except ValueError as exc:
        out['errors'] = [str(exc)]
        return out
    seed = tree.get('seed')
    out['seed'] = {'by': seed['by'], 'blocks': len(seed['blocks'])} if seed else None
    try:  # the dispatch-side check of a handed-in tree: the same function, the same message
        tree = decompose.apply(tree, [], [], {'root': root, 'prefix': prefix, 'params': p})
    except ValueError as exc:
        out['errors'] = [str(exc)]
    try:
        decompose.judge(tree, p)
    except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:  # a shape too broken to schedule
        out['errors'].append('accounts: ' + str(exc)[:300])
        return out
    out.update(ok=not out['errors'], chain=tree['chain'], width=tree['width'])
    out['leaf_false'] = [{'id': n['id'], 'dT': n['bound']['dT'], 'dC': n['bound']['dC'], 'reason': n['leaf_reason']}
                         for n in decompose.level_nodes(tree) if n.get('leaf') is False and n.get('bound')]
    return out


def render(path, res):
    errors = res['errors']
    lines = ['tree check ' + path,
             'shape: ' + ('ok' if not errors else str(len(errors)) + ' error' + ('s' if len(errors) > 1 else ''))]
    lines += ['- ' + e for e in errors]
    if res['seed']:
        lines.append('seed: by ' + res['seed']['by'] + ', ' + str(res['seed']['blocks']) + ' blocks')
    if res['chain'] is not None:
        lines.append('longest chain: ' + (' -> '.join(res['chain']['ids']) or '-') + ' ('
                     + decompose._fmt(res['chain']['minutes']) + ' min)')
        lines.append('max parallel width: ' + str(res['width']))
        lines.append('leaf=false: ' + ('; '.join(x['id'] + ' (dT <= ' + decompose._fmt(x['dT']) + ' min, dC >= '
                                                 + decompose._fmt(x['dC']) + ' session min)'
                                                 for x in res['leaf_false']) or 'none'))
    p = res['params']
    lines.append('params: ' + p['source'] + ' (k ' + str(p['split']['k']) + ', margin '
                 + str(p['split'].get('margin') or 0) + ', '
                 + ', '.join(k + ' ' + str(v or 0) for k, v in p['cost'].items()) + ')')
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='fleetmux tree', description='decomposition trees (docs/design.md section 62)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('check', help='check a tree file before submitting it: shape, longest chain, width, '
                       'leaf=false blocks; exit 1 on shape errors')
    c.add_argument('file', help='the tree JSON file ({"nodes": [...], "decisions": [...], "by": optional})')
    c.add_argument('--config', help='a private fleet.yaml whose decompose block is laid over the shipped one '
                   '(default: the shipped values)')
    c.add_argument('--prefix', default='M', help='the id prefix of the level (default M; a sub-fleet: M4.)')
    c.add_argument('--json', action='store_true', help='print the result as JSON')
    args = ap.parse_args(argv)
    try:
        res = check(args.file, args.prefix, args.config)
    except ValueError as exc:
        print('tree check: ' + str(exc))
        return 2
    print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else render(args.file, res))
    return 0 if res['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
