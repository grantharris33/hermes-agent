#!/bin/sh
# shellcheck shell=sh
# /opt/hermes/bin/hermes — `docker exec` privilege-drop shim.
#
# Background
# ----------
# The s6 image runs the supervised gateway/main process as the unprivileged
# `hermes` user (UID 10000). When an operator runs `docker exec <c> hermes ...`
# the default UID is root (0), and any file the command writes under
# $HERMES_HOME — auth.json, .env, config.yaml — ends up root-owned and
# unreadable to the supervised gateway. The most common manifestation: the
# user runs `docker exec <c> hermes login`, this writes
# /opt/data/auth.json as root:root mode 0600, and from then on the gateway
# returns "Provider authentication failed: Hermes is not logged into Nous
# Portal" on every incoming message — even though `docker exec <c> hermes
# chat -q ping` (also running as root) succeeds because root happens to be
# able to read its own root-owned file. See systematic-debugging skill
# notes attached to this fix.
#
# Fix
# ---
# This shim sits at /opt/hermes/bin/hermes and is placed earliest on PATH.
# When invoked as root, it drops to the hermes user (via s6-setuidgid)
# before exec'ing the real venv binary, so anything that writes under
# $HERMES_HOME is uid-aligned with the supervised processes. When invoked
# as any non-root UID — including the supervised processes themselves,
# `docker exec --user hermes`, kanban subagents, etc. — it short-circuits
# straight to the venv binary with no privilege change. Net: one extra
# fork on the docker-exec-as-root path, zero behavioral change on every
# other path.
#
# Recursion safety: the shim exec's the venv binary by *absolute path*
# (/opt/hermes/.venv/bin/hermes), so the second hop cannot re-enter this
# shim regardless of PATH state. No sentinel env var needed.
#
# Opt-out: set HERMES_DOCKER_EXEC_AS_ROOT=1 (1/true/yes, case-insensitive)
# to keep running as root. Reserved for diagnostic sessions where the
# operator deliberately wants root semantics — e.g. inspecting root-only
# state via the hermes CLI. Default is to drop.

set -e

REAL=/opt/hermes/.venv/bin/hermes
S6_BROWSER_ENV=/run/s6/container_environment/AGENT_BROWSER_EXECUTABLE_PATH

# s6 exports boot-discovered runtime values to supervised services through its
# envdir.  A process launched with `docker exec`, however, starts from the
# image's static Config.Env and does not inherit that envdir.  Import only the
# one verified browser executable that stage2 writes so operator and synthetic
# CLI sessions get the same browser behavior as the gateway.
inherit_s6_browser_path() {
    [ -z "${AGENT_BROWSER_EXECUTABLE_PATH:-}" ] || return 0
    [ -f "$S6_BROWSER_ENV" ] || return 0
    [ ! -L "$S6_BROWSER_ENV" ] || return 0

    browser_path=$(cat "$S6_BROWSER_ENV") || return 0
    [ -n "$browser_path" ] || return 0
    [ -x "$browser_path" ] || return 0
    AGENT_BROWSER_EXECUTABLE_PATH=$browser_path
    export AGENT_BROWSER_EXECUTABLE_PATH
}

inherit_s6_browser_path

# Defensive: if the venv binary is missing (corrupted image, partial
# install), fail loudly rather than silently masking it.
if [ ! -x "$REAL" ]; then
    echo "hermes-shim: $REAL not found or not executable" >&2
    exit 127
fi

# Already non-root? Just exec the real binary. This is the hot path for
# supervised processes (uid 10000) and for `docker exec --user hermes`.
if [ "$(id -u)" != "0" ]; then
    exec "$REAL" "$@"
fi

# Root, with opt-out set? Honor it.
case "${HERMES_DOCKER_EXEC_AS_ROOT:-}" in
    1|true|TRUE|True|yes|YES|Yes)
        exec "$REAL" "$@"
        ;;
esac

# Root, no opt-out. Drop to the hermes user.
#
# s6-setuidgid lives under /command/ which is NOT on `docker exec`'s PATH
# (s6-overlay only puts /command/ on PATH for supervision-tree children).
# Reference it by absolute path so the drop is robust against PATH
# manipulation.
S6_SUID=/command/s6-setuidgid
if [ ! -x "$S6_SUID" ]; then
    # Non-s6 image (someone stripped s6-overlay, or a hand-built variant).
    # Fail loud rather than silently re-execing as root and leaking the
    # bug this shim exists to prevent.
    echo "hermes-shim: $S6_SUID not found; refusing to silently run as root." >&2
    echo "hermes-shim: re-run with --user hermes or set HERMES_DOCKER_EXEC_AS_ROOT=1." >&2
    exit 126
fi

# Reset HOME to the hermes user's home before dropping privileges. Without
# this, $HOME stays /root and any library that resolves paths off $HOME
# (XDG caches, lockfiles, .config writes) will try to write to /root and
# fail with EACCES. Mirrors main-wrapper.sh.
export HOME=/opt/data

exec "$S6_SUID" hermes "$REAL" "$@"
