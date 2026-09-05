"""Dashboard handoff route/config/trusted-channel contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli.web_routers import chat_ws


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://computer.example.test/",
        "https://user:secret@computer.example.test/",
        "https://computer.example.test/?token=nope",
        "https://computer.example.test/#frame",
        "https://computer.example.test/" + "x" * 2_100,
        "https://[not-an-ip/",
    ],
)
def test_computer_url_rejects_absent_or_untrusted_values(monkeypatch, value):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {"dashboard": {"computer_url": value}})
    assert chat_ws._dashboard_computer_url() is None


def test_computer_url_accepts_https_config(monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: {"dashboard": {"computer_url": "https://computer.example.test/console"}},
    )
    assert chat_ws._dashboard_computer_url() == "https://computer.example.test/console"


def _fake_app():
    return SimpleNamespace(
        state=SimpleNamespace(pty_channel_bindings={}, pty_channel_sessions={}),
    )


def test_active_sid_requires_current_capability_and_gateway_source():
    app = _fake_app()
    app.state.pty_channel_bindings["channel-a"] = "current-cap"
    app.state.pty_channel_sessions["channel-a"] = {
        "capability": "stale-cap",
        "session_id": "wrong",
        "source": "gateway_ws",
    }
    with pytest.raises(chat_ws.HTTPException) as exc:
        chat_ws._active_sid_for_channel(app, "channel-a")
    assert exc.value.status_code == 409

    app.state.pty_channel_sessions["channel-a"]["capability"] = "current-cap"
    assert chat_ws._active_sid_for_channel(app, "channel-a") == "wrong"


def test_gateway_observer_ignores_uncorrelated_session_id(monkeypatch):
    from tui_gateway import server

    app = _fake_app()
    bound = []
    monkeypatch.setattr(server, "dashboard_clarify_bind_channel", lambda *args: bound.append(args))
    observer = chat_ws._GatewaySessionObserver(app, "channel-a", "cap-a", "connection-a")

    observer.on_frame({"id": "unrelated", "result": {"session_id": "wrong"}})
    observer.on_request({"id": "detail", "method": "session.detail"})
    observer.on_frame({"id": "detail", "result": {"session_id": "wrong"}})
    assert app.state.pty_channel_sessions == {}
    assert bound == []

    observer.on_request({"id": "resume", "method": "session.resume"})
    observer.on_frame({"id": "resume", "result": {"session_id": "runtime-right"}})
    assert app.state.pty_channel_sessions["channel-a"]["session_id"] == "runtime-right"
    assert bound == [("runtime-right", "channel-a", "cap-a")]

    observer.on_close()
    assert app.state.pty_channel_sessions == {}


def test_control_route_csrf_contract():
    from hermes_cli import web_server

    loopback = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_required=False)),
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    chat_ws._require_dashboard_control_token(loopback)
    loopback.headers = {}
    with pytest.raises(chat_ws.HTTPException) as exc:
        chat_ws._require_dashboard_control_token(loopback)
    assert exc.value.status_code == 403

    gated = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_required=True)),
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    chat_ws._require_dashboard_control_token(gated)
    gated.headers = {"Sec-Fetch-Site": "cross-site"}
    with pytest.raises(chat_ws.HTTPException) as exc:
        chat_ws._require_dashboard_control_token(gated)
    assert exc.value.status_code == 403
