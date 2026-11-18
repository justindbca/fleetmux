"""relay.progress: the five fixed headings of a slot's PROGRESS.md and the task.json summary."""
from relay_kit import write_files
from fleetmux.relay import progress as P

GOOD = """# PROGRESS

## Done
- wrote the parser (abc1234)
- tests for it

## Doing
- the snapshot round

## Next
- run tests/test_relay_snapshot.py first, then wire the checkpoint

## Dead ends
- none

## Red tests
- `tests/test_relay_snapshot.py::test_secret`: secret file still in the tree
"""


def test_headings_are_the_interface_ones():
    assert P.SECTIONS == ("Done", "Doing", "Next", "Dead ends", "Red tests")


def test_template_parses_ok():
    r = P.parse(P.template())
    assert r.ok and r.red_tests == 0 and all(v == [] for v in r.sections.values())


def test_good_file_parses_with_red_test_ids():
    r = P.parse(GOOD)
    assert r.ok and r.sections["Doing"] == ["the snapshot round"]
    assert r.red_test_items == [("tests/test_relay_snapshot.py::test_secret", "secret file still in the tree")]


def test_missing_out_of_order_duplicate_heading_not_ok():
    assert not P.parse(GOOD.replace("## Next", "## Later")).ok
    swapped = GOOD.replace("## Done", "## TMP").replace("## Doing", "## Done").replace("## TMP", "## Doing")
    assert not P.parse(swapped).ok
    assert not P.parse(GOOD + "\n## Done\n- again\n").ok
    assert not P.parse(GOOD.replace("## Doing", "## In progress")).ok       # the old heading is not accepted


def test_summary_shape_and_clipping():
    s = P.summary(".relay/j/M0/notes/dev-1/PROGRESS.md", P.parse(GOOD), text_max=20)
    assert s == {"path": ".relay/j/M0/notes/dev-1/PROGRESS.md", "ok": True, "done": 2,
                 "doing": ["the snapshot round"], "next": ["run tests/test_rela-"],
                 "red_tests": ["tests/test_relay_sn-"]}
    assert all(ord(ch) < 128 for v in s.values() if isinstance(v, list) for x in v for ch in x)


def test_summary_for_reads_the_newest_slot_file(tmp_path):
    write_files(tmp_path, {".relay/j/M0/notes/dev-1/PROGRESS.md": P.template(),
                           ".relay/j/M0/notes/dev-2/PROGRESS.md": GOOD})
    import os
    os.utime(tmp_path / ".relay/j/M0/notes/dev-1/PROGRESS.md", (1, 1))
    s = P.summary_for(tmp_path, ".relay/j/M0", text_max=200)
    assert s["path"] == ".relay/j/M0/notes/dev-2/PROGRESS.md" and s["done"] == 2
    assert P.summary_for(tmp_path, ".relay/j/M9", text_max=200) is None
