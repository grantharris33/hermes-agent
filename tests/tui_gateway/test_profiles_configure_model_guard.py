"""profiles.configure honours the model selection guard (#95293 remainder).

The Bots-mode editor writes a profile's default model through
``profiles.configure`` — a surface that historically bypassed the
data-policy / expensive-model selection guard every other model-switch path
enforces (``config.set model`` answers ``confirm_required`` and waits for a
``confirm_expensive_model`` resend).  A guarded pick made from the Bots
surface was therefore applied silently, with no confirm flow anywhere.

These tests pin the same handshake contract on ``profiles.configure``:

* a guarded model WITHOUT ``confirm_expensive_model`` answers
  ``confirm_required`` + ``confirm_message`` and writes NOTHING;
* the confirmed resend (``confirm_expensive_model: true``) writes;
* unguarded models keep writing exactly as before.
"""

from __future__ import annotations

import contextlib
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import hermes_cli.model_selection_guards as guards
import tui_gateway.server as srv

GUARDED_MODEL = "muse-spark-1.2-contributor"
GUARD_MESSAGE = "CONTRIBUTOR TIER: this model may train on your data."


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


@pytest.fixture
def contributor_guard(monkeypatch):
    """Fire the selection guard for GUARDED_MODEL only, like the real
    data-policy guard fires for ``-contributor`` ids."""

    def fake_combined_selection_warning(model_name, **_kwargs):
        if model_name == GUARDED_MODEL:
            return SimpleNamespace(message=GUARD_MESSAGE, kind="data_policy")
        return None

    monkeypatch.setattr(guards, "combined_selection_warning", fake_combined_selection_warning)


def _configure(params):
    return srv._methods["profiles.configure"]("configure", {"name": "default", **params})["result"]


def _profile_model(home: Path):
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return None
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    model_cfg = cfg.get("model") or {}
    return model_cfg.get("default")


def _profile_coding_instructions(home: Path):
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return None
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    return (cfg.get("agent") or {}).get("coding_instructions")


def _describe():
    return srv._methods["profiles.describe"]("describe", {"name": "default"})["result"]


def test_guarded_model_answers_confirm_required_and_writes_nothing(home, contributor_guard):
    result = _configure({"model": GUARDED_MODEL, "provider": "opencode-go"})

    assert result.get("confirm_required") is True
    assert GUARD_MESSAGE in (result.get("confirm_message") or "")
    # The model section is PENDING confirmation, not failed — it must not
    # poison ``ok`` (the Bots editor toasts "Some sections failed" on False).
    assert result["applied"].get("model") is not False
    assert _profile_model(home) != GUARDED_MODEL


def test_confirmed_resend_writes_the_guarded_model(home, contributor_guard):
    result = _configure(
        {
            "model": GUARDED_MODEL,
            "provider": "opencode-go",
            "confirm_expensive_model": True,
        }
    )

    assert not result.get("confirm_required")
    assert result["applied"].get("model") is True
    assert _profile_model(home) == GUARDED_MODEL


def test_unguarded_model_still_writes_without_confirmation(home, contributor_guard):
    result = _configure({"model": "hermes-4.5-405b", "provider": "nous"})

    assert not result.get("confirm_required")
    assert result["applied"].get("model") is True
    assert _profile_model(home) == "hermes-4.5-405b"


def test_other_sections_still_apply_while_model_awaits_confirmation(home, contributor_guard):
    result = _configure(
        {
            "model": GUARDED_MODEL,
            "provider": "opencode-go",
            "soul": "# SOUL\nBe kind.",
        }
    )

    assert result.get("confirm_required") is True
    assert result["applied"].get("soul") is True
    assert _profile_model(home) != GUARDED_MODEL


def test_profile_standing_instructions_roundtrip_and_clear(home):
    instructions = "Prefer uv for Python environments and dependencies. Use nvm for Node version selection."

    result = _configure({"coding_instructions": instructions})

    assert result["applied"]["coding_instructions"] is True
    assert _profile_coding_instructions(home) == instructions
    assert _describe()["coding_instructions"] == instructions

    cleared = _configure({"coding_instructions": "  "})
    assert cleared["applied"]["coding_instructions"] is True
    assert _profile_coding_instructions(home) is None
    assert _describe()["coding_instructions"] == ""


def test_profile_standing_instructions_reject_oversize_without_overwrite(home):
    from agent.coding_context import CODING_INSTRUCTIONS_MAX_CHARS

    assert _configure({"coding_instructions": "keep me"})["applied"]["coding_instructions"] is True
    rejected = _configure({"coding_instructions": "x" * (CODING_INSTRUCTIONS_MAX_CHARS + 1)})

    assert rejected["applied"]["coding_instructions"] is False
    assert rejected["ok"] is False
    assert _profile_coding_instructions(home) == "keep me"


def test_concurrent_distinct_config_sections_do_not_erase_each_other(home, monkeypatch):
    """Two LONG_HANDLER workers must not save snapshots read before the other's write."""
    import hermes_cli.config as config_module

    state: dict = {}
    state_guard = threading.Lock()
    transaction_lock = threading.RLock()
    transaction_state = threading.local()
    unsynchronized_loads = threading.Barrier(2)
    simultaneous_start = threading.Barrier(3)

    @contextlib.contextmanager
    def deterministic_transaction():
        with transaction_lock:
            transaction_state.active = True
            try:
                yield
            finally:
                transaction_state.active = False

    def load_config():
        with state_guard:
            snapshot = copy.deepcopy(state)
        # If the production RMW boundary is removed, force both workers to read
        # the same stale snapshot before either can save. With the boundary,
        # this branch is never entered and the second read sees the first save.
        if not getattr(transaction_state, "active", False):
            unsynchronized_loads.wait(timeout=2)
        return snapshot

    def save_config(config, **_kwargs):
        with state_guard:
            state.clear()
            state.update(copy.deepcopy(config))

    monkeypatch.setattr(config_module, "config_rmw_transaction", deterministic_transaction)
    monkeypatch.setattr(config_module, "load_config", load_config)
    monkeypatch.setattr(config_module, "save_config", save_config)

    def configure(params):
        simultaneous_start.wait(timeout=2)
        return _configure(params)

    with ThreadPoolExecutor(max_workers=2) as pool:
        instructions = pool.submit(configure, {"coding_instructions": "Prefer uv for Python."})
        toolsets = pool.submit(configure, {"enabled_toolsets": ["terminal"]})
        simultaneous_start.wait(timeout=2)
        results = [instructions.result(timeout=3), toolsets.result(timeout=3)]

    assert all(result["ok"] for result in results)
    assert state["agent"]["coding_instructions"] == "Prefer uv for Python."
    assert state["tools"]["enabled_toolsets"] == ["terminal"]


def test_real_concurrent_handlers_persist_both_distinct_sections(home):
    simultaneous_start = threading.Barrier(3)

    def configure(params):
        simultaneous_start.wait(timeout=2)
        return _configure(params)

    with ThreadPoolExecutor(max_workers=2) as pool:
        instructions = pool.submit(configure, {"coding_instructions": "Prefer uv for Python."})
        toolsets = pool.submit(configure, {"enabled_toolsets": ["terminal"]})
        simultaneous_start.wait(timeout=2)
        results = [instructions.result(timeout=3), toolsets.result(timeout=3)]

    persisted = yaml.safe_load((home / "config.yaml").read_text()) or {}
    assert all(result["ok"] for result in results)
    assert persisted["agent"]["coding_instructions"] == "Prefer uv for Python."
    assert persisted["tools"]["enabled_toolsets"] == ["terminal"]
