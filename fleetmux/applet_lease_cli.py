"""Lease commands of ``fleetmux applet`` (docs/applet-interface.md section 5).

``pause`` / ``resume`` / ``revoke`` / ``answer`` hand one operator request to the host daemon, which applies it
on its tick (``applet_lease.answer_ctl``) and answers; ``leases`` reads the records directly.
Exit codes as in ``applet_cli``: 0 ok, 2 rejected, 3 pending (the daemon did not answer in time).
"""
from fleetmux import applet_cli, applet_lease


def _ask(r, req, wait):
    out = applet_lease.ask(r, req, wait=wait)
    code = {'ok': applet_cli.OK, 'pending': applet_cli.PENDING}.get(out.get('status'), applet_cli.REJECTED)
    return code, out


def _known(cfg, name):
    return name in applet_cli.declared(cfg)


def cmd_pause(action):
    def run(cfg, r, args):
        if not _known(cfg, args.name):
            return applet_cli.rejected('the roster declares no applet ' + args.name)
        return _ask(r, {'action': action, 'name': args.name, 'note': args.note or ''}, args.wait)
    return run


def cmd_leases(cfg, r, args):
    return applet_cli.OK, {'status': 'ok', 'leases': applet_lease.lease_rows(r, all_=args.all)}


def cmd_set(cfg, r, args):
    if not _known(cfg, args.name):
        return applet_cli.rejected('the roster declares no applet ' + args.name)
    return _ask(r, {'action': 'set', 'name': args.name, 'key': args.key, 'value': args.value}, args.wait)


def cmd_reviews(cfg, r, args):
    from fleetmux import applet_ops
    return applet_cli.OK, {'status': 'ok', 'reviews': applet_ops.review_rows(r, all_=args.all)}


def cmd_revoke(cfg, r, args):
    return _ask(r, {'action': 'revoke', 'lease': args.lease, 'note': args.note}, args.wait)


def cmd_answer(cfg, r, args):
    return _ask(r, {'action': 'answer', 'id': args.op_id, 'decision': args.decision, 'choice': args.choice,
                    'note': args.note or ''}, args.wait)


def cmd_notices(cfg, r, args):
    if not applet_cli._known(cfg, r, args.name):
        return applet_cli.rejected('no applet ' + args.name + ' on this host')
    return applet_cli.OK, {'status': 'ok', 'applet': args.name,
                           'notices': applet_lease.notices(r, args.name, args.last)}


def _wait(p):
    p.add_argument('--wait', type=float, default=applet_lease.SETTINGS['ctl_wait_s'],
                   help='seconds to wait for the host daemon (default %(default)s)')


def _setup_pause(p):
    p.add_argument('name')
    p.add_argument('--note', default='')
    _wait(p)


def _setup_leases(p):
    p.add_argument('--all', action='store_true', help='every kept record, not only open leases')


def _setup_notices(p):
    p.add_argument('name')
    p.add_argument('--last', type=int, default=None, help='only the newest n notices')


def _setup_set(p):
    p.add_argument('name')
    p.add_argument('key', choices=('hot_add_max',))
    p.add_argument('value', type=int)
    _wait(p)


def _setup_reviews(p):
    p.add_argument('--all', action='store_true', help='every kept review, not only running ones')


def _setup_revoke(p):
    p.add_argument('lease')
    p.add_argument('--note', required=True)
    _wait(p)


def _setup_answer(p):
    p.add_argument('op_id')
    p.add_argument('decision', choices=('approve', 'reject'))
    p.add_argument('--choice', type=int, default=None)
    p.add_argument('--note', default='')
    _wait(p)


applet_cli.register('pause', cmd_pause('pause'), _setup_pause, 'pause an applet: its lease requests are refused')
applet_cli.register('resume', cmd_pause('resume'), _setup_pause, 'resume a paused applet')
applet_cli.register('leases', cmd_leases, _setup_leases, 'open (or, with --all, all kept) lease records')
applet_cli.register('set', cmd_set, _setup_set, 'change hot_add_max while the fleet runs (written back to the roster)')
applet_cli.register('reviews', cmd_reviews, _setup_reviews, 'running (or, with --all, all kept) fix reviews')
applet_cli.register('revoke', cmd_revoke, _setup_revoke, 'end one lease now (operator)')
applet_cli.register('notices', cmd_notices, _setup_notices,
                    'the full text of the notices the host kept: applet.notify and human escalations (the audit line '
                    'and the card are cut short)')
applet_cli.register('answer', cmd_answer, _setup_answer,
                    "the human owner's answer to a delete or human escalation (agents never run this)")
