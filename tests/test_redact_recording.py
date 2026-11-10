"""scripts/redact_recording.py (brief-m03 P-10 / B24, PROTOCOL.md §7.1). Every sample is synthetic, built here;
no recording fixture is read. Linux home paths are assembled from parts so no literal one is in the repo."""
import importlib.util
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/redact_recording.py'
spec = importlib.util.spec_from_file_location('redact_recording', SCRIPT)
redact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(redact)

LINUX_HOME = b'/' + b'home' + b'/'
SECRETS = [
    b'alice.smith+ci@example.co.uk',
    b'/Users/alice/work/proj/notes.md',
    LINUX_HOME + b'bob/.config/tool/cfg.toml',
    b'sk-ant-api03-AbCdEfGhIjKlMnOpQrSt_uv-wx',
    b'ghp_' + b'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8',
    b'0123456789abcdef0123456789ABCDEF01234567',
]


def rec(*entries):
    return b''.join(struct.pack('>QI', off, len(b)) + b for off, b in entries)


def parse(data):
    out, pos = [], 0
    while pos < len(data):
        off, n = struct.unpack_from('>QI', data, pos)
        assert 1 <= n <= 1024 * 1024
        out.append((off, data[pos + 12:pos + 12 + n]))
        assert len(out[-1][1]) == n
        pos += 12 + n
    return out


def changed_only_inside(before, after, spans):
    assert len(before) == len(after)
    for i, (x, y) in enumerate(zip(before, after)):
        if x != y:
            assert any(a <= i < b for a, b in spans), f'byte {i} changed outside a secret: {before[i - 8:i + 8]!r}'


def spans_of(data, needles):
    out = []
    for n in needles:
        i = data.find(n)
        assert i >= 0, n
        out.append((i, i + len(n)))
    return out


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def test_each_secret_shape_replaced_with_same_length_placeholder():
    text = b'cfg: ' + b' | '.join(SECRETS) + b' end\r\n'
    out, counts = redact.redact_stream(text)
    assert len(out) == len(text)
    for s in SECRETS:
        assert s not in out, s
    assert LINUX_HOME not in out and b'/Users/' not in out
    assert b'@example' not in out and b'alice' not in out and b'bob' not in out
    changed_only_inside(text, out, spans_of(text, SECRETS))
    assert counts == {'email': 1, 'path': 2, 'sk': 1, 'github': 1, 'hex': 1}, counts
    assert out.startswith(b'cfg: ') and out.endswith(b' end\r\n')


def test_entry_offsets_lengths_and_parseability_kept():
    entries = [(0, b'hello '), (1500, b'user ' + SECRETS[0] + b'\r\n'), (1500, b'x' * 300), (99_000, SECRETS[3])]
    data = rec(*entries)
    out, _ = redact.redact_record(data)
    assert len(out) == len(data)
    got = parse(out)
    assert [(o, len(b)) for o, b in got] == [(o, len(b)) for o, b in entries]
    assert got[0][1] == b'hello ' and got[2][1] == b'x' * 300
    assert SECRETS[0] not in got[1][1] and SECRETS[3] not in got[3][1]


def test_secret_split_across_entry_boundaries_is_found():
    token, path = SECRETS[4], SECRETS[1]
    entries = [(10, b'tok=' + token[:9]), (20, token[9:] + b' at ' + path[:8]), (30, path[8:] + b'\n')]
    out = parse(redact.redact_record(rec(*entries))[0])
    joined = b''.join(b for _, b in out)
    assert token not in joined and path not in joined and b'alice' not in joined
    assert [len(b) for _, b in out] == [len(b) for _, b in entries]
    assert joined.startswith(b'tok=ghp_') and joined.endswith(b'\n')


def test_escape_sequences_and_utf8_are_left_intact():
    esc = b'\x1b'
    text = (
        esc + b'[1m' + b'mail ' + esc + b'[38;5;196m' + SECRETS[0] + esc + b'[0m\r\n'
        + esc + b']0;' + SECRETS[1] + b'\x07'
        + esc + b']8;;file://host' + SECRETS[2] + esc + b'\\link' + esc + b']8;;' + esc + b'\\'
        # b' ' after the path: M34 ruling, UTF-8 glued to a home path is part of the path
        + '漢字'.encode() + SECRETS[1] + b' ' + 'é ü'.encode() + esc + b'(B' + esc + b'[2026h' + esc + esc + b'[K'
    )
    out, _ = redact.redact_stream(text)
    for s in SECRETS[:3]:
        assert s not in out
    changed_only_inside(text, out, spans_of(text, SECRETS[:3]) + [(text.rfind(SECRETS[1]), text.rfind(SECRETS[1]) + len(SECRETS[1]))])
    out.decode('utf-8')
    for seq in (b'\x1b[1m', b'\x1b[38;5;196m', b'\x1b[0m', b'\x1b]0;', b'\x07', b'\x1b]8;;file://host', b'\x1b\\',
                b'\x1b(B', b'\x1b[2026h', b'\x1b\x1b[K', '漢字'.encode(), 'é ü'.encode()):
        assert seq in out, seq


def test_sgr_final_byte_is_not_eaten_by_an_email():
    text = b'\x1b[1muser@example.org\x1b[0m'
    out, _ = redact.redact_stream(text)
    assert out == b'\x1b[1mxxxx@xxxxxxx.xxx\x1b[0m'


def test_lookalikes_are_not_touched():
    text = (b'task-list sk-short ~/notes.md /usr/local/bin a@b user@host '
            + b'f' * 31 + b' 1a2b3c4 /Users_x /rootless ' + b'g' * 40 + b' 0x' + b'1' * 20)
    out, counts = redact.redact_stream(text)
    assert out == text and counts == {}


def test_redaction_is_idempotent():
    text = b' '.join(SECRETS) + b'\r\n'
    once, _ = redact.redact_stream(text)
    assert redact.redact_stream(once)[0] == once


def test_cli_writes_0600_and_handles_literal_and_empty(tmp_path):
    src, dst = tmp_path / 'in.rec', tmp_path / 'out.rec'
    src.write_bytes(rec((5, b'carol@mbp ~ % ' + SECRETS[5] + b'\r\n')))
    r = run('--literal', 'mbp', src, dst)
    assert r.returncode == 0, r.stderr
    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o600
    (off, body), = parse(dst.read_bytes())
    assert off == 5 and b'mbp' not in body and SECRETS[5] not in body and body.startswith(b'carol@xxx ~ % ')
    assert 'hex 1' in r.stderr and 'literal 1' in r.stderr
    src.write_bytes(b'')
    assert run(src, dst).returncode == 0 and dst.read_bytes() == b''
    assert sorted(p.name for p in tmp_path.iterdir()) == ['in.rec', 'out.rec']


def test_cli_malformed_input_exits_1_and_writes_nothing(tmp_path):
    good = rec((1, b'ok'))
    bad = {
        'short-header': good + b'\x00' * 5,
        'short-body': good + struct.pack('>QI', 2, 10) + b'abc',
        'zero-length': good + struct.pack('>QI', 2, 0),
        'over-1mib': good + struct.pack('>QI', 2, 1024 * 1024 + 1) + b'x' * 16,
    }
    for name, data in bad.items():
        src, dst = tmp_path / f'{name}.rec', tmp_path / f'{name}.out'
        src.write_bytes(data)
        r = run(src, dst)
        assert r.returncode == 1, (name, r.stderr)
        assert 'nothing written' in r.stderr and not dst.exists(), name
    keep = tmp_path / 'keep.out'
    keep.write_bytes(b'previous')
    assert run(tmp_path / 'short-body.rec', keep).returncode == 1 and keep.read_bytes() == b'previous'
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('.redact-')]


def test_cli_usage_exits_2(tmp_path):
    assert run().returncode == 2
    assert run(tmp_path / 'a.rec').returncode == 2
    assert run('--bogus', tmp_path / 'a', tmp_path / 'b').returncode == 2
    assert run('--literal').returncode == 2


# --- M34 calibration: secrets split by escape sequences, non-ASCII home paths, refused sequences -------------

ESC_RE = __import__('re').compile(rb'\x1b\[[0-?]*[ -/]*[@-~]')


def plain(data):
    """The text with CSI sequences removed: what the secret looks like on screen."""
    return ESC_RE.sub(b'', data)


def test_secrets_split_by_sgr_across_entries_are_fully_redacted():
    sgr, reset = b'\x1b[1;31m', b'\x1b[0m'
    pieces = [
        (0, b'mail: ali' + sgr + b'ce.sm'), (1, b'ith@exa' + reset + b'mple.org\r\n'),
        (2, b'key sk-ant-api03-' + b'\x1b[38;5;196m' + b'AbCdEfGh'), (3, b'IjKlMnOp' + reset + b'Qr\r\n'),
        (4, b'gh ghp_a1B2c3D4e5' + sgr + b'F6g7H8i9J0k1L2m3' + reset + b'N4o5P6q7R8\r\n'),
        (5, b'sha 0123456789abcdef' + sgr), (6, b'0123456789ABCDEF' + reset + b'01234567 done\r\n'),
    ]
    data = rec(*pieces)
    out = parse(redact.redact_record(data)[0])
    assert [(o, len(b)) for o, b in out] == [(o, len(b)) for o, b in pieces]
    joined, before = b''.join(b for _, b in out), b''.join(b for _, b in pieces)
    shown = plain(joined)
    for secret in (b'alice.smith', b'example.org', b'AbCdEfGhIjKlMnOpQr', b'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8',
                   b'0123456789abcdef0123456789ABCDEF01234567'):
        assert secret not in shown, secret
    assert ESC_RE.findall(joined) == ESC_RE.findall(before)
    for m in ESC_RE.finditer(before):
        assert joined[m.start():m.end()] == m.group(), m.group()
    assert shown.startswith(b'mail: ') and shown.endswith(b' done\r\n') and b'\r\nkey sk-' in shown
    joined.decode('utf-8')


def test_non_ascii_home_paths_are_redacted_to_valid_utf8_of_same_length():
    texts = [
        'open /Users/josé/项目/文件.txt now\r\n'.encode(),
        b'cd ' + LINUX_HOME + '田中/ドキュメント/a.md'.encode() + b'\r\n',
        'cwd /Users/Zoë\r\n'.encode(),
    ]
    for text in texts:
        out, counts = redact.redact_stream(text)
        assert len(out) == len(text) and counts.get('path') == 1, (text, counts)
        s = out.decode('utf-8')
        for word in ('josé', '项目', '文件', '田中', 'ドキュメント', 'Zoë', 'Users', 'home'):
            assert word not in s, (word, s)
        assert s.endswith('\r\n') and (s.startswith('open ') or s.startswith('cd ') or s.startswith('cwd '))


def test_unsafe_sequences_are_refused_not_partially_redacted(tmp_path):
    import base64
    secret = b'sk-ant-api03-AbCdEfGhIjKlMnOpQrSt'
    bad = {
        'dcs-secret': rec((1, b'\x1bPtmux;' + secret + b'\x1b\\')),
        'apc-secret': rec((1, b'\x1b_G' + b'0123456789abcdef' * 3 + b'\x1b\\')),
        'osc52-secret': rec((1, b'\x1b]52;c;' + base64.b64encode(b'token ' + secret) + b'\x07')),
        'c1-csi': rec((1, b'red \x9b31m' + SECRETS[0])),
        'bad-utf8': rec((1, b'\xff\xfe' + SECRETS[1])),
    }
    for name, data in bad.items():
        src, dst = tmp_path / f'{name}.rec', tmp_path / f'{name}.out'
        src.write_bytes(data)
        r = run(src, dst)
        assert r.returncode == 1, (name, r.returncode, r.stderr)
        assert 'nothing written' in r.stderr and not dst.exists(), name
    fine = rec((1, b'\x1bPq#0;2;0;0;0#0~~@@\x1b\\'), (2, b'\x1b]52;c;' + base64.b64encode(b'hello') + b'\x07'))
    assert redact.redact_record(fine)[0] == fine
    tail = rec((1, b'ok '), (2, '漢'.encode()[:2]))
    assert redact.redact_record(tail)[0] == tail, 'a character cut by the end of the recording is accepted'


def test_home_path_last_component_non_ascii_or_glued_utf8_is_redacted_whole():
    """Supervisor M34 ruling: the whole last component is part of the path, pure non-ASCII or glued to the end."""
    cases = [
        ('ls /Users/x/项目说明\r\n', '项目说明'),
        ('cat /Users/alice/notes.mdé ü\r\n', 'notes.mdé'),
        ('cd ' + LINUX_HOME.decode() + 'bob/文件说明v2版\r\n', '文件说明v2版'),
    ]
    for text, tail in cases:
        data = text.encode()
        out, counts = redact.redact_stream(data)
        s = out.decode('utf-8')
        assert len(out) == len(data) and counts.get('path') == 1, (text, counts)
        assert tail not in s and not any(ch in s for ch in tail if ord(ch) > 127 and ch != 'ü'), (tail, s)
        start = data.index(b'/')
        end = data.index(tail.encode()) + len(tail.encode())
        assert out[start:end] == bytes(c if c == 0x2F else 0x78 for c in data[start:end]), s
        assert out[:start] == data[:start] and out[end:] == data[end:], s


# ---- task6 [M73]: hyphenated UUIDs (8-4-4-4-12 hex, any case) ----

UUID_RE = __import__('re').compile(rb'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')
UUIDS = [
    b'3f2b8c1e-9a4d-4e6f-b1c2-0d9e8f7a6b5c',  # lower case
    b'3F2B8C1E-9A4D-4E6F-B1C2-0D9E8F7A6B5C',  # upper case
    b'3f2B8c1E-9a4D-4E6f-b1C2-0d9E8f7A6b5C',  # mixed case
]
UUID_PLACEHOLDER = b'XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX'


def test_uuid_any_case_becomes_same_length_placeholder_that_is_no_uuid():
    text = b'session ' + b' / '.join(UUIDS) + b' end\r\n'
    out, counts = redact.redact_stream(text)
    assert len(out) == len(text)
    assert counts == {'uuid': 3}, counts
    assert UUID_RE.search(out) is None, out
    assert out == b'session ' + b' / '.join([UUID_PLACEHOLDER] * 3) + b' end\r\n'
    changed_only_inside(text, out, spans_of(text, UUIDS))
    assert redact.redact_stream(out) == (out, {}), 'a redacted UUID is not matched again'


def test_uuid_coexists_with_hex_path_and_literal():
    hex40 = SECRETS[5]
    text = (b'janedo@mbp ~ % claude --resume ' + UUIDS[0] + b'\r\n'
            + b'log /Users/janedo/.claude/projects/' + UUIDS[1] + b'.jsonl sha ' + hex40 + b'\r\n'
            + b'id=' + UUIDS[2] + b'janedo tail\r\n')
    out, counts = redact.redact_stream(text, literals=[b'janedo'])
    assert len(out) == len(text)
    assert UUID_RE.search(out) is None and b'janedo' not in out and hex40 not in out, out
    assert counts == {'uuid': 3, 'path': 1, 'hex': 1, 'literal': 3}, counts
    assert out.startswith(b'xxxxxx@mbp ~ % claude --resume ' + UUID_PLACEHOLDER + b'\r\n')
    assert b'sha ' + b'0' * len(hex40) in out and out.endswith(b'id=' + UUID_PLACEHOLDER + b'xxxxxx tail\r\n')
    out.decode('utf-8')


def test_uuid_split_across_entries_and_styled_keeps_frames():
    u = UUIDS[1]
    sgr, reset = b'\x1b[1;32m', b'\x1b[0m'
    pieces = [(0, b'resume ' + u[:6]), (250, u[6:14] + sgr + u[14:20]), (99_000, u[20:] + reset + b' ok\r\n')]
    data = rec(*pieces)
    out = parse(redact.redact_record(data)[0])
    assert [(o, len(b)) for o, b in out] == [(o, len(b)) for o, b in pieces]
    joined = b''.join(b for _, b in out)
    assert UUID_RE.search(plain(joined)) is None and plain(joined) == b'resume ' + UUID_PLACEHOLDER + b' ok\r\n'
    assert sgr in joined and reset in joined


def test_uuid_lookalikes_and_glued_uuids():
    near = (b'8-4-4-4-11 3f2b8c1e-9a4d-4e6f-b1c2-0d9e8f7a6b5 '
            b'non-hex 3f2b8c1e-9a4d-4e6f-b1c2-0d9e8f7a6g5c '
            b'no-dash 3f2b8c1e9a4d4e6f-b1c2-0d9e8f7a6b5c ' + UUID_PLACEHOLDER)
    out, counts = redact.redact_stream(near)
    assert out == near and counts == {}, (out, counts)
    glued = b'xx' + UUIDS[0] + b'yy ab' + UUIDS[1] + b'cd'  # glued to letters or more hex: still no UUID left
    out, counts = redact.redact_stream(glued)
    assert len(out) == len(glued) and UUID_RE.search(out) is None and counts == {'uuid': 2}, (out, counts)


def test_uuid_in_title_redacted_and_in_opaque_payload_refused(tmp_path):
    import base64
    title = rec((1, b'\x1b]0;claude ' + UUIDS[0] + b'\x07'))
    out = parse(redact.redact_record(title)[0])[0][1]
    assert out == b'\x1b]0;claude ' + UUID_PLACEHOLDER + b'\x07'
    for name, data in {'dcs-uuid': rec((1, b'\x1bPtmux;' + UUIDS[1] + b'\x1b\\')),
                       'osc52-uuid': rec((1, b'\x1b]52;c;' + base64.b64encode(b'id ' + UUIDS[2]) + b'\x07'))}.items():
        src, dst = tmp_path / f'{name}.rec', tmp_path / f'{name}.out'
        src.write_bytes(data)
        r = run(src, dst)
        assert r.returncode == 1 and 'nothing written' in r.stderr and not dst.exists(), (name, r.stderr)


def test_cli_literal_is_exact_case_sensitive_bytes(tmp_path):
    """`--literal` matches its exact bytes only: every case variant to remove is passed as its own `--literal`."""
    src, dst = tmp_path / 'in.rec', tmp_path / 'out.rec'
    src.write_bytes(rec((7, b'janedo Janedo JANEDO ' + UUIDS[0] + b'\r\n')))
    r = run('--literal', 'janedo', src, dst)
    assert r.returncode == 0, r.stderr
    (off, body), = parse(dst.read_bytes())
    assert off == 7 and body == b'xxxxxx Janedo JANEDO ' + UUID_PLACEHOLDER + b'\r\n', body
    assert 'literal 1' in r.stderr and 'uuid 1' in r.stderr, r.stderr
    r = run('--literal', 'janedo', '--literal', 'Janedo', '--literal', 'JANEDO', src, dst)
    assert r.returncode == 0, r.stderr
    (off, body), = parse(dst.read_bytes())
    assert body == b'xxxxxx xxxxxx xxxxxx ' + UUID_PLACEHOLDER + b'\r\n' and 'literal 3' in r.stderr, (body, r.stderr)
