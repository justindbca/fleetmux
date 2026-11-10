"""Read-only control regressions; every secret and run is synthetic."""

import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest
import redis

from cmux_fleet import control_view as control


def sid(value):
    return tuple(map(int, value.split("-")))


class Store:
    def __init__(self):
        self.hashes = {}
        self.lists = {}
        self.values = {}
        self.rows = []
        self.added = 0
        self.last_id = "0-0"
        self.clients = []
        self.calls = []
        self.changed = threading.Event()

    def add(self, stream_id, **fields):
        self.rows.append((stream_id, fields))
        self.added += 1
        self.last_id = stream_id
        self.changed.set()


class Reader:
    """Only read methods exist: any work ACK or mutation immediately fails."""

    def __init__(self, store, blocking=False):
        self.store, self.blocking, self.closed = store, blocking, False
        store.clients.append(self)

    def ping(self):
        return True

    def hgetall(self, key):
        self.store.calls.append(("hgetall", key))
        return copy.deepcopy(self.store.hashes.get(key, {}))

    def get(self, key):
        self.store.calls.append(("get", key))
        return self.store.values.get(key)

    def llen(self, key):
        self.store.calls.append(("llen", key))
        return len(self.store.lists.get(key, []))

    def scan_iter(self, match):
        prefix = match[:-1]
        return iter([key for key in self.store.hashes if key.startswith(prefix)])

    def xinfo_stream(self, key):
        rows = self.store.rows
        return {"first-entry": rows[0] if rows else None,
                "last-generated-id": self.store.last_id,
                "length": len(rows), "entries-added": self.store.added}

    def xrange(self, key, min="-", max="+", count=None):
        self.store.calls.append(("xrange", key))
        def after_start(value):
            if min == "-":
                return True
            return sid(value) > sid(min[1:]) if min.startswith("(") else sid(value) >= sid(min)
        return [(i, copy.deepcopy(f)) for i, f in self.store.rows
                if after_start(i) and (max == "+" or sid(i) <= sid(max))][:count]

    def xread(self, streams, count=None, block=None):
        self.store.calls.append(("xread", self.blocking, block))
        assert self.blocking and 0 < block <= 1000
        key, after = next(iter(streams.items()))
        rows = self.xrange(key, min="(" + after, count=count)
        if not rows:
            self.store.changed.clear()
            self.store.changed.wait(min(block / 1000, .05))
            rows = self.xrange(key, min="(" + after, count=count)
        return [(key, rows)] if rows else []

    def close(self):
        self.closed = True


@pytest.fixture
def observed(monkeypatch, tmp_path):
    store = Store()
    cfg = {"run": str(tmp_path / "run-test"), "redis_socket": "/tmp/cmux-fleet-test.sock",
           "workers": ["worker-1"]}
    monkeypatch.setattr(control, "_open_client",
                        lambda cfg, *, blocking=False: Reader(store, blocking))
    return store, cfg, "fx:v2:run:run-test"


def seed(store, prefix):
    body = {"instruction": "报告 /tmp/report.md\nprint(1/2)\n" + "中文" * 300,
            "payload": {"request_id": "business-id", "attempt": 17,
                        "nested": [{"Authorization": "Bearer SYNTHETIC_SECRET",
                                    "token_count": 42}]}}
    store.hashes[prefix + ":message:m-one"] = {
        "run_id": "run-test", "slot": "worker-1", "message_id": "m-one",
        "request_id": "request:one", "delivery_id": "d-one", "generation": "1",
        "session_id": "session-one", "stream_id": "10-0", "fence": "7",
        "created_at": "1000", "deadline_at": "9999999999999", "received_at": "1300",
        "body": json.dumps(body, ensure_ascii=False),
        "result": json.dumps({"reply": "OK", "details": {"code": "print('<tag>')"}}),
        "status": "committed"}
    store.hashes[prefix + ":lease:worker-1"] = {
        "run_id": "run-test", "generation": "1", "session_id": "session-one",
        "message_id": "m-one", "current_delivery": "d-one", "status": "received"}
    store.hashes["fx:agent:worker-1"] = {
        "generation": "1", "label": "worker-1.1", "state": "busy", "request": "request:one",
        "ctx_used": "20", "ctx_window": "100", "busy_since": str(time.time() - 3)}
    store.add("20-0", event="received", slot="worker-1", run_id="run-test",
              message_id="m-one", delivery_id="d-one", at="1300")
    return body


def test_full_nested_details_cursor_filters_and_no_mutation(observed):
    store, cfg, prefix = observed
    body = seed(store, prefix)
    before = copy.deepcopy((store.hashes, store.lists, store.values, store.rows))
    events = list(control.iter_events(cfg, request_id="request:one", slot="worker-1",
                                     generation=1, event="received", level="info"))
    assert len(events) == 1
    out = events[0]
    assert out["stream_id"] == out["audit_id"] == "20-0"
    assert out["delivery_stream_id"] == "10-0" and out["generation"] == 1
    assert out["body"]["instruction"] == body["instruction"]
    assert out["body"]["payload"]["nested"][0]["Authorization"] == "***"
    assert out["body"]["payload"]["request_id"] == "business-id"
    assert out["result"]["details"]["code"] == "print('<tag>')"
    assert out["message"]["fence"] == 7 and out["label"] == "worker-1.1"
    assert out["at"] == 1300
    assert (store.hashes, store.lists, store.values, store.rows) == before
    assert list(control.iter_events(cfg, after="20-0")) == []
    for filters in ({"request_id": "other"}, {"generation": 0}, {"event": "failed"},
                    {"slot": "supervisor"}, {"level": "error"}):
        assert list(control.iter_events(cfg, **filters)) == []
    assert all(c.closed for c in store.clients)


def test_stale_delivery_does_not_borrow_current_generation(observed):
    store, cfg, prefix = observed
    seed(store, prefix)
    store.rows[0][1]["delivery_id"] = "d-old"
    out = next(control.iter_events(cfg))
    assert out["generation"] is None and out["session_id"] is None
    assert out["message"]["generation"] == 1
    assert out["enrichment"]["delivery_matches"] is False
    assert list(control.iter_events(cfg, generation=1)) == []


def test_deep_json_controls_html_and_known_config_secrets_are_safe(observed):
    store, cfg, prefix = observed
    seed(store, prefix)
    cfg["api_key"] = "CONFIG_SECRET_SENTINEL"
    deep = {"code": "<div>中文</div>", "secret": "SYNTHETIC_DEEP_SECRET"}
    for _ in range(30):
        deep = {"business": deep}
    store.hashes[prefix + ":message:m-one"]["body"] = json.dumps({
        "deep": deep, "text": "CONFIG_SECRET_SENTINEL\n\x1b]0;bad\x07\r\x00\ud800",
        "notes": '{"Authorization":"Bearer JSON_SECRET","path":"/tmp/report.md"}'})
    out = next(control.iter_events(cfg))
    encoded = json.dumps(out, ensure_ascii=False)
    assert all(secret not in encoded for secret in
               ("CONFIG_SECRET_SENTINEL", "SYNTHETIC_DEEP_SECRET", "JSON_SECRET"))
    node = out["body"]["deep"]
    for _ in range(30):
        node = node["business"]
    assert node == {"code": "<div>中文</div>", "secret": "***"}
    assert "\\x1b" in out["body"]["text"] and "\\ud800" in out["body"]["text"]
    assert "\x1b" not in encoded and "\ud800" not in encoded
    assert "/tmp/report.md" in out["body"]["notes"]


def test_embedded_json_headers_preserve_business_error_details(observed):
    store, cfg, prefix = observed
    seed(store, prefix)
    text = ('error {"Authorization": "Bearer SYNTHETIC_SECRET", "path": "/tmp/report.md",'
            ' "code": "print(1/2)", "token_count": 42}\n'
            'Cookie: a=COOKIE_SECRET; b=SECOND_SECRET\n'
            'password="PASSWORD SECRET"; code=print(1/2)')
    store.hashes[prefix + ":message:m-one"]["uncertain_reason"] = text
    out = next(control.iter_events(cfg))
    safe = out["message"]["uncertain_reason"]
    for secret in ("SYNTHETIC_SECRET", "COOKIE_SECRET", "SECOND_SECRET", "PASSWORD SECRET"):
        assert secret not in safe
    assert "/tmp/report.md" in safe and "print(1/2)" in safe and "42" in safe


def test_redaction_preserves_schema_and_filters_use_recorded_id(observed):
    store, cfg, prefix = observed
    seed(store, prefix)
    cfg["password"] = "request:one"
    out = list(control.iter_events(cfg, request_id="request:one"))
    assert len(out) == 1 and out[0]["request_id"] == "***"
    cfg["password"] = "generation"
    out = next(control.iter_events(cfg))
    assert out["generation"] == 1


@pytest.mark.parametrize("kwargs", [
    {"after": "$"}, {"after": "1-2\n"}, {"limit": 0}, {"limit": True},
    {"generation": -1}, {"generation": "1"}, {"slot": "bad:slot"},
    {"request_id": "../bad"}, {"event": "\x1b"}, {"level": "bogus"},
    {"follow": "yes"}, {"level": []}, {"after": "9" * 50 + "-0"},
])
def test_invalid_filters_rejected_before_connect(observed, kwargs):
    store, cfg, _ = observed
    with pytest.raises(ValueError):
        list(control.iter_events(cfg, **kwargs))
    assert not store.clients


def test_run_id_and_private_socket_validation(monkeypatch, tmp_path):
    cfg = {"run": str(tmp_path / "fallback"), "redis_socket": "/tmp/cmux-fleet-none.sock"}
    assert control._settings(cfg).run_id == "fallback"
    cfg["run_id"] = "explicit"
    assert control._settings(cfg).prefix == "fx:v2:run:explicit"
    for socket in ("localhost:6379", "/tmp/other.sock", "/tmp/cmux-fleet-a/../b.sock"):
        cfg["redis_socket"] = socket
        with pytest.raises(ValueError, match="private"):
            control.snapshot(cfg)


def test_snapshot_state_context_queue_unknown_and_no_config_dump(observed):
    store, cfg, prefix = observed
    seed(store, prefix)
    cfg["auth"] = {"password": "CONFIG_SECRET_SENTINEL"}
    store.hashes["fx:service"] = {"paused": "1", "reason": "operator pause"}
    store.values["fx:daemon:beat"] = str(time.time())
    store.lists[prefix + ":pending:worker-1"] = ["m-next", "m-later"]
    out = control.snapshot(cfg)
    assert out["socket"]["available"] is True and out["daemon"]["paused"] is True
    worker = out["slots"]["worker-1"]
    assert worker["label"] == "worker-1.1" and worker["context"]["ratio"] == .2
    assert worker["queue_depth"] == 2 and worker["current_request"] == "request:one"
    assert worker["deadline_at"] == 9999999999999
    assert worker["elapsed_s"] >= 3
    assert out["slots"]["supervisor"]["label"] is None
    assert "CONFIG_SECRET_SENTINEL" not in json.dumps(out)
    assert all(c.closed for c in store.clients)


def test_socket_failure_clear_safe_and_does_not_fallback(monkeypatch, tmp_path):
    cfg = {"run": str(tmp_path / "run"), "redis_socket": "/tmp/cmux-fleet-none.sock",
           "api_key": "SYNTHETIC_SECRET"}
    def fail(*args, **kwargs):
        raise redis.ConnectionError("unreachable SYNTHETIC_SECRET\x1b")
    monkeypatch.setattr(control, "_open_client", fail)
    out = control.snapshot(cfg)
    assert out["socket"]["available"] is False
    assert "SYNTHETIC_SECRET" not in str(out) and "\x1b" not in str(out)
    with pytest.raises(control.ControlError, match="private Redis socket"):
        list(control.iter_events(cfg))


def test_export_limit_scans_filtered_records_without_repeating(observed):
    store, cfg, _ = observed
    for n in range(1, 9):
        store.add("%d-0" % n, event="failed" if n % 2 else "committed", slot="worker-1")
    assert [e["audit_id"] for e in control.iter_events(cfg, event="committed", limit=2)] == ["2-0", "4-0"]


def test_trim_gap_is_explicit_and_replay_cursor_recovers(observed):
    store, cfg, _ = observed
    for n in range(1, 5):
        store.add("%d-0" % n, event="committed")
    store.rows = store.rows[2:]
    out = list(control.iter_events(cfg, after="1-0"))
    assert out[0]["event"] == "control.gap"
    assert out[0]["resume_after"] == "1-0" and out[0]["first_available_id"] == "3-0"
    assert [e["audit_id"] for e in out[1:]] == ["3-0", "4-0"]


def test_empty_trimmed_stream_still_reports_missing_audit(observed):
    store, cfg, _ = observed
    store.add("1-0", event="committed")
    store.rows.clear()
    out = list(control.iter_events(cfg))
    assert len(out) == 1 and out[0]["event"] == "control.gap"
    assert out[0]["first_available_id"] is None and out[0]["last_generated_id"] == "1-0"


def test_reported_interior_deletion_gap_is_not_repeated_per_batch(observed, monkeypatch):
    store, cfg, _ = observed
    for n in range(1, 6):
        store.add("%d-0" % n, event="committed")
    store.rows = [row for row in store.rows if row[0] != "4-0"]
    original = Reader.xinfo_stream
    def deleted(self, key):
        return dict(original(self, key), **{"max-deleted-entry-id": "4-0"})
    monkeypatch.setattr(Reader, "xinfo_stream", deleted)
    monkeypatch.setattr(control, "BATCH_SIZE", 1)
    out = list(control.iter_events(cfg, after="1-0"))
    assert sum(record["event"] == "control.gap" for record in out) == 1
    assert [record["audit_id"] for record in out[1:]] == ["2-0", "3-0", "5-0"]


def test_follow_uses_owned_finite_block_and_no_repeats(observed):
    store, cfg, _ = observed
    store.add("1-0", event="queued")
    gen = control.iter_events(cfg, follow=True, limit=1)
    assert next(gen)["audit_id"] == "1-0"
    store.add("2-0", event="committed")
    assert next(gen)["audit_id"] == "2-0"
    gen.close()
    assert any(call[0] == "xread" for call in store.calls)
    assert len(store.clients) == 2 and all(c.closed for c in store.clients)


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.005)


class SlowOutput:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.text = ""

    def write(self, value):
        self.entered.set()
        self.release.wait()
        self.text += value

    def flush(self):
        pass


def test_slow_renderer_nonblocking_bounded_queue_gap_and_stop(observed):
    store, cfg, _ = observed
    cfg["control_queue_size"] = 2
    output = SlowOutput()
    started = time.monotonic()
    handle = control.start_control(cfg, stream=output)
    assert time.monotonic() - started < .1
    assert output.entered.wait(1)
    for n in range(1, 30):
        store.add("%d-0" % n, event="committed", slot="worker-1")
    until(lambda: handle.cursor == "29-0")
    assert handle.queue_depth <= 2
    started = time.monotonic()
    assert handle.stop(timeout=.02) is False
    assert time.monotonic() - started < .5  # bounded by the timeout, not the blocked writer (hosted CI is slower)
    output.release.set()
    assert handle.stop(timeout=1)
    assert "control.gap" in output.text and "resume_after" in output.text
    assert len(store.rows) == 29


def test_broken_output_reports_failure_and_reader_keeps_running(observed, monkeypatch):
    store, cfg, _ = observed
    fallback = io.StringIO()
    monkeypatch.setattr(control.sys, "__stderr__", fallback)
    class Broken:
        def write(self, value):
            raise BrokenPipeError("synthetic output failure")
    handle = control.start_control(cfg, stream=Broken())
    until(lambda: bool(handle.errors))
    store.add("1-0", event="committed")
    until(lambda: handle.cursor == "1-0")
    assert handle.stop()
    assert "control.output_error" in fallback.getvalue()
    assert "committed" in fallback.getvalue()


def test_heartbeats_suppressed_and_narrow_chinese_timeline_safe(observed):
    store, cfg, _ = observed
    cfg["control_width"] = 20
    for n in range(1, 20):
        store.add("%d-0" % n, event="heartbeat", slot="worker-1")
    store.add("20-0", event="failed", slot="worker-1", error="中文\x1b]0;bad\x07<div>")
    output = io.StringIO()
    handle = control.start_control(cfg, stream=output)
    until(lambda: handle.cursor == "20-0")
    assert handle.stop()
    assert "heartbeat" not in output.getvalue()
    assert "\x1b" not in output.getvalue()
    assert "failed" in output.getvalue()
    assert all(control._cell_width(line) <= 20 for line in output.getvalue().splitlines())


def test_optional_archive_full_safe_details_and_disk_full_visible(observed, monkeypatch):
    store, cfg, prefix = observed
    seed(store, prefix)
    cfg["control_archive"] = True
    Path(cfg["run"]).mkdir()
    output = io.StringIO()
    handle = control.start_control(cfg, stream=output)
    until(lambda: handle.cursor == "20-0")
    assert handle.stop()
    archive = Path(cfg["run"]) / "control-audit.jsonl"
    data = json.loads(archive.read_text())
    assert len(data["body"]["instruction"]) > 600
    assert "SYNTHETIC_SECRET" not in archive.read_text()
    def full(*args):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(control, "_append_archive", full)
    output = io.StringIO()
    handle = control.start_control(cfg, stream=output)
    until(lambda: bool(handle.errors))
    assert handle.stop()
    assert "control.archive_error" in output.getvalue() and "28" in output.getvalue()


def test_missing_message_malformed_detail_and_explicit_audit_identity(observed):
    store, cfg, _ = observed
    raw = {"generation": "3", "request_id": "req-known", "event": "result",
           "slot": "worker-1", "message_id": "m-missing", "level": "error",
           "detail": '{"error":"raw ' + "long " * 300 + '"}'}
    store.add("1-0", **raw)
    out = next(control.iter_events(cfg, generation=3, request_id="req-known", level="error"))
    assert out["message"] is None and out["generation"] == 3
    assert len(out["detail"]["error"]) > 1200
    assert out["result"] is None
    store.rows[0][1]["detail"] = '{"broken": ANSI\x1b'
    out = next(control.iter_events(cfg))
    assert out["detail"] == '{"broken": ANSI\\x1b'


def test_follow_filter_does_not_rescan_unmatched_entries(observed):
    store, cfg, _ = observed
    store.add("1-0", event="heartbeat")
    store.add("2-0", event="failed")
    gen = control.iter_events(cfg, follow=True, event="failed", limit=1)
    assert next(gen)["audit_id"] == "2-0"
    store.add("3-0", event="heartbeat")
    store.add("4-0", event="failed")
    assert next(gen)["audit_id"] == "4-0"
    gen.close()
    audit_reads = [call for call in store.calls if call[:1] == ("hgetall",)]
    assert not audit_reads  # these records have no message/slot references


def test_finite_export_freezes_high_watermark_and_detects_trim_during_scan(observed, monkeypatch):
    store, cfg, _ = observed
    for n in range(1, 5):
        store.add("%d-0" % n, event="committed")
    monkeypatch.setattr(control, "BATCH_SIZE", 1)
    gen = control.iter_events(cfg, limit=100)
    assert next(gen)["audit_id"] == "1-0"
    store.add("5-0", event="committed")
    store.rows = store.rows[2:]  # lose 2-0 before the next read
    remaining = list(gen)
    assert remaining[0]["event"] == "control.gap"
    assert [r["audit_id"] for r in remaining[1:]] == ["3-0", "4-0"]


def test_requested_heartbeats_visible_and_invalid_options_start_no_threads(observed):
    store, cfg, _ = observed
    store.add("1-0", event="heartbeat")
    cfg["control_filters"] = {"event": "heartbeat"}
    output = io.StringIO()
    handle = control.start_control(cfg, stream=output)
    until(lambda: handle.cursor == "1-0")
    assert handle.stop() and "heartbeat" in output.getvalue()
    for invalid in ({"control_queue_size": 0}, {"control_status_interval": 1},
                    {"control_heartbeats": "yes"}, {"control_width": 0},
                    {"control_archive": "/tmp/elsewhere"}, {"control_filters": {"follow": True}},
                    {"workers": ["bad:slot"]}):
        with pytest.raises(ValueError):
            control.start_control(dict(cfg, **invalid), stream=output)


def test_renderer_crash_falls_back_and_observer_internal_failure_visible(observed, monkeypatch):
    store, cfg, _ = observed
    fallback = io.StringIO()
    monkeypatch.setattr(control.sys, "__stderr__", fallback)
    original = control._render
    def crash_once(record, width):
        if record.get("event") == "control.state":
            raise RuntimeError("synthetic renderer failure")
        return original(record, width)
    monkeypatch.setattr(control, "_render", crash_once)
    handle = control.start_control(cfg, stream=io.StringIO())
    until(lambda: bool(handle.errors))
    store.add("1-0", event="failed")
    until(lambda: handle.cursor == "1-0")
    assert handle.stop()
    assert "control.output_error" in fallback.getvalue() and "failed" in fallback.getvalue()
    monkeypatch.setattr(control, "_render", original)
    def bad_snapshot(cfg):
        raise RuntimeError("synthetic snapshot failure")
    monkeypatch.setattr(control, "snapshot", bad_snapshot)
    output = io.StringIO()
    handle = control.start_control(cfg, stream=output)
    assert handle.stop()
    assert "control.observer_error" in output.getvalue()


def test_v1_import_path_has_no_control_side_effects():
    result = subprocess.run([sys.executable, "-c",
        "import sys, threading; import cmux_fleet.fleet; "
        "assert 'cmux_fleet.control_view' not in sys.modules; "
        "import cmux_fleet.control_view; assert len(threading.enumerate()) == 1"],
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def private_redis():
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server unavailable")
    with tempfile.TemporaryDirectory(prefix="control-v2-") as directory:
        socket = "/tmp/cmux-fleet-control-test-" + uuid.uuid4().hex[:12] + ".sock"
        logpath = Path(directory) / "redis.log"
        with logpath.open("wb") as log:
            process = subprocess.Popen([binary, "--port", "0", "--unixsocket", socket,
                "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
                "--dir", directory], stdout=log, stderr=subprocess.STDOUT)
        client = redis.Redis(unix_socket_path=socket, decode_responses=True,
                             socket_connect_timeout=.5, socket_timeout=2)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    reason = logpath.read_text()
                    if "Operation not permitted" in reason and "socket" in reason:
                        pytest.skip("sandbox cannot bind temporary private Redis: " + reason)
                    pytest.fail("temporary Redis exited: " + reason)
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    time.sleep(.02)
            else:
                pytest.fail("temporary private Redis did not become reachable")
            yield client, {"run": directory, "run_id": "control-" + uuid.uuid4().hex,
                           "redis_socket": socket, "workers": ["worker-1"]}
        finally:
            client.close()
            client.connection_pool.disconnect()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            Path(socket).unlink(missing_ok=True)


def test_real_redis_replay_full_result_snapshot_and_no_work_ack(private_redis):
    from cmux_fleet.message_bus import MessageBus
    r, cfg = private_redis
    bus = MessageBus(r, cfg["run_id"])
    bus.register_slot("worker-1", 0, "session-zero")
    mid = bus.enqueue("worker-1", {"instruction": "中文" * 1000,
        "headers": {"Authorization": "Bearer SYNTHETIC_SECRET"}}, request_id="request:real")
    bus.offer("worker-1")
    envelope = bus.receive("worker-1", "consumer-test", generation=0, session_id="session-zero")
    before = r.xpending(bus.inbox_key("worker-1"), bus.slot_group("worker-1"))
    all_keys = sorted(r.scan_iter())
    dumps = {k: r.dump(k) for k in all_keys}
    events = list(control.iter_events(cfg, request_id="request:real"))
    snap = control.snapshot(cfg)
    assert events[-1]["generation"] == 0 and len(events[-1]["body"]["instruction"]) == 2000
    assert "SYNTHETIC_SECRET" not in json.dumps(events)
    assert snap["slots"]["worker-1"]["current_request"] == "request:real"
    assert r.xpending(bus.inbox_key("worker-1"), bus.slot_group("worker-1")) == before
    assert sorted(r.scan_iter()) == all_keys and {k: r.dump(k) for k in all_keys} == dumps
    bus.commit("worker-1", envelope, {"reply": "OK", "detail": "full" * 300})
    tail = list(control.iter_events(cfg, after=events[-1]["audit_id"], request_id="request:real"))
    assert len(tail) == 1 and tail[0]["event"] == "committed"
    assert tail[0]["result"]["detail"] == "full" * 300
    assert tail[0]["message"]["message_id"] == mid


def test_real_redis_follow_wakes_independent_connection(private_redis):
    r, cfg = private_redis
    prefix = "fx:v2:run:" + cfg["run_id"]
    after = r.xadd(prefix + ":audit", {"event": "follow.test_ready"})
    gen = control.iter_events(cfg, after=after, follow=True)
    result, error = [], []
    def receive():
        try:
            result.append(next(gen))
        except Exception as exc:
            error.append(exc)
    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    until(lambda: any(c["cmd"] == "xread" and "b" in c["flags"] for c in r.client_list()))
    added = r.xadd(prefix + ":audit", {"event": "failed", "slot": "worker-1", "error": "full error"})
    thread.join(timeout=2)
    assert not thread.is_alive() and not error and result[0]["audit_id"] == added
    gen.close()
