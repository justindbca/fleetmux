"""Real private Redis regression tests. Only a missing binary may skip these."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest
import redis

from cmux_fleet.message_bus import AuditError, BusError, MAX_BODY, MessageBus


@pytest.fixture(scope="module")
def private_redis_v2():
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server unavailable")
    sock = "/tmp/cmux-fleet-" + uuid.uuid4().hex[:12] + ".sock"
    with tempfile.TemporaryDirectory(prefix="fleet-bus-v2-") as directory:
        logpath = Path(directory) / "redis.log"
        with logpath.open("wb") as log:
            process = subprocess.Popen([
                binary, "--port", "0", "--unixsocket", sock,
                "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
                "--dir", directory,
            ], stdout=log, stderr=subprocess.STDOUT)
        client = redis.Redis(unix_socket_path=sock, decode_responses=True,
                             socket_timeout=2, socket_connect_timeout=1,
                             max_connections=1)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail("private redis-server exited: " + logpath.read_text())
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    time.sleep(.02)
            else:
                pytest.fail("private Redis socket not available: " + logpath.read_text())
            yield client
        finally:
            client.close()
            client.connection_pool.disconnect()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()  # Only this fixture's child.
                    process.wait(timeout=5)
            Path(sock).unlink(missing_ok=True)


@pytest.fixture
def bus(private_redis_v2):
    return MessageBus(private_redis_v2, "test-" + uuid.uuid4().hex)


def register(bus):
    assert bus.register_slot("worker-1", 0, "session-0") == "registered"


def received(bus, mid="m-first"):
    register(bus)
    bus.enqueue("worker-1", {"instruction": "reply OK"}, message_id=mid)
    offered = bus.offer("worker-1")
    result = bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    assert result["delivery_id"] == offered["delivery_id"]
    return result


def test_two_queued_messages_offer_has_real_state_stream_lease_correlation(bus):
    register(bus)
    for mid in ("m-first", "m-second"):
        bus.enqueue("worker-1", {"instruction": mid}, message_id=mid)
    out = bus.offer("worker-1")
    assert bus.pending("worker-1") == ["m-second"]
    first, second = bus.message("m-first"), bus.message("m-second")
    assert first["status"] == "offered" and second["status"] == "queued"
    assert first["delivery_id"] == out["delivery_id"] != out["stream_id"]
    entries = bus.r.xrange(bus.inbox_key("worker-1"))
    assert len(entries) == 1
    sid, fields = entries[0]
    lease = bus.slot_state("worker-1")
    assert sid == first["stream_id"] == lease["stream_id"] == out["stream_id"]
    for key in ("run_id", "slot", "session_id", "message_id"):
        assert lease[key] == first[key] == fields[key]
    for key in ("generation", "fence", "attempt"):
        assert int(lease[key]) == first[key] == int(fields[key])
    assert lease["current_delivery"] == fields["delivery_id"] == out["delivery_id"]
    assert lease["pel_count"] == 0
    assert bus.r.xinfo_groups(bus.inbox_key("worker-1"))[0]["last-delivered-id"] == "0-0"
    assert bus.r.xlen(bus.audit_key) == 4  # register, enqueue x2, offer
    assert bus.audit_tail(1)[0][1]["event"] == "offered"
    with pytest.raises(BusError, match="busy"):
        bus.offer("worker-1")
    assert bus.pending("worker-1") == ["m-second"]


@pytest.mark.parametrize("bad", ["message", "lease", "inbox", "audit", "missing_message", "group"])
def test_offer_preconditions_do_not_destructively_dequeue(bus, bad):
    register(bus)
    for mid in ("m-first", "m-second"):
        bus.enqueue("worker-1", {"instruction": mid}, message_id=mid)
    keys = {"message": bus.msg_key("m-first"), "lease": bus.lease_key("worker-1"),
            "inbox": bus.inbox_key("worker-1"), "audit": bus.audit_key,
            "missing_message": bus.msg_key("m-first")}
    if bad == "group":
        bus.r.xgroup_destroy(bus.inbox_key("worker-1"), bus.slot_group("worker-1"))
    else:
        bus.r.delete(keys[bad])
        if bad != "missing_message":
            bus.r.set(keys[bad], "wrong-type")
    with pytest.raises(BusError):
        bus.offer("worker-1")
    assert bus.pending("worker-1") == ["m-first", "m-second"]
    if bad not in ("message", "missing_message"):
        assert bus.status("m-first") == "queued"
    if bad != "inbox":
        assert bus.r.xlen(bus.inbox_key("worker-1")) == 0


def test_named_offer_preserves_fifo(bus):
    register(bus)
    for mid in ("m-first", "m-second"):
        bus.enqueue("worker-1", {}, message_id=mid)
    with pytest.raises(BusError, match="not_fifo_head"):
        bus.offer("worker-1", "m-second")
    assert bus.pending("worker-1") == ["m-first", "m-second"]
    assert bus.offer("worker-1", "m-first")["message_id"] == "m-first"


def test_blocking_receive_uses_dedicated_connection_and_first_offer(bus, monkeypatch):
    register(bus)
    observer = redis.Redis(unix_socket_path=bus.r.connection_pool.connection_kwargs["path"],
                           decode_responses=True, socket_timeout=2, max_connections=1)
    observer_bus = MessageBus(observer, bus.run_id)
    client_name = "reader-v2-" + uuid.uuid4().hex
    make_reader = bus.blocking_client
    def observed_reader(block_ms):
        reader = make_reader(block_ms)
        assert reader.connection_pool is not bus.r.connection_pool
        assert reader.connection_pool is not observer.connection_pool
        assert reader.connection_pool.connection_kwargs["path"] == bus.r.connection_pool.connection_kwargs["path"]
        assert reader.connection_pool.connection_kwargs["socket_timeout"] > block_ms / 1000
        reader.client_setname(client_name)
        return reader
    monkeypatch.setattr(bus, "blocking_client", observed_reader)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(bus.receive, "worker-1", "consumer-0", 2000,
                                 generation=0, session_id="session-0")
            deadline = time.monotonic() + 2
            # Observe Redis's actual blocked flag using a different pool. This
            # establishes BLOCK without racing receive's control preflight.
            while True:
                if future.done():
                    pytest.fail("reader returned before BLOCK: %r" % future.result())
                if any(client["name"] == client_name and "b" in client["flags"]
                       and client["cmd"] == "xreadgroup" for client in observer.client_list()):
                    break
                assert time.monotonic() < deadline, "dedicated reader never entered BLOCK"
            assert not future.done()
            # Producer operations also use the observer's pool, so receipt's
            # validation cannot race a producer command on the one-connection pool.
            observer_bus.enqueue("worker-1", {"text": "合法 /tmp/report.md"})
            offered = observer_bus.offer("worker-1")
            env = future.result(timeout=2)
    finally:
        observer.close(); observer.connection_pool.disconnect()
    assert env["message_id"] == offered["message_id"]
    assert env["body"] == {"text": "合法 /tmp/report.md"}
    assert env["execution_allowed"] is True and env["recovered"] is False
    assert bus.message(env["message_id"])["status"] == "received"
    state = bus.slot_state("worker-1")
    assert state["status"] == "received" and state["pel_count"] == 1
    assert state["pel"][0]["message_id"] == env["stream_id"]
    assert state["pel"][0]["consumer"] == "consumer-0"
    assert bus.r.type(bus.inbox_key("worker-1")) == "stream"


def test_pending_recovery_keeps_same_delivery_and_never_authorizes_reexecution(bus):
    env = received(bus)
    resumed = MessageBus(bus.r, bus.run_id).receive(
        "worker-1", "consumer-0", generation=0, session_id="session-0")
    assert resumed["delivery_id"] == env["delivery_id"]
    assert resumed["stream_id"] == env["stream_id"]
    assert resumed["recovered"] and not resumed["execution_allowed"]
    recovery = bus.recover("worker-1", "consumer-0")
    assert len(recovery["own_pending"]) == 1 and not recovery["execution_allowed"]
    with pytest.raises(BusError, match="protected_consumer"):
        bus.receive("worker-1", "consumer-other", generation=0, session_id="session-0")
    assert len(bus.recover("worker-1", "consumer-other")["protected"]) == 1
    with pytest.raises(ValueError, match="claiming"):
        bus.recover("worker-1", "consumer-other", claim=True)
    assert bus.slot_state("worker-1")["pel"][0]["consumer"] == "consumer-0"


def test_receive_audit_failure_retains_pel_and_can_reconcile(bus):
    register(bus)
    mid = bus.enqueue("worker-1", {})
    bus.offer("worker-1")
    bus.r.delete(bus.audit_key); bus.r.set(bus.audit_key, "wrong-type")
    with pytest.raises(AuditError):
        bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    assert bus.status(mid) == "offered"
    assert bus.slot_state("worker-1")["pel_count"] == 1
    bus.r.delete(bus.audit_key)
    resumed = bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    assert not resumed["execution_allowed"] and bus.status(mid) == "received"


def test_terminal_commit_ack_release_and_replay_after_new_delivery(bus):
    env = received(bus)
    assert bus.renew("worker-1", env)
    assert bus.slot_state("worker-1")["pel_count"] == 1
    result = {"reply": "OK", "count": 2}
    assert bus.commit("worker-1", env, result) == "committed"
    state = bus.slot_state("worker-1")
    assert state["status"] == "idle" and state["pel_count"] == 0
    assert state["current_delivery"] == ""
    assert bus.message(env["message_id"])["result"] == result
    assert bus.commit("worker-1", env, {"count": 2, "reply": "OK"}) == "already"
    with pytest.raises(BusError, match="commit_conflict"):
        bus.commit("worker-1", env, {"reply": "changed"})
    with pytest.raises(BusError, match="commit_conflict"):
        bus.commit("worker-1", env, result, status="failed")
    bus.enqueue("worker-1", {}, message_id="m-next")
    newer = bus.offer("worker-1")
    assert bus.commit("worker-1", env, result) == "already"
    with pytest.raises(BusError, match="commit_conflict"):
        bus.commit("worker-1", env, {"reply": "changed after new offer"})
    assert bus.slot_state("worker-1")["current_delivery"] == newer["delivery_id"]
    assert bus.message(env["message_id"])["result"] == result


@pytest.mark.parametrize("field,value", [("run_id", "other-run"), ("slot", "worker-2"),
    ("generation", 1), ("session_id", "old-session"), ("fence", 999),
    ("delivery_id", "d-old"), ("stream_id", "1-0"), ("message_id", "m-other"),
    ("consumer", "other-consumer")])
def test_full_identity_rejects_old_or_foreign_callbacks(bus, field, value):
    env = received(bus)
    wrong = dict(env, **{field: value})
    for action in (lambda: bus.commit("worker-1", wrong, {"reply": "stale"}),
                   lambda: bus.renew("worker-1", wrong)):
        with pytest.raises(BusError):
            action()
    assert bus.status(env["message_id"]) == "received"
    assert bus.slot_state("worker-1")["pel_count"] == 1


def test_registration_identity_and_generation_replacement(bus):
    env = received(bus)
    assert bus.register_slot("worker-1", 0, "session-0") == "ok"
    with pytest.raises(BusError, match="busy"):
        bus.register_slot("worker-1", 1, "session-1")
    with pytest.raises(BusError, match="identity"):
        bus.receive("worker-1", "old", generation=0, session_id="old")
    bus.commit("worker-1", env, {"reply": "OK"})
    assert bus.register_slot("worker-1", 1, "session-1") == "replaced"
    with pytest.raises(BusError, match="old_generation"):
        bus.register_slot("worker-1", 0, "session-0")
    with pytest.raises(BusError, match="old_generation"):
        bus.register_slot("worker-1", 1, "different-session")
    bus.enqueue("worker-1", {}, message_id="m-new")
    new = bus.offer("worker-1")
    assert new["generation"] == 1 and new["session_id"] == "session-1"
    assert new["fence"] > env["fence"]
    assert bus.commit("worker-1", env, {"reply": "OK"}) == "already"
    with pytest.raises(BusError, match="identity"):
        bus.receive("worker-1", "old", generation=0, session_id="session-0")
    assert bus.slot_state("worker-1")["pel_count"] == 0


def test_run_namespaces_are_private_even_on_same_socket(bus):
    register(bus)
    other = MessageBus(bus.r, "other-" + uuid.uuid4().hex)
    register(other)
    for instance in (bus, other):
        instance.enqueue("worker-1", {"run": instance.run_id}, message_id="m-same")
    offered = bus.offer("worker-1")
    assert other.pending("worker-1") == ["m-same"]
    assert other.status("m-same") == "queued"
    assert bus.msg_key("m-same") != other.msg_key("m-same")
    with pytest.raises(BusError, match="foreign"):
        other.mark_uncertain("worker-1", offered)
    assert bus.slots() == ["worker-1"]
    assert list(bus.inspect()) == ["worker-1"]


def test_service_request_id_with_colon_preserved_through_commit(bus):
    register(bus)
    mid = bus.enqueue("worker-1", {"reply": "OK"}, message_id="m-service", request_id="cu-1:r7")
    assert bus.enqueue("worker-1", {"reply": "OK"}, message_id=mid, request_id="cu-1:r7") == mid
    assert bus.message(mid)["request_id"] == "cu-1:r7"
    assert bus.offer("worker-1")["request_id"] == "cu-1:r7"
    env = bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    assert env["request_id"] == "cu-1:r7"
    bus.commit("worker-1", env, {"reply": "OK"})
    assert bus.message(mid)["request_id"] == "cu-1:r7"
    with pytest.raises(BusError, match="conflict"):
        bus.enqueue("worker-1", {"reply": "OK"}, message_id=mid, request_id="cu-1:r8")
    # Service correlation rules do not relax any internal identity/key validator.
    with pytest.raises(ValueError):
        bus.register_slot("worker:1", 0, "session-0")
    with pytest.raises(ValueError):
        bus.enqueue("worker-1", {}, message_id="m:one", request_id="cu-1:r7")


@pytest.mark.parametrize("request_id", ["", "-req", ".req", "req with spaces", "req/1", "r"*129])
def test_service_request_id_rejects_invalid_original_format(bus, request_id):
    with pytest.raises(ValueError, match="request_id"):
        bus.enqueue("worker-1", {}, request_id=request_id)
    assert bus.pending("worker-1") == []


def wait_for_deadline_v2(bus, deadline):
    """Wait for the declared deadline on the test Redis clock, not a fixed delay."""
    while True:
        seconds, micros = bus.r.time()
        remaining = deadline - (seconds * 1000 + micros // 1000)
        if remaining <= 0:
            return
        Event().wait(remaining / 1000)


@pytest.mark.parametrize("named", [False, True])
def test_never_offered_expiry_retires_head_and_progresses_fifo(bus, named):
    register(bus)
    deadline = int(time.time()*1000) + 200
    old_body = {"instruction": "must never execute"}
    bus.enqueue("worker-1", old_body, message_id="old", request_id="cu-1:r7", deadline_at=deadline)
    bus.enqueue("worker-1", {"instruction": "next task"}, message_id="new")
    wait_for_deadline_v2(bus, deadline)
    if named:
        assert bus.offer("worker-1", "old") is None
        assert bus.pending("worker-1") == ["new"]
        assert bus.slot_state("worker-1")["status"] == "idle"
    offered = bus.offer("worker-1")
    assert offered["message_id"] == "new" and bus.pending("worker-1") == []
    old = bus.message("old")
    assert old["status"] == "committed" and old["outcome"] == "expired"
    assert old["expired_before_offer"] == "1" and old["attempt"] == 0
    assert old["body"] == old_body and old["request_id"] == "cu-1:r7"
    assert old["committed_at"] >= deadline and "delivery_id" not in old
    assert bus.r.ttl(bus.msg_key("old")) == -1
    expiry_events = [fields for _, fields in bus.audit_tail()
                     if fields["event"] == "queued_expired"]
    assert len(expiry_events) == 1 and expiry_events[0]["message_id"] == "old"
    assert expiry_events[0]["delivery_id"] == ""
    entries = bus.r.xrange(bus.inbox_key("worker-1"))
    assert len(entries) == 1 and entries[0][1]["message_id"] == "new"
    # Matching replay after expiry is evidence-only, even though its deadline passed.
    assert bus.enqueue("worker-1", old_body, message_id="old", request_id="cu-1:r7",
                       deadline_at=deadline) == "old"
    assert bus.pending("worker-1") == []
    with pytest.raises(BusError, match="conflict"):
        bus.enqueue("worker-1", {"changed": True}, message_id="old", request_id="cu-1:r7")
    env = bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    assert env["message_id"] == "new" and env["execution_allowed"]
    bus.commit("worker-1", env, {})
    assert bus.offer("worker-1") is None


def test_all_expired_queued_heads_terminalize_without_holding_slot(bus):
    register(bus)
    deadline = int(time.time()*1000) + 200
    for mid in ("old-1", "old-2"):
        bus.enqueue("worker-1", {}, message_id=mid, deadline_at=deadline)
    wait_for_deadline_v2(bus, deadline)
    assert bus.offer("worker-1") is None and bus.pending("worker-1") == []
    state = bus.slot_state("worker-1")
    assert state["status"] == "idle" and state["current_delivery"] == ""
    assert state["pel_count"] == 0 and bus.r.xlen(bus.inbox_key("worker-1")) == 0
    for mid in ("old-1", "old-2"):
        assert bus.message(mid)["outcome"] == "expired"
    assert bus.offer("worker-1") is None  # No duplicate terminal event.
    assert len([fields for _, fields in bus.audit_tail() if fields["event"] == "queued_expired"]) == 2


def test_expired_queued_head_audit_failure_keeps_reference_and_evidence(bus):
    register(bus)
    deadline = int(time.time()*1000) + 200
    bus.enqueue("worker-1", {}, message_id="old", deadline_at=deadline)
    bus.enqueue("worker-1", {}, message_id="new")
    wait_for_deadline_v2(bus, deadline)
    bus.r.delete(bus.audit_key); bus.r.set(bus.audit_key, "wrong-type")
    with pytest.raises(AuditError):
        bus.offer("worker-1")
    assert bus.pending("worker-1") == ["old", "new"] and bus.status("old") == "queued"
    assert bus.slot_state("worker-1")["status"] == "idle"
    assert bus.r.xlen(bus.inbox_key("worker-1")) == 0
    bus.r.delete(bus.audit_key)
    assert bus.offer("worker-1")["message_id"] == "new"
    assert bus.message("old")["outcome"] == "expired"


def test_enqueue_matching_replay_and_conflicts_including_after_commit(bus):
    register(bus)
    args = dict(message_id="m-one", request_id="req-one", kind="task",
                deadline_at=int(time.time()*1000) + 100000)
    body = {"b": [1, True, None], "a": "你好"}
    bus.enqueue("worker-1", body, **args)
    assert bus.enqueue("worker-1", {"a": "你好", "b": [1, True, None]}, **args) == "m-one"
    assert bus.pending("worker-1") == ["m-one"]
    for changed in (dict(args, request_id="req-other"), dict(args, kind="operator"),
                    dict(args, deadline_at=args["deadline_at"]+1)):
        with pytest.raises(BusError, match="conflict"):
            bus.enqueue("worker-1", body, **changed)
    with pytest.raises(BusError, match="conflict"):
        bus.enqueue("worker-1", {"changed": True}, **args)
    bus.offer("worker-1")
    env = bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    bus.commit("worker-1", env, {})
    assert bus.enqueue("worker-1", body, **args) == "m-one"
    assert bus.pending("worker-1") == [] and bus.status("m-one") == "committed"
    # Omitted request/deadline replay uses deterministic request + original deadline.
    mid = bus.enqueue("worker-1", {}, message_id="m-default")
    assert bus.enqueue("worker-1", {}, message_id=mid) == mid
    assert bus.pending("worker-1") == [mid]


def test_deadline_refuses_commit_holds_slot_pel_until_confirmed_stop(bus):
    env = received(bus)
    # Deterministic passage of deadline in this owned fixture, no flaky wall sleep.
    bus.r.hset(bus.msg_key(env["message_id"]), "deadline_at", 1)
    with pytest.raises(BusError, match="expired"):
        bus.commit("worker-1", env, {"reply": "late"})
    state = bus.slot_state("worker-1")
    assert state["status"] == "expired" and state["pel_count"] == 1
    bus.enqueue("worker-1", {}, message_id="m-waiting")
    with pytest.raises(BusError, match="busy"):
        bus.offer("worker-1")
    with pytest.raises(BusError, match="busy"):
        bus.register_slot("worker-1", 1, "session-1")
    with pytest.raises(BusError, match="confirmation"):
        bus.resolve_uncertain("worker-1", env, "failed")
    assert bus.resolve_uncertain("worker-1", env, "failed", execution_stopped=True) == "resolved"
    assert bus.resolve_uncertain("worker-1", env, "failed", execution_stopped=True) == "already"
    assert bus.slot_state("worker-1")["pel_count"] == 0
    assert bus.message(env["message_id"])["outcome"] == "failed"
    newer = bus.offer("worker-1")
    with pytest.raises(BusError, match="conflict"):
        bus.resolve_uncertain("worker-1", env, "cancelled", execution_stopped=True)
    assert bus.slot_state("worker-1")["current_delivery"] == newer["delivery_id"]


def test_expired_lease_and_uncertain_recovery_never_claims(bus):
    env = received(bus)
    bus.r.hset(bus.lease_key("worker-1"), "lease_until", 1)
    report = bus.recover("worker-1", "different-consumer")
    assert report["state"]["status"] == "expired"
    assert len(report["protected"]) == 1 and not report["execution_allowed"]
    assert bus.slot_state("worker-1")["pel"][0]["consumer"] == "consumer-0"
    with pytest.raises(BusError, match="expired"):
        bus.renew("worker-1", env)
    bus.resolve_uncertain("worker-1", env, "cancelled", execution_stopped=True)


def test_explicit_uncertain_holds_slot_and_rejects_commit(bus):
    env = received(bus)
    assert bus.mark_uncertain("worker-1", env, "tool outcome unknown") == "uncertain"
    assert bus.slot_state("worker-1")["pel_count"] == 1
    with pytest.raises(BusError, match="not_committable"):
        bus.commit("worker-1", env, {"reply": "unverified"})
    with pytest.raises(BusError, match="held"):
        bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    bus.resolve_uncertain("worker-1", env, "cancelled", execution_stopped=True)


def test_confirmed_stop_of_unread_offer_cannot_deliver_old_work_later(bus):
    register(bus)
    bus.enqueue("worker-1", {}, message_id="m-unread")
    old = bus.offer("worker-1")
    assert bus.mark_uncertain("worker-1", old) == "uncertain"
    assert bus.slot_state("worker-1")["pel_count"] == 0
    bus.resolve_uncertain("worker-1", old, "cancelled", execution_stopped=True)
    bus.enqueue("worker-1", {}, message_id="m-next")
    new = bus.offer("worker-1")
    env = bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    assert env["delivery_id"] == new["delivery_id"]
    assert env["message_id"] == "m-next"
    assert bus.r.xrange(bus.inbox_key("worker-1"), old["stream_id"], old["stream_id"]) == []
    assert bus.status("m-unread") == "committed"


def test_live_work_not_expired_or_trimmed(bus):
    env = received(bus)
    for key in (bus.msg_key(env["message_id"]), bus.inbox_key("worker-1"), bus.lease_key("worker-1")):
        assert bus.r.ttl(key) == -1
    for _ in range(10):
        assert bus.renew("worker-1", env)
    assert bus.slot_state("worker-1")["pel_count"] == 1
    assert bus.r.xrange(bus.inbox_key("worker-1"))[0][0] == env["stream_id"]


def test_renew_never_shortens_or_exceeds_deadline(bus):
    env = received(bus)
    old = int(bus.slot_state("worker-1")["lease_until"])
    assert bus.renew("worker-1", env, 100)
    assert int(bus.slot_state("worker-1")["lease_until"]) >= old
    assert bus.renew("worker-1", env, 10000000)
    assert int(bus.slot_state("worker-1")["lease_until"]) <= env["deadline_at"]


def test_audit_failure_prevents_enqueue_or_commit_without_partial_state(bus):
    register(bus)
    bus.r.delete(bus.audit_key); bus.r.set(bus.audit_key, "wrong-type")
    with pytest.raises(AuditError):
        bus.enqueue("worker-1", {}, message_id="m-fault")
    assert not bus.message("m-fault") and bus.pending("worker-1") == []
    bus.r.delete(bus.audit_key)
    bus.enqueue("worker-1", {}, message_id="m-good"); bus.offer("worker-1")
    env = bus.receive("worker-1", "consumer-0", generation=0, session_id="session-0")
    bus.r.delete(bus.audit_key); bus.r.set(bus.audit_key, "wrong-type")
    with pytest.raises(AuditError):
        bus.commit("worker-1", env, {})
    assert bus.status("m-good") == "received"
    assert bus.slot_state("worker-1")["pel_count"] == 1


def test_atomic_offers_only_one_active_lease(bus):
    register(bus)
    for mid in ("m-a", "m-b"):
        bus.enqueue("worker-1", {}, message_id=mid)
    def attempt():
        connection = redis.Redis(unix_socket_path=bus.r.connection_pool.connection_kwargs["path"],
                                 decode_responses=True)
        try:
            return MessageBus(connection, bus.run_id).offer("worker-1")
        except BusError as exc:
            return str(exc)
        finally:
            connection.close(); connection.connection_pool.disconnect()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(out, dict) for out in outcomes) == 1
    assert bus.pending("worker-1") == ["m-b"]
    assert bus.r.xlen(bus.inbox_key("worker-1")) == 1


def test_json_size_types_and_deadline_validation(bus):
    for body in ([], {1: "bad"}, {"value": float("nan")}, {"tuple": (1, 2)}, {"bad": "\ud800"},
                 {"huge": "中" * (MAX_BODY // 3 + 1)}):
        with pytest.raises(ValueError):
            bus.enqueue("worker-1", body)
    with pytest.raises(BusError, match="deadline_passed"):
        bus.enqueue("worker-1", {}, deadline_at=1)
    for deadline in (True, 1.5, "100", -1):
        with pytest.raises(ValueError):
            bus.enqueue("worker-1", {}, deadline_at=deadline)
    for generation in (True, -1, "0"):
        with pytest.raises(ValueError):
            bus.register_slot("worker-1", generation, "session-0")
    with pytest.raises(ValueError):
        MessageBus(bus.r, "bad:run")
    assert bus.pending("worker-1") == []


def test_empty_queue_and_finite_receive_return_none(bus):
    register(bus)
    assert bus.offer("worker-1") is None
    assert bus.receive("worker-1", "consumer-0", 1, generation=0, session_id="session-0") is None
    with pytest.raises(ValueError):
        bus.receive("worker-1", "consumer-0", 0, generation=0, session_id="session-0")
