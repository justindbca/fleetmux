"""CI Linux: the attaching-start settings test must not depend on the host's /bin layout.

`join.start_settings` normalises a `--claude` path with `Path.resolve()`. On merged-/usr Linux (Ubuntu) `/bin` is a
symlink to `usr/bin`, so a test that typed `/bin/claude` got `/usr/bin/claude` back (CI run 44361071214); on macOS
`/bin` is a real directory. Here join's paths resolve as on a merged-/usr host; the original test must still pass.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_attach_gaps as attach_gaps  # noqa: E402  (module import: its tests are not collected twice)
from fleetmux import join  # noqa: E402


class MergedUsrPath(type(Path())):
    """A Path whose resolve() follows /bin -> /usr/bin, like a merged-/usr Linux host."""

    def resolve(self, strict=False):
        resolved = super().resolve(strict)
        if resolved.parts[:2] == ('/', 'bin'):
            return type(self)('/usr', *resolved.parts[1:])
        return resolved


def test_merged_usr_path_stand_in():
    assert str(MergedUsrPath('/bin/claude').resolve()) == '/usr/bin/claude'
    assert str(MergedUsrPath('/opt/x/claude').resolve()) == '/opt/x/claude'


def test_settings_are_forwarded_on_a_merged_usr_host(tmp_path, monkeypatch):
    monkeypatch.setattr(join, 'Path', MergedUsrPath)
    attach_gaps.test_supervisor_and_worker_settings_are_forwarded(tmp_path)
