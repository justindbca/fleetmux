"""Minimal applet for the acceptance tests (docs/applet-interface.md section 8, reduced).

It starts one child process, records the child's pid under $FLEETMUX_APPLET_RUN, and keeps it until it is
stopped. It writes nothing outside the fleet: only the run directory and the host Redis.
"""
import subprocess
import sys

from fleetmux.applet import Applet


def main():
    app = Applet.from_env()
    child = subprocess.Popen([sys.executable, '-c', 'import time\nwhile True:\n    time.sleep(1)'],
                             stdin=subprocess.DEVNULL)
    (app.run_dir / 'child.pid').write_text(str(child.pid))
    app.set_phase('serving')

    def step(a):
        if child.poll() is not None:
            raise RuntimeError('child exited')
        return 0.2

    try:
        app.run(step)
    finally:
        child.terminate()
        child.wait(5)


if __name__ == '__main__':
    main()
