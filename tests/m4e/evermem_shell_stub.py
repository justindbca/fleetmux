"""Stand-in for the memx EverMemOS shell tab (the one command substitution of the copied roster, design §8.4).

Copied to ``<test root>/bin/evermem_shell_stub.py`` and run as the memx tab ``command``. It opens no socket and
no port, runs no other program, and keeps its own command line equal to the tab argv (so ``tabs.clear``'s
"stop the old copy first" rule, design §31, is really exercised). It writes one JSON line (pid, argv, cwd, HOME)
to ``--log`` and waits for a signal.
"""
import json
import os
import signal
import sys
import time


def main(argv):
    log = argv[argv.index('--log') + 1] if '--log' in argv else None
    rec = {'event': 'start', 'pid': os.getpid(), 'pgid': os.getpgrp(), 'argv': [sys.executable] + sys.argv,
           'cwd': os.getcwd(), 'home': os.environ.get('HOME'), 'at': time.time()}

    def bye(sig, _frame):
        if log:
            with open(log, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'event': 'signal', 'pid': os.getpid(), 'sig': sig, 'at': time.time()}) + '\n')
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, bye)
    if log:
        with open(log, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    while True:
        signal.pause()


if __name__ == '__main__':
    main(sys.argv[1:])
