"""
stall_detector — Symphony-style stall-timeout sweeper.

Runs forever, every 30s:
  1. Find issues in DEV_IN_PROGRESS / JUNIOR_QA / SENIOR_QA / DESIGN_QA.
  2. For each, locate its dev-agent tmux session (matched by issue identifier).
  3. Capture last 500 lines of the pane, hash, compare with previous hash.
     - If unchanged for > workflow.stall_timeout_ms() / 1000 sec, kill the
       tmux session, transition issue to BLOCKED, and mark phase Stalled.
     - If no tmux session at all is found while issue is DEV_IN_PROGRESS,
       that also counts as stalled.

This module is import-safe. It only touches state inside `stall_loop()`.

Tmux naming convention (see dev_agent_runner._tmux_session_name):
    dev-<short>-<safe_id>-<suffix>
where short is "claude" or "codex", safe_id is the issue identifier
lowercased with ":" and "." replaced by "-", and suffix is a 4-hex-char nonce.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

log = logging.getLogger("stall_detector")

# Lazy imports inside the loop body to avoid import-time cycles with
# orchestrator.py (which imports modules from agents/).


# In-memory hash state per linear_id:
#   { linear_id: (last_hash, first_seen_unchanged_ts) }
_HASH_STATE: dict[str, tuple[str, float]] = {}

# Iteration cadence.
LOOP_INTERVAL_S = 30.0
# Number of tmux pane lines to hash.
CAPTURE_LINES = 500


# --------------------------------------------------------------------------- #
# tmux helpers
# --------------------------------------------------------------------------- #


async def _exec(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _safe_id(identifier: str) -> str:
    """Mirror dev_agent_runner._tmux_session_name id sanitisation."""
    return identifier.replace(":", "-").replace(".", "-").lower()


async def _list_tmux_sessions() -> list[str]:
    rc, out, err = await _exec(
        ["tmux", "list-sessions", "-F", "#{session_name}"]
    )
    if rc != 0:
        # "no server running" is normal when there are no sessions.
        if "no server running" not in err.lower():
            log.debug("tmux list-sessions rc=%d err=%s", rc, err.strip())
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


async def _find_session_for(identifier: str, sessions: list[str]) -> str | None:
    """Return the dev-agent tmux session matching this issue identifier, if any.

    Naming: dev-<short>-<safe_id>-<suffix>. We match by middle component.
    """
    needle = f"-{_safe_id(identifier)}-"
    # Prefer dev-* names; fall back to any session containing the safe_id.
    for s in sessions:
        if s.startswith("dev-") and needle in f"-{s}-":
            return s
    for s in sessions:
        if needle in f"-{s}-":
            return s
    return None


async def _capture_pane(session: str, lines: int = CAPTURE_LINES) -> str | None:
    """Return last ``lines`` lines from the tmux pane, or None on failure."""
    rc, out, err = await _exec(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"]
    )
    if rc != 0:
        log.debug("capture-pane failed for %s: %s", session, err.strip())
        return None
    return out


async def _kill_session(session: str) -> None:
    rc, _out, err = await _exec(["tmux", "kill-session", "-t", session])
    if rc != 0:
        log.warning("kill-session %s failed: %s", session, err.strip())
    else:
        log.info("killed stalled tmux session %s", session)


# --------------------------------------------------------------------------- #
# Phase helper (optional)
# --------------------------------------------------------------------------- #


def _mark_phase_stalled(linear_id: str, note: str) -> None:
    try:
        from agents.phase import set_phase, Phase  # type: ignore
    except ImportError:
        return
    except Exception:
        log.exception("phase import failed (non-fatal)")
        return
    try:
        set_phase(linear_id, Phase.Stalled, note=note)
    except Exception:
        log.exception("set_phase(Stalled) failed for %s", linear_id)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


async def stall_loop() -> None:
    """Periodically scan active issues and block ones with stalled tmux output."""
    # Local imports to avoid cycles at module-load time.
    import orchestrator as orch  # type: ignore
    import workflow  # type: ignore

    S = orch.IssueState
    active_states = (
        S.DEV_IN_PROGRESS.value,
        S.JUNIOR_QA.value,
        S.SENIOR_QA.value,
        S.DESIGN_QA.value,
    )

    log.info("stall_detector started; interval=%.0fs", LOOP_INTERVAL_S)

    while True:
        try:
            await _tick(orch, workflow, active_states)
        except Exception:
            log.exception("stall_detector tick failed")
        await asyncio.sleep(LOOP_INTERVAL_S)


async def _tick(orch, workflow, active_states: tuple[str, ...]) -> None:
    timeout_s = max(1.0, workflow.stall_timeout_ms() / 1000.0)

    # 1. Query active issues.
    with orch.db() as conn:
        rows = conn.execute(
            "SELECT linear_id, identifier, state FROM issues "
            "WHERE state IN ({})".format(",".join("?" * len(active_states))),
            active_states,
        ).fetchall()

    if not rows:
        # Drop any stale hash entries (e.g. for issues now DONE).
        if _HASH_STATE:
            _HASH_STATE.clear()
        return

    # 2. Snapshot tmux sessions once per tick.
    sessions = await _list_tmux_sessions()
    now = time.time()
    alive_ids: set[str] = set()

    for row in rows:
        linear_id = row["linear_id"]
        identifier = row["identifier"]
        state_value = row["state"]
        alive_ids.add(linear_id)

        try:
            current_state = orch.IssueState(state_value)
        except ValueError:
            log.warning("unknown state %s for %s", state_value, identifier)
            continue

        session = await _find_session_for(identifier, sessions)

        if session is None:
            # No tmux session for an in-progress issue.
            if current_state == orch.IssueState.DEV_IN_PROGRESS:
                _maybe_block(
                    orch, linear_id, identifier, current_state,
                    reason="no tmux session found while DEV_IN_PROGRESS",
                )
            # For QA states a session may legitimately be absent (QA runs
            # are subprocesses, not tmux). Skip stall tracking.
            _HASH_STATE.pop(linear_id, None)
            continue

        pane = await _capture_pane(session)
        if pane is None:
            # Could not capture — treat as transient; don't blame the issue.
            continue

        digest = hashlib.sha256(pane.encode("utf-8", errors="replace")).hexdigest()
        prev = _HASH_STATE.get(linear_id)

        if prev is None or prev[0] != digest:
            _HASH_STATE[linear_id] = (digest, now)
            continue

        elapsed = now - prev[1]
        if elapsed <= timeout_s:
            continue

        # Stalled.
        log.warning(
            "STALL detected: issue=%s session=%s state=%s unchanged_for=%.0fs "
            "threshold=%.0fs",
            identifier, session, state_value, elapsed, timeout_s,
        )
        await _kill_session(session)
        _maybe_block(
            orch, linear_id, identifier, current_state,
            reason=f"no tmux output for {elapsed:.0f}s (>{timeout_s:.0f}s threshold)",
        )
        _HASH_STATE.pop(linear_id, None)

    # Garbage-collect hashes for issues that left the active set.
    for stale in list(_HASH_STATE.keys()):
        if stale not in alive_ids:
            _HASH_STATE.pop(stale, None)


def _maybe_block(orch, linear_id: str, identifier: str,
                 current_state, reason: str) -> None:
    """Transition to BLOCKED and mark phase Stalled. Best-effort."""
    try:
        ok = orch.transition(
            linear_id, current_state, orch.IssueState.BLOCKED,
            actor="stall-detector", reason=reason,
        )
    except Exception:
        log.exception("transition->BLOCKED failed for %s", identifier)
        return
    if not ok:
        log.info("stall-detector lost transition race for %s (%s)",
                 identifier, current_state)
        return
    log.info("stall-detector BLOCKED %s: %s", identifier, reason)
    _mark_phase_stalled(linear_id, note=reason)
