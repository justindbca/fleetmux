"""CI Linux: the codex start helper must not depend on what the host's /bin/echo prints.

`_start_codex` (tests/test_codex_queue_versions.py) used `/bin/echo` as the claude binary, and start really runs
`<claude> --version`. macOS echo prints `--version` (no version: not judged); the Ubuntu runner's echo printed its own
version `0.10.0`, below deps.yaml's claude minimum, and start refused (CI run 44361071214). Here `/bin/echo --version`
answers like that runner's echo on every host; the original test must still pass.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_codex_queue_versions as versions  # noqa: E402  (module import: its tests are not collected twice)
from fleetmux import fleet  # noqa: E402


def test_start_pins_the_probe_with_a_versioned_host_echo(tmp_path, monkeypatch, capsys):
    real_run = subprocess.run

    def run(argv, *a, **kw):
        if list(argv) == ['/bin/echo', '--version']:  # what the Ubuntu runner's echo answers
            return subprocess.CompletedProcess(argv, 0, stdout='echo (uutils coreutils) 0.10.0\n', stderr='')
        return real_run(argv, *a, **kw)
    monkeypatch.setattr(fleet.subprocess, 'run', run)
    versions.test_start_pins_the_probe_in_config(tmp_path, monkeypatch, capsys)
