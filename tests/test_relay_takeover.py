"""relay.takeover: TAKEOVER.md in the task's directory for the new owner's supervisor (R9)."""
import json

import relay_kit
from relay_kit import write_files
from fleetmux.relay import records as R
from fleetmux.relay import progress as P
from fleetmux.relay.takeover import write_package

PROGRESS = """## Done
- parser (abc1234)

## Doing
- snapshot round

## Next
- wire the checkpoint in fleetmux/relay/checkpoint.py; run tests/test_relay_materialize.py first

## Dead ends
- none

## Red tests
- `tests/test_relay_snapshot.py::test_secret`: still red
"""


def taken(why=R.WHY_LOST):
    rec = R.claim(R.new_task(job="j", id="M3", title="protocol core", weight="heavy", dir=".relay/j/M3",
                             needs=["rust"], base="a" * 40), me="laptop")
    rec = R.checkpoint(rec, commit="b" * 40, snap="c" * 40, kind="phase", phase="design")
    return R.takeover(rec, to="desktop", why=why)


def test_takeover_md_names_the_previous_owner_checkpoint_and_progress(tmp_path):
    write_files(tmp_path, {".relay/j/M3/notes/dev-1/PROGRESS.md": PROGRESS})
    prev = {"node": "laptop", "state": "online", "tasks": [{"id": "M3", "state": "working", "phase": "design"}]}
    md = write_package(tmp_path, taken(), previous=prev, judged="lost after 301 s without a change", by="desktop",
                       settings=relay_kit.settings())
    assert md == tmp_path / ".relay/j/M3/TAKEOVER.md"
    text = md.read_text()
    assert "laptop" in text and "takeover:lost" in text and "lost after 301 s" in text
    assert "relay/j/M3/e1-laptop" in text and "relay/j/M3/e2-desktop" in text
    assert "cccccccccccc" in text and "phase design" in text
    assert "### Next" in text and "wire the checkpoint" in text and "test_secret" in text
    assert "salvage/M3/laptop-e1" in text
    data = json.loads((tmp_path / ".relay/j/M3/takeover.json").read_text())
    assert data["previous"]["node"] == "laptop" and data["why"] == "takeover:lost" and data["epoch"] == 2
    assert all(ord(ch) < 128 for ch in text)


def test_takeover_md_line_limit_comes_from_the_parameter(tmp_path):
    write_files(tmp_path, {".relay/j/M3/notes/dev-1/PROGRESS.md": PROGRESS.replace(
        "- parser (abc1234)", "\n".join(f"- item {i}" for i in range(200)))})
    s = relay_kit.settings()
    md = write_package(tmp_path, taken(), previous=None, judged="goodbye: sleep", by="desktop", settings=s)
    lines = md.read_text().splitlines()
    assert len(lines) == s.takeover_md_max_lines and "cut to" in lines[-1]


def test_takeover_without_or_with_malformed_progress_says_so(tmp_path):
    md = write_package(tmp_path, taken(R.WHY_GOODBYE), previous=None, judged="goodbye: sleep", by="desktop",
                       settings=relay_kit.settings())
    assert "No progress file" in md.read_text() and "takeover:goodbye" in md.read_text()
    write_files(tmp_path, {".relay/j/M3/notes/dev-1/PROGRESS.md": "## Done\n- x\n"})
    md = write_package(tmp_path, taken(), previous=None, judged="x", by="desktop", settings=relay_kit.settings())
    assert "did not parse" in md.read_text()
    assert P.template()                                     # the template is the shape the owner should use
