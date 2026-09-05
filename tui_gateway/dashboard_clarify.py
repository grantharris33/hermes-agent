"""Durable, request-bound human handoff for the dashboard chat surface.

The ordinary clarify registry is process-local because it blocks the live tool call.  A dashboard
handoff also records a small server-owned marker in the owning session database.  If the process
dies while the human is using the shared computer, session resume replays that exact marker and
waits; only an answer for the same session and request id starts a recovery continuation.

This module is rebound into :mod:`tui_gateway.server` by ``method_ctx.bind_module``.  It therefore
uses the gateway's authoritative prompt lock, pending registry, session registry and transport.
"""

from __future__ import annotations

import json
import threading
import time

from .method_ctx import bind_module

_DASHBOARD_CLARIFY_META_PREFIX = "dashboard_clarify:"
_DASHBOARD_CLARIFY_VERSION = 1
_DASHBOARD_CLARIFY_MAX_CHOICES = 9
_DASHBOARD_CLARIFY_MAX_QUESTION_CHARS = 4_000
_DASHBOARD_CLARIFY_MAX_CHOICE_CHARS = 1_000
_DASHBOARD_CLARIFY_MAX_QUESTION_ID_CHARS = 128
_DASHBOARD_CLARIFY_TERMINAL_STATES = frozenset({"completed", "expired"})
_DASHBOARD_CLARIFY_RETRY_MESSAGE = (
    "The manager could not resume yet. Your handoff is still paused; choose an answer to retry."
)


def _dashboard_clarify_key(session_key: str) -> str:
    return f"{_DASHBOARD_CLARIFY_META_PREFIX}{session_key}"


def _dashboard_clarify_session(sid: str) -> dict | None:
    with _sessions_lock:
        session = _sessions.get(sid)
    return session if session is not None and not session.get("_finalized") else None


def _dashboard_clarify_session_key(session: dict) -> str:
    return _session_lookup_key(session)


def _dashboard_clarify_lineage(session: dict, db) -> list[str]:
    """Bounded canonical compression lineage, or an empty list when unavailable."""
    current = _dashboard_clarify_session_key(session)
    lineage_reader = getattr(db, "get_compression_lineage", None)
    if not current or not callable(lineage_reader):
        return []
    try:
        lineage = lineage_reader(current)
    except Exception:
        return []
    return [str(key) for key in lineage if key] if isinstance(lineage, (list, tuple)) else []


def _dashboard_clarify_lineage_keys(session: dict, db) -> list[str]:
    """Storage candidates ordered by the current runtime, then its compression ancestors."""
    current = _dashboard_clarify_session_key(session)
    keys = [str(session.get("_dashboard_clarify_marker_session_key") or ""), current]
    keys.extend(reversed(_dashboard_clarify_lineage(session, db)))
    return list(dict.fromkeys(key for key in keys if key))


def _dashboard_clarify_initial_storage_key(session: dict, db) -> str:
    """Use the compression root so a later segment rotation cannot orphan a handoff."""
    candidates = _dashboard_clarify_lineage_keys(session, db)
    lineage = _dashboard_clarify_lineage(session, db)
    if lineage:
        return lineage[0]
    return candidates[0] if candidates else ""


def dashboard_clarify_bind_channel(sid: str, channel: str, capability: str) -> bool:
    """Stamp a session only from the trusted gateway-WS admission callback."""
    session = _dashboard_clarify_session(sid)
    if session is None or not channel or not capability:
        return False
    with session["history_lock"]:
        session["_dashboard_clarify_channel"] = channel
        session["_dashboard_clarify_capability"] = capability
    return True


def _dashboard_clarify_read(session: dict) -> dict | None:
    try:
        with _session_db(session) as db:
            if db is None:
                return None
            for session_key in _dashboard_clarify_lineage_keys(session, db):
                raw = db.get_meta(_dashboard_clarify_key(session_key))
                marker = json.loads(raw) if raw else None
                if (
                    isinstance(marker, dict)
                    and marker.get("version") == _DASHBOARD_CLARIFY_VERSION
                    and marker.get("session_key") == session_key
                ):
                    session["_dashboard_clarify_marker_session_key"] = session_key
                    return marker
    except Exception:
        logger.warning(
            "dashboard clarify marker read failed for %s",
            _dashboard_clarify_session_key(session),
            exc_info=True,
        )
        return None
    return None


def _dashboard_clarify_write(session: dict, marker: dict) -> bool:
    try:
        with _session_db(session) as db:
            if db is None:
                return False
            session_key = str(session.get("_dashboard_clarify_marker_session_key") or "")
            if not session_key:
                session_key = _dashboard_clarify_initial_storage_key(session, db)
            if not session_key:
                return False
            marker["session_key"] = session_key
            db.set_meta(
                _dashboard_clarify_key(session_key),
                json.dumps(marker, ensure_ascii=False, separators=(",", ":")),
            )
            session["_dashboard_clarify_marker_session_key"] = session_key
        return True
    except Exception:
        logger.warning(
            "dashboard clarify marker write failed for %s",
            _dashboard_clarify_session_key(session),
            exc_info=True,
        )
        return False


def _dashboard_clarify_wire_payload(request_id: str, payload: dict) -> dict | None:
    """Return the bounded single-question payload the mobile card is allowed to render."""
    normalized = _dashboard_clarify_single_question(payload)
    if normalized is None:
        return None
    question, choices, question_id = normalized
    if not _dashboard_clarify_valid_text(question, _DASHBOARD_CLARIFY_MAX_QUESTION_CHARS):
        return None
    if not isinstance(choices, list) or not 1 <= len(choices) <= _DASHBOARD_CLARIFY_MAX_CHOICES:
        return None
    if not all(_dashboard_clarify_valid_text(choice, _DASHBOARD_CLARIFY_MAX_CHOICE_CHARS)
               for choice in choices):
        return None
    wire = {"request_id": request_id, "question": question, "choices": list(choices)}
    if question_id:
        wire["question_id"] = question_id
    return wire


def _dashboard_clarify_single_question(payload: dict) -> tuple[object, object, str] | None:
    """Flatten the canonical one-entry batch while excluding unsupported handoff forms."""
    questions = payload.get("questions")
    if questions is None:
        question_id = payload.get("question_id") or ""
        if question_id and not _dashboard_clarify_valid_text(
            question_id, _DASHBOARD_CLARIFY_MAX_QUESTION_ID_CHARS,
        ):
            return None
        return payload.get("question"), payload.get("choices"), question_id
    if not isinstance(questions, list) or len(questions) != 1:
        return None
    entry = questions[0]
    if not isinstance(entry, dict) or entry.get("multi_select"):
        return None
    question_id = entry.get("qid")
    if not _dashboard_clarify_valid_text(question_id, _DASHBOARD_CLARIFY_MAX_QUESTION_ID_CHARS):
        return None
    return entry.get("question"), entry.get("choices"), question_id


def _dashboard_clarify_valid_text(value, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _dashboard_clarify_record_request(sid: str, request_id: str, payload: dict) -> None:
    """Persist a live clarify before it is emitted.  Failure leaves ordinary TUI clarify intact."""
    session = _dashboard_clarify_session(sid)
    wire = _dashboard_clarify_wire_payload(request_id, payload)
    if (
        session is None or wire is None
        or not session.get("_dashboard_clarify_channel")
        or not session.get("_dashboard_clarify_capability")
        or not session.get("_active_turn_marker_key")
        or not session.get("_active_turn_generation")
    ):
        return
    marker = {
        "version": _DASHBOARD_CLARIFY_VERSION,
        "status": "pending",
        "session_key": _dashboard_clarify_session_key(session),
        "owner_sid": sid,
        "turn_marker_key": str(session["_active_turn_marker_key"]),
        "turn_generation": str(session["_active_turn_generation"]),
        "created_at": time.time(),
        **wire,
    }
    if not _dashboard_clarify_write(session, marker):
        logger.warning("dashboard clarify request %s is live but not restart-durable", request_id)


def _dashboard_clarify_set_status(session: dict, request_id: str, status: str, **updates) -> bool:
    marker = _dashboard_clarify_read(session)
    if marker is None or marker.get("request_id") != request_id:
        return False
    marker.update(status=status, updated_at=time.time(), **updates)
    return _dashboard_clarify_write(session, marker)


def _dashboard_clarify_expire(sid: str, request_id: str) -> None:
    session = _dashboard_clarify_session(sid)
    if session is not None:
        _dashboard_clarify_set_status(session, request_id, "expired")


def _dashboard_clarify_note_live_answer(sid: str, request_id: str, answer: str) -> None:
    """Record answers submitted by the native TUI as well as the dashboard card."""
    session = _dashboard_clarify_session(sid)
    if session is not None:
        _dashboard_clarify_set_status(
            session, request_id, "answered", answer=answer, answered_at=time.time(),
        )


def _dashboard_clarify_finish_turn(
    session: dict, *, turn_marker_key: str, turn_generation: str, request_id: str = "",
    outcome: str = "complete",
) -> None:
    """Retire only the exact original/recovery turn at its terminal commit boundary."""
    marker = _dashboard_clarify_read(session)
    if marker is None:
        return
    if not _dashboard_clarify_turn_matches(
        marker, turn_marker_key=turn_marker_key,
        turn_generation=turn_generation, request_id=request_id,
    ):
        return
    marker_request_id = str(marker.get("request_id") or "")
    if outcome == "complete":
        _dashboard_clarify_set_status(session, marker_request_id, "completed")
    else:
        _dashboard_clarify_reexpose_retry(
            session, marker_request_id, f"continuation_{outcome}",
        )


def _dashboard_clarify_turn_matches(
    marker: dict, *, turn_marker_key: str, turn_generation: str, request_id: str,
) -> bool:
    """True only for the live turn or the request-bound recovery that owns this marker."""
    if marker.get("status") == "answered":
        return (
            marker.get("turn_marker_key") == turn_marker_key
            and marker.get("turn_generation") == turn_generation
        )
    return (
        marker.get("status") == "admitted"
        and marker.get("recovery_turn_marker_key") == turn_marker_key
        and marker.get("recovery_turn_generation") == turn_generation
        and marker.get("request_id") == request_id
    )


def _dashboard_clarify_public_marker(marker: dict | None) -> dict | None:
    if marker is None or marker.get("status") != "pending":
        return None
    wire = _dashboard_clarify_wire_payload(str(marker.get("request_id") or ""), marker)
    if wire is None:
        return None
    retry_message = marker.get("retry_message")
    return {
        **wire,
        "created_at": marker.get("created_at"),
        **({"retry_message": retry_message} if isinstance(retry_message, str) else {}),
    }


def dashboard_clarify_pending_for_sid(sid: str) -> dict | None:
    """Trusted dashboard read: pending card for the exact live UI session, if any."""
    session = _dashboard_clarify_session(sid)
    return _dashboard_clarify_public_marker(_dashboard_clarify_read(session)) if session is not None else None


def dashboard_clarify_prompt_admission_error(
    session: dict, authorized_request_id: str = "",
) -> str | None:
    """Fence every ordinary turn while an exact human handoff owns the session."""
    marker = _dashboard_clarify_read(session)
    if marker is None or marker.get("status") in _DASHBOARD_CLARIFY_TERMINAL_STATES:
        return None
    status = str(marker.get("status") or "")
    is_authorized_recovery = (
        authorized_request_id
        and marker.get("request_id") == authorized_request_id
        and status in {"continuing", "admitted"}
    )
    if is_authorized_recovery:
        return None
    return "manager is waiting for the exact dashboard handoff response"


def _dashboard_clarify_continuation(marker: dict) -> str:
    choice = str(marker.get("answer") or "")
    question = str(marker.get("question") or "")
    return (
        "[Human handoff completed after a backend restart]\n"
        f"Your earlier question was: {question}\n"
        f"The human selected: {choice}\n\n"
        "Continue the same task from this durable session. Check the current computer state before "
        "taking the next step, and do not repeat work that is already complete."
    )


def _dashboard_clarify_terminal_receipt(session: dict, request_id: str, payload: dict) -> None:
    """Persist a terminal recovery result before its ordinary turn marker is retired."""
    marker = _dashboard_clarify_read(session)
    if marker is None or marker.get("status") != "admitted":
        raise RuntimeError("handoff terminal receipt lost its admitted marker")
    if marker.get("request_id") != request_id:
        raise RuntimeError("handoff terminal receipt request mismatch")
    outcome = str(payload.get("status") or "failed")
    if outcome == "settled":
        updates = {"completed_at": time.time()}
        status = "completed"
    elif _dashboard_clarify_reexpose_retry(
        session, request_id, f"continuation_{outcome}",
    ):
        return
    else:
        raise RuntimeError("handoff retry state could not be persisted")
    if not _dashboard_clarify_set_status(session, request_id, status, **updates):
        raise RuntimeError("handoff terminal receipt could not be persisted")


def _dashboard_clarify_reexpose_retry(session: dict, request_id: str, reason: str) -> bool:
    """Make a failed continuation visible again without exposing internal exception text."""
    marker = _dashboard_clarify_read(session) or {}
    latest_turn = {}
    if marker.get("status") == "admitted":
        latest_turn = {
            "turn_marker_key": marker.get("recovery_turn_marker_key"),
            "turn_generation": marker.get("recovery_turn_generation"),
        }
    return _dashboard_clarify_set_status(
        session,
        request_id,
        "pending",
        recovery_lost_at=time.time(),
        recovery_lost_reason=reason,
        retry_message=_DASHBOARD_CLARIFY_RETRY_MESSAGE,
        **latest_turn,
    )


def _dashboard_clarify_schedule_recovery(sid: str, session: dict, marker: dict) -> bool:
    """Start one recovered continuation; the marker state is the duplicate-start fence."""
    request_id = str(marker.get("request_id") or "")
    if not request_id:
        return False
    with session["history_lock"]:
        if session.get("running") or session.get("_dashboard_clarify_recovery"):
            return False
        session["_dashboard_clarify_recovery"] = request_id
    attempts = int(marker.get("recovery_attempts") or 0) + 1
    if not _dashboard_clarify_set_status(session, request_id, "continuing", recovery_attempts=attempts):
        with session["history_lock"]:
            session.pop("_dashboard_clarify_recovery", None)
        return False

    def kickoff() -> None:
        try:
            _start_agent_build(sid, session)
            err = _wait_agent(session, f"__dashboard_clarify__{request_id}", timeout=120.0)
            if err:
                raise RuntimeError(str((err.get("error") or {}).get("message") or "agent build failed"))
            with session["history_lock"]:
                if session.get("running") or session.get("_finalized"):
                    raise RuntimeError("session became busy before handoff recovery")
                session["running"] = True
                session["last_active"] = time.time()
            if _ensure_active_session_slot(sid, session) is not None:
                with session["history_lock"]:
                    session["running"] = False
                raise RuntimeError("session has another live owner")
            interrupted_turn_key = str(
                marker.get("turn_marker_key") or _dashboard_clarify_session_key(session)
            )
            clear_turn_marker(_session_home(session), interrupted_turn_key)
            def admitted(turn_marker_key: str, turn_generation: str) -> None:
                if not _dashboard_clarify_set_status(
                    session, request_id, "admitted", admitted_at=time.time(),
                    recovery_turn_marker_key=turn_marker_key,
                    recovery_turn_generation=turn_generation,
                ):
                    # No model/tool work has started. Re-expose the exact card so a transient DB
                    # failure never becomes an invisible, unresumable handoff.
                    _dashboard_clarify_set_status(session, request_id, "pending")
                    raise RuntimeError("handoff admission receipt could not be persisted")

            def terminal_receipt(_payload: dict) -> None:
                try:
                    _dashboard_clarify_terminal_receipt(session, request_id, _payload)
                finally:
                    with session["history_lock"]:
                        session.pop("_dashboard_clarify_recovery", None)

            if not _run_prompt_submit(
                f"__dashboard_clarify__{request_id}", sid, session,
                _dashboard_clarify_continuation(marker), display_kind="auto_continue",
                display_metadata={"dashboard_clarify_request_id": request_id},
                terminal_callback=terminal_receipt,
                turn_admitted_callback=admitted,
                dashboard_recovery_request_id=request_id,
            ):
                raise RuntimeError("recovered continuation was not admitted")
        except Exception:
            logger.warning("dashboard clarify recovery failed for %s", request_id, exc_info=True)
            with session["history_lock"]:
                session["running"] = False
                session.pop("_dashboard_clarify_recovery", None)
            _dashboard_clarify_reexpose_retry(session, request_id, "recovery_start_failed")

    threading.Thread(target=kickoff, name=f"dashboard-clarify-{request_id}", daemon=True).start()
    return True


def dashboard_clarify_resume_gate(sid: str, session: dict) -> dict | None:
    """Suppress generic crash auto-continue while handoff input is pending; recover an answered handoff."""
    marker = _dashboard_clarify_read(session)
    if marker is None or marker.get("status") in _DASHBOARD_CLARIFY_TERMINAL_STATES:
        return None
    status = str(marker.get("status") or "")
    if status == "pending":
        return {"waiting_for_human": True, "request_id": marker.get("request_id")}
    if status in {"answered", "continuing"}:
        # continuing is still pre-admission: its callback runs before input preparation/model work.
        # A crash there is safe to retry from the exact durable answer.
        scheduled = _dashboard_clarify_schedule_recovery(sid, session, marker)
        return {"handoff_recovery": "scheduled" if scheduled else "pending", "request_id": marker.get("request_id")}
    if status == "admitted":
        return _dashboard_clarify_resume_admitted(session, marker)
    return None


def _dashboard_clarify_resume_admitted(session: dict, marker: dict) -> dict | None:
    """Delegate an admitted turn to bounded crash recovery, never answer it twice."""
    turn_key = str(marker.get("recovery_turn_marker_key") or "")
    turn = read_turn_marker(_session_home(session), turn_key)
    request_id = str(marker.get("request_id") or "")
    if turn is None:
        # Absence is not proof of completion: generic auto-continue also clears stale, disabled,
        # or exhausted markers. Re-expose the exact choice instead of fabricating a receipt.
        _dashboard_clarify_reexpose_retry(session, request_id, "turn_marker_missing")
        return {"waiting_for_human": True, "request_id": request_id}
    session["_dashboard_clarify_generic_recovery"] = request_id
    session["_dashboard_clarify_generic_turn_key"] = turn_key
    return None


def dashboard_clarify_generic_recovery_callbacks(session: dict) -> dict:
    """Callbacks that bind ordinary crash auto-continue to the exact admitted handoff."""
    request_id = str(session.pop("_dashboard_clarify_generic_recovery", "") or "")
    marker = _dashboard_clarify_read(session)
    if not request_id or marker is None or marker.get("status") != "admitted":
        return {}
    if marker.get("request_id") != request_id:
        return {}

    def admitted(turn_marker_key: str, turn_generation: str) -> None:
        if not _dashboard_clarify_set_status(
            session, request_id, "admitted", admitted_at=time.time(),
            recovery_turn_marker_key=turn_marker_key,
            recovery_turn_generation=turn_generation,
        ):
            raise RuntimeError("handoff crash-recovery admission could not be persisted")

    def terminal_receipt(_payload: dict) -> None:
        _dashboard_clarify_terminal_receipt(session, request_id, _payload)

    return {
        "dashboard_recovery_request_id": request_id,
        "display_metadata": {"dashboard_clarify_request_id": request_id},
        "terminal_callback": terminal_receipt,
        "turn_admitted_callback": admitted,
    }


def dashboard_clarify_abandon_generic_recovery(session: dict, reason: str) -> None:
    """Make a non-admitted generic recovery visible/actionable; never claim it completed."""
    expected_request_id = str(session.pop("_dashboard_clarify_generic_recovery", "") or "")
    session.pop("_dashboard_clarify_generic_turn_key", None)
    marker = _dashboard_clarify_read(session)
    if marker is None or marker.get("status") != "admitted":
        return
    request_id = str(marker.get("request_id") or "")
    if request_id and (not expected_request_id or request_id == expected_request_id):
        _dashboard_clarify_reexpose_retry(session, request_id, reason)


def dashboard_clarify_respond_choice(sid: str, request_id: str, choice_index: int) -> dict:
    """Atomically validate and answer only the owning session's exact clarify request.

    The browser supplies an index, never answer text.  The choice is read from the server-owned
    marker/prompt payload.  A stale, replayed, wrong-session or wrong-kind request is a conflict.
    """
    if type(choice_index) is not int:  # bool is an int subclass; it is not a choice index.
        return {"status": "invalid"}
    session = _dashboard_clarify_session(sid)
    if session is None:
        return {"status": "conflict"}
    with _prompt_lock:
        marker = _dashboard_clarify_read(session)
        if marker is None or marker.get("status") != "pending" or marker.get("request_id") != request_id:
            return {"status": "conflict"}
        choice = _dashboard_clarify_choice(marker, choice_index)
        if choice is None:
            return {"status": "invalid"}
        if request_id in _pending:
            return _dashboard_clarify_respond_live(
                sid, session, request_id, choice_index, choice, marker,
            )

        # No live blocking tool call means this is the post-restart path.  The durable marker is
        # authoritative, and the continuation starts only after its pending→answered transition.
        if not _dashboard_clarify_set_status(
            session, request_id, "answered", answer=choice, choice_index=choice_index, answered_at=time.time()
        ):
            return {"status": "unavailable"}
    scheduled = _dashboard_clarify_schedule_recovery(sid, session, {**marker, "answer": choice})
    if not scheduled:
        _dashboard_clarify_reexpose_retry(session, request_id, "recovery_not_scheduled")
        return {"status": "unavailable"}
    return {"status": "ok", "mode": "recovered", "scheduled": True}


def _dashboard_clarify_choice(marker: dict, choice_index: int) -> str | None:
    choices = marker.get("choices")
    if not isinstance(choices, list) or not 0 <= choice_index < len(choices):
        return None
    choice = choices[choice_index]
    return choice if isinstance(choice, str) else None


def _dashboard_clarify_respond_live(
    sid: str, session: dict, request_id: str, choice_index: int,
    choice: str, marker: dict,
) -> dict:
    slot = _dashboard_clarify_live_answer_slot(
        sid, request_id, marker,
    )
    if slot is None:
        return {"status": "conflict"}
    event_ready, question_id, batch = slot
    if not _dashboard_clarify_set_status(
        session, request_id, "answered", answer=choice,
        choice_index=choice_index, answered_at=time.time(),
    ):
        return {"status": "unavailable"}
    if question_id:
        assert batch is not None  # validated before the durable status transition
        batch["answers"][question_id] = choice
    else:
        _answers[request_id] = choice
    event_ready.set()
    return {"status": "ok", "mode": "live"}


def _dashboard_clarify_live_answer_slot(
    sid: str, request_id: str, marker: dict,
) -> tuple[threading.Event, str, dict | None] | None:
    """Validate the process-local clarify slot before its durable status changes."""
    owner_sid, event_ready = _pending[request_id]
    event, prompt_payload = _pending_prompt_payloads.get(request_id, ("", {}))
    if owner_sid != sid or event != "clarify.request":
        return None
    wire = _dashboard_clarify_wire_payload(request_id, prompt_payload)
    stored_wire = _dashboard_clarify_wire_payload(request_id, marker)
    if wire is None or wire != stored_wire:
        return None
    question_id = str(wire.get("question_id") or "")
    batch = _batch_clarify.get(request_id) if question_id else None
    if question_id and (batch is None or batch.get("qids") != [question_id]):
        return None
    return event_ready, question_id, batch


def register(server) -> None:
    bind_module(globals(), server)
