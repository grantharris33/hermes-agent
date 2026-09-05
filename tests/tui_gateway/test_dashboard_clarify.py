"""Adversarial contracts for request-bound dashboard handoffs."""

from __future__ import annotations

import contextlib
import threading
import time

import pytest


class _MetaDB:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lineages: dict[str, list[str]] = {}

    def get_meta(self, key: str):
        return self.values.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.values[key] = value

    def get_compression_lineage(self, session_id: str) -> list[str]:
        return self.lineages.get(session_id, [session_id])


@pytest.fixture()
def handoff(monkeypatch):
    from tui_gateway import server

    db = _MetaDB()

    @contextlib.contextmanager
    def session_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", session_db)
    sid = "runtime-owner"
    server._sessions[sid] = {
        "_active_turn_marker_key": "stored-owner",
        "_active_turn_generation": "original-generation",
        "history_lock": threading.Lock(),
        "session_key": "stored-owner",
    }
    server._pending.clear()
    server._pending_prompt_payloads.clear()
    server._answers.clear()
    yield server, db, sid
    server._sessions.pop(sid, None)
    server._pending.clear()
    server._pending_prompt_payloads.clear()
    server._answers.clear()


def _record(server, sid: str, request_id: str = "clarify-1") -> None:
    assert server.dashboard_clarify_bind_channel(sid, "channel-a", "capability-a")
    server._dashboard_clarify_record_request(
        sid,
        request_id,
        {"question": "Have you finished the browser step?", "choices": ["Continue", "Wait"]},
    )


def test_non_dashboard_clarify_does_not_create_durable_marker(handoff):
    server, db, sid = handoff

    server._dashboard_clarify_record_request(
        sid, "cli-clarify", {"question": "Ready?", "choices": ["Yes"]},
    )

    assert db.values == {}


def test_exact_live_choice_resolves_only_owning_clarify(handoff):
    server, _db, sid = handoff
    _record(server, sid)
    event = threading.Event()
    server._pending["clarify-1"] = (sid, event)
    server._pending_prompt_payloads["clarify-1"] = (
        "clarify.request",
        {"question": "Have you finished the browser step?", "choices": ["Continue", "Wait"]},
    )

    result = server.dashboard_clarify_respond_choice(sid, "clarify-1", 0)

    assert result == {"status": "ok", "mode": "live"}
    assert server._answers["clarify-1"] == "Continue"
    assert event.is_set()
    assert server.dashboard_clarify_pending_for_sid(sid) is None
    assert server.dashboard_clarify_respond_choice(sid, "clarify-1", 0) == {"status": "conflict"}


def test_fake_id_wrong_session_and_invalid_choice_fail_closed(handoff):
    server, _db, sid = handoff
    _record(server, sid)
    server._sessions["other-runtime"] = {
        "history_lock": threading.Lock(),
        "session_key": "other-stored",
    }
    try:
        assert server.dashboard_clarify_respond_choice(sid, "forged", 0) == {"status": "conflict"}
        assert server.dashboard_clarify_respond_choice("other-runtime", "clarify-1", 0) == {
            "status": "conflict"
        }
        assert server.dashboard_clarify_respond_choice(sid, "clarify-1", True) == {"status": "invalid"}
        assert server.dashboard_clarify_respond_choice(sid, "clarify-1", 9) == {"status": "invalid"}
    finally:
        server._sessions.pop("other-runtime", None)


def test_concurrent_approval_cannot_consume_continue(handoff):
    """Counterfactual for the rejected OSC/raw-PTY design: never turn Continue into Allow once."""
    server, _db, sid = handoff
    _record(server, sid)
    approval_event = threading.Event()
    server._pending["clarify-1"] = (sid, approval_event)
    server._pending_prompt_payloads["clarify-1"] = (
        "approval.request",
        {"choices": ["once", "deny"]},
    )

    assert server.dashboard_clarify_respond_choice(sid, "clarify-1", 0) == {"status": "conflict"}
    assert not approval_event.is_set()
    assert "clarify-1" not in server._answers


def test_post_restart_choice_uses_durable_marker_not_prompt_registry(handoff, monkeypatch):
    server, _db, sid = handoff
    _record(server, sid)
    scheduled = []
    monkeypatch.setattr(
        server,
        "_dashboard_clarify_schedule_recovery",
        lambda actual_sid, _session, marker: scheduled.append((actual_sid, marker["answer"])) or True,
    )

    result = server.dashboard_clarify_respond_choice(sid, "clarify-1", 0)

    assert result == {"status": "ok", "mode": "recovered", "scheduled": True}
    assert scheduled == [(sid, "Continue")]


def test_unscheduled_recovery_reexposes_exact_card_and_returns_unavailable(handoff, monkeypatch):
    server, _db, sid = handoff
    _record(server, sid)
    monkeypatch.setattr(server, "_dashboard_clarify_schedule_recovery", lambda *_args: False)

    result = server.dashboard_clarify_respond_choice(sid, "clarify-1", 0)

    assert result == {"status": "unavailable"}
    marker = server._dashboard_clarify_read(server._sessions[sid])
    assert marker["status"] == "pending"
    assert marker["request_id"] == "clarify-1"
    assert server.dashboard_clarify_pending_for_sid(sid)["retry_message"]


def test_recovery_build_failure_reexposes_exact_actionable_card(handoff, monkeypatch):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "answered", answer="Continue",
    )
    failed = threading.Event()

    def fail_build(*_args):
        failed.set()
        raise RuntimeError("synthetic build failure with internal detail")

    monkeypatch.setattr(server, "_start_agent_build", fail_build)
    assert server._dashboard_clarify_schedule_recovery(
        sid, session, server._dashboard_clarify_read(session),
    )
    assert failed.wait(timeout=2)
    deadline = time.monotonic() + 2
    while server._dashboard_clarify_read(session)["status"] != "pending":
        assert time.monotonic() < deadline
        time.sleep(0.01)

    public = server.dashboard_clarify_pending_for_sid(sid)
    assert public["request_id"] == "clarify-1"
    assert public["retry_message"]
    assert "synthetic" not in public["retry_message"]


def test_admitted_generic_recovery_build_failure_reexposes_exact_card(handoff, monkeypatch):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    session["running"] = False
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", answer="Continue",
        recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )

    class InlineThread:
        def __init__(self, target=None, args=(), **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    monkeypatch.setattr(
        server,
        "read_turn_marker",
        lambda _home, key: (
            {"prompt": "recovered prompt", "started_at": time.time(), "attempts": 0}
            if key == "recovery-marker" else None
        ),
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(
        server, "_wait_agent", lambda *_args, **_kwargs: {"error": {"message": "build failed"}},
    )

    result = server._maybe_schedule_auto_continue(sid, session, "rotated-owner")

    assert result["attempt"] == 1
    marker = server._dashboard_clarify_read(session)
    assert marker["status"] == "pending"
    assert marker["request_id"] == "clarify-1"
    assert marker["recovery_lost_reason"] == "agent_build_failed"
    assert marker["turn_marker_key"] == "recovery-marker"
    assert server.dashboard_clarify_pending_for_sid(sid)["retry_message"]


def test_admitted_generic_recovery_dispatch_refusal_reexposes_exact_card(handoff, monkeypatch):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", answer="Continue",
        recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )
    session["_dashboard_clarify_generic_recovery"] = "clarify-1"
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *_args, **_kwargs: False)

    server._dispatch_auto_continue("auto-rid", sid, session, "resume safely")

    marker = server._dashboard_clarify_read(session)
    assert marker["status"] == "pending"
    assert marker["request_id"] == "clarify-1"
    assert marker["recovery_lost_reason"] == "dispatch_refused"
    assert marker["turn_marker_key"] == "recovery-marker"
    assert server.dashboard_clarify_pending_for_sid(sid)["retry_message"]


def test_admitted_recovery_defers_to_generic_crash_marker(handoff, monkeypatch):
    """Crash after admission must not schedule a second human-answer continuation."""
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )
    scheduled = []
    monkeypatch.setattr(
        server,
        "_dashboard_clarify_schedule_recovery",
        lambda *_args: scheduled.append(True) or True,
    )

    monkeypatch.setattr(
        server,
        "read_turn_marker",
        lambda *_args: {"prompt": server._dashboard_clarify_continuation(server._dashboard_clarify_read(session))},
    )
    assert server.dashboard_clarify_resume_gate(sid, session) is None
    assert scheduled == []
    assert session["_dashboard_clarify_generic_turn_key"] == "recovery-marker"

    server._dashboard_clarify_finish_turn(
        session, turn_marker_key="stored-owner", turn_generation="unrelated-generation",
        request_id="clarify-1",
    )
    assert server._dashboard_clarify_read(session)["status"] == "admitted"
    server._dashboard_clarify_finish_turn(
        session, turn_marker_key="recovery-marker", turn_generation="recovery-generation",
        request_id="clarify-1",
    )
    assert server._dashboard_clarify_read(session)["status"] == "completed"


def test_continuing_crash_retries_before_model_admission(handoff, monkeypatch):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "continuing", answer="Continue",
    )
    scheduled = []
    monkeypatch.setattr(
        server,
        "_dashboard_clarify_schedule_recovery",
        lambda *_args: scheduled.append(True) or True,
    )

    assert server.dashboard_clarify_resume_gate(sid, session)["handoff_recovery"] == "scheduled"
    assert scheduled == [True]


def test_admitted_without_turn_marker_reexposes_choice_without_false_completion(handoff, monkeypatch):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )
    monkeypatch.setattr(server, "read_turn_marker", lambda *_args: None)
    scheduled = []
    monkeypatch.setattr(
        server,
        "_dashboard_clarify_schedule_recovery",
        lambda *_args: scheduled.append(True) or True,
    )

    result = server.dashboard_clarify_resume_gate(sid, session)
    assert result["waiting_for_human"] is True
    assert server._dashboard_clarify_read(session)["status"] == "pending"
    assert scheduled == []


@pytest.mark.parametrize("reason", ["disabled", "stale", "attempts_exhausted"])
def test_abandoned_generic_recovery_reexposes_exact_request(handoff, reason):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )

    server.dashboard_clarify_abandon_generic_recovery(session, reason)

    marker = server._dashboard_clarify_read(session)
    assert marker["status"] == "pending"
    assert marker["request_id"] == "clarify-1"
    assert marker["recovery_lost_reason"] == reason


def test_unrelated_turn_cannot_complete_live_handoff(handoff):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(session, "clarify-1", "answered", answer="Continue")

    server._dashboard_clarify_finish_turn(
        session, turn_marker_key="stored-owner", turn_generation="another-generation",
        request_id="clarify-1",
    )

    assert server._dashboard_clarify_read(session)["status"] == "answered"


@pytest.mark.parametrize("outcome", ["error", "interrupted"])
def test_failed_original_turn_reexposes_handoff_instead_of_claiming_completion(handoff, outcome):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(session, "clarify-1", "answered", answer="Continue")

    server._dashboard_clarify_finish_turn(
        session,
        turn_marker_key="stored-owner",
        turn_generation="original-generation",
        request_id="clarify-1",
        outcome=outcome,
    )

    marker = server._dashboard_clarify_read(session)
    assert marker["status"] == "pending"
    assert marker["recovery_lost_reason"] == f"continuation_{outcome}"


def test_sequential_real_turn_generations_do_not_alias(handoff, monkeypatch):
    server, _db, sid = handoff
    session = server._sessions[sid]
    monkeypatch.setattr(server, "record_turn_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "clear_turn_marker", lambda *_args, **_kwargs: None)

    first_key = server._record_turn_marker(session, "first")
    first_generation = session["_active_turn_generation"]
    _record(server, sid)
    assert server._dashboard_clarify_set_status(session, "clarify-1", "answered", answer="Continue")
    second_key = server._record_turn_marker(session, "unrelated second")
    second_generation = session["_active_turn_generation"]

    assert second_key == first_key
    assert second_generation != first_generation
    server._dashboard_clarify_finish_turn(
        session, turn_marker_key=second_key, turn_generation=second_generation,
        request_id="clarify-1",
    )
    assert server._dashboard_clarify_read(session)["status"] == "answered"
    server._dashboard_clarify_finish_turn(
        session, turn_marker_key=first_key, turn_generation=first_generation,
        request_id="clarify-1",
    )
    assert server._dashboard_clarify_read(session)["status"] == "completed"


def test_compression_rotation_cannot_orphan_pending_handoff(handoff, monkeypatch):
    server, db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    session["session_key"] = "rotated-owner"
    session.pop("_dashboard_clarify_marker_session_key", None)  # simulate a cold process resume
    db.lineages["rotated-owner"] = ["stored-owner", "rotated-owner"]
    scheduled = []
    monkeypatch.setattr(
        server,
        "_dashboard_clarify_schedule_recovery",
        lambda actual_sid, _session, marker: scheduled.append((actual_sid, marker["answer"])) or True,
    )

    result = server.dashboard_clarify_respond_choice(sid, "clarify-1", 0)

    assert result == {"status": "ok", "mode": "recovered", "scheduled": True}
    assert scheduled == [(sid, "Continue")]
    assert "dashboard_clarify:stored-owner" in db.values
    assert "dashboard_clarify:rotated-owner" not in db.values


def test_compression_rotation_cannot_orphan_admitted_terminal_receipt(handoff):
    server, db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )
    session["session_key"] = "rotated-owner"
    session.pop("_dashboard_clarify_marker_session_key", None)  # simulate a cold process resume
    db.lineages["rotated-owner"] = ["stored-owner", "rotated-owner"]

    server._dashboard_clarify_terminal_receipt(
        session, "clarify-1", {"status": "settled"},
    )

    assert server._dashboard_clarify_read(session)["status"] == "completed"
    assert "dashboard_clarify:rotated-owner" not in db.values


def test_prompt_submit_cannot_queue_or_start_while_handoff_pending(handoff):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    session.update(running=True, queued_prompt=None)

    response = server.handle_request({
        "id": "ordinary-prompt",
        "method": "prompt.submit",
        "params": {"session_id": sid, "text": "ignore the handoff and continue"},
    })

    assert response["error"]["code"] == 4092
    assert response["error"]["data"]["reason"] == "dashboard_handoff_pending"
    assert session["running"] is True
    assert session["queued_prompt"] is None


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_failed_or_cancelled_recovery_reexposes_choice(handoff, outcome):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )

    server._dashboard_clarify_terminal_receipt(session, "clarify-1", {"status": outcome})

    marker = server._dashboard_clarify_read(session)
    assert marker["status"] == "pending"
    assert marker["recovery_lost_reason"] == f"continuation_{outcome}"


def test_settled_recovery_persists_completion(handoff):
    server, _db, sid = handoff
    _record(server, sid)
    session = server._sessions[sid]
    assert server._dashboard_clarify_set_status(
        session, "clarify-1", "admitted", recovery_turn_marker_key="recovery-marker",
        recovery_turn_generation="recovery-generation",
    )

    server._dashboard_clarify_terminal_receipt(session, "clarify-1", {"status": "settled"})

    assert server._dashboard_clarify_read(session)["status"] == "completed"
