"""The README recording pipeline: scripts/rec_to_cast.py (fleetmux --record -> asciicast v2) and the committed
recording itself (redacted, shows the scripted banner and both verdicts)."""
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import rec_to_cast  # noqa: E402

CAST = ROOT / 'docs' / 'images' / 'fleetmux-demo.cast'
REC = ROOT / 'docs' / 'images' / 'fleetmux-demo.rec'


def rec(*entries):
    return b''.join(struct.pack('>QI', at, len(data)) + data for at, data in entries)


def test_entries_become_events_with_utf8_joined_and_pauses_capped():
    word = 'verdict — ok'.encode()
    data = rec((0, b'\x1b[1mhello'), (500_000, word[:9]), (9_500_000, word[9:]))  # the dash split in two
    lines = rec_to_cast.convert(data, 80, 24, title='t', idle_limit=2).splitlines()
    header = json.loads(lines[0])
    assert header == {'version': 2, 'width': 80, 'height': 24, 'title': 't', 'idle_time_limit': 2}
    events = [json.loads(x) for x in lines[1:]]
    assert ''.join(e[2] for e in events) == '\x1b[1mhello' + word.decode()
    assert [e[0] for e in events] == [0.0, 0.5, 2.5]  # the 9 s pause is cut to 2 s


def test_malformed_recording_writes_nothing(tmp_path):
    bad = tmp_path / 'bad.rec'
    bad.write_bytes(rec((0, b'abc'))[:-1])  # truncated entry
    out = tmp_path / 'out.cast'
    assert rec_to_cast.main(['--cols', '80', '--rows', '24', str(bad), str(out)]) == 1
    assert not out.exists()


def test_record_script_parses():
    assert subprocess.run(['bash', '-n', str(ROOT / 'scripts' / 'record_demo.sh')]).returncode == 0


def test_committed_recording_is_redacted_and_tells_the_story():
    header = json.loads(CAST.read_text().splitlines()[0])
    assert set(header) <= {'version', 'width', 'height', 'title', 'idle_time_limit'}  # no time, no environment
    text = ''.join(json.loads(x)[2] for x in CAST.read_text().splitlines()[1:])
    plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    assert plain.count('scripted agents, no model calls — the acceptance is real fleetmux code') >= 2
    assert 'REJECTED' in plain and 'artifact not written: worker-1/test_slug.py' in plain
    assert 'PASS' in plain and 'Task status: completed' in plain and 'Traceback' not in plain
    for blob in (CAST.read_bytes(), REC.read_bytes()):
        assert b'/Users/' not in blob and b'/home/' not in blob
        assert not re.search(rb'[\w.+-]+@[\w-]+\.[\w.]+', blob)  # no e-mail address


# ------------------------------------------------------------------ scripts/cast_to_svg.py (.cast -> animated SVG)
SVG = ROOT / 'docs' / 'images' / 'fleetmux-demo.svg'
SMALL_CAST = '\n'.join(json.dumps(x) for x in [
    {'version': 2, 'width': 20, 'height': 3},
    [0.0, 'o', 'hello'],
    [0.5, 'o', '\r\nworld \x1b[31mred\x1b[0m'],
    [0.52, 'o', ''],                                  # no change on screen: no new frame
    [1.0, 'o', '\x1b[2J\x1b[Hbye & <ok> — done'],
]) + '\n'


def small_svg(**kw):
    import cast_to_svg
    header, events = cast_to_svg.load_cast(SMALL_CAST)
    frames = cast_to_svg.frames(header, events)
    return frames, cast_to_svg.render_svg(frames, header['width'], header['height'], **kw)


def test_cast_frames_follow_the_screen_changes():
    frames, _ = small_svg()
    assert [round(t, 2) for t, _ in frames] == [0.0, 0.5, 1.0]
    texts = [''.join(text for row in rows for _, text, _ in row) for _, rows in frames]
    assert 'hello' in texts[0] and 'world red' in texts[1] and 'bye & <ok> — done' in texts[2]


def test_svg_is_one_self_contained_animated_image():
    import xml.etree.ElementTree as ET
    _, svg = small_svg(hold=2.0)
    root = ET.fromstring(svg)  # well-formed XML
    ns = '{http://www.w3.org/2000/svg}'
    assert root.tag == ns + 'svg'
    frames = root.findall('.//' + ns + 'g[@class="frame"]')
    assert len(frames) == 3
    text = ''.join(root.itertext())
    assert 'hello' in text and 'world' in text and 'bye & <ok> — done' in text
    style = ''.join(e.text or '' for e in root.iter(ns + 'style'))
    assert '@keyframes' in style and 'infinite' in style and 'steps(1' in style
    assert re.search(r'animation:[^;]*\b3(\.0+)?s\b', style)  # last frame at 1.0 s + 2.0 s hold = one loop
    lowered = svg.lower()
    assert '<script' not in lowered and 'href' not in lowered and '@import' not in lowered and 'url(' not in lowered
    assert '@font-face' not in lowered and 'foreignobject' not in lowered
    assert re.findall(r'https?://[^"\s]+', svg) == ['http://www.w3.org/2000/svg']  # the namespace, nothing fetched
    assert '#' in svg and 'red' not in style  # colours are written as values, the run carries them
    assert re.search(r'fill="#[0-9a-f]{6}"[^>]*>red<', svg)


def test_svg_width_fits_a_readme(tmp_path):
    import cast_to_svg
    src = tmp_path / 'in.cast'
    src.write_text(SMALL_CAST)
    out = tmp_path / 'out.svg'
    assert cast_to_svg.main([str(src), str(out), '--hold', '1']) == 0
    root = __import__('xml.etree.ElementTree', fromlist=['x']).fromstring(out.read_text())
    assert float(root.get('width')) <= 960 and root.get('viewBox')
    assert cast_to_svg.main([str(tmp_path / 'missing.cast'), str(tmp_path / 'x.svg')]) == 1
    assert not (tmp_path / 'x.svg').exists()


def test_committed_svg_is_redacted_small_and_tells_the_story():
    import xml.etree.ElementTree as ET
    data = SVG.read_bytes()
    assert len(data) <= 5 * 1024 * 1024
    root = ET.fromstring(data)
    text = ''.join(root.itertext())
    assert 'scripted agents, no model calls — the acceptance is real fleetmux code' in text
    assert 'REJECTED' in text and 'PASS' in text and 'Task status: completed' in text
    assert b'<script' not in data.lower() and b'/Users/' not in data and b'/home/' not in data
    assert not re.search(rb'[\w.+-]+@[\w-]+\.[\w.]+', data)
