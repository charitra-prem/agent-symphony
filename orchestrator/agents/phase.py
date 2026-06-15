"""Run-attempt phase tracking for orchestrator observability.

Symphony-style: every long-running invocation transitions through
discrete phases. We record each transition both as an append-only
event row (phase_events) and as a denormalized "current" pair on
the issues row (current_phase / current_phase_at).

This module is intentionally side-effect-free outside the DB and
never raises -- instrumentation must not break the pipeline.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from enum import Enum
from pathlib import Path

log = logging.getLogger("orchestrator.phase")

DB_PATH = Path(os.environ.get("ORCHESTRATOR_DB", "/root/work/orchestrator/orchestrator.db"))


class Phase(str, Enum):
    PreparingWorkspace = "PreparingWorkspace"
    BuildingPrompt = "BuildingPrompt"
    LaunchingAgentProcess = "LaunchingAgentProcess"
    InitializingSession = "InitializingSession"
    StreamingTurn = "StreamingTurn"
    Finishing = "Finishing"
    Succeeded = "Succeeded"
    Failed = "Failed"
    TimedOut = "TimedOut"
    Stalled = "Stalled"
    CanceledByReconciliation = "CanceledByReconciliation"


def _connect() -> sqlite3.Connection:
    # isolation_level=None => autocommit; matches the orchestrator's short-write pattern.
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def set_phase(linear_id: str, phase: Phase, *, note: str = "", attempt: int | None = None) -> None:
    """Record a phase transition for an issue.

    Writes an append-only row to phase_events and updates the
    denormalized issues.current_phase / current_phase_at columns.
    Best-effort: errors are logged but never propagated.
    """
    try:
        conn = _connect()
        try:
            if attempt is None:
                row = conn.execute(
                    "SELECT current_attempt FROM issues WHERE linear_id = ?",
                    (linear_id,),
                ).fetchone()
                if row is not None:
                    attempt = row["current_attempt"]
            conn.execute(
                "INSERT INTO phase_events(linear_id, attempt, phase, note) VALUES (?, ?, ?, ?)",
                (linear_id, attempt, phase.value, note or None),
            )
            conn.execute(
                "UPDATE issues "
                "   SET current_phase = ?, "
                "       current_phase_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                " WHERE linear_id = ?",
                (phase.value, linear_id),
            )
        finally:
            conn.close()
    except Exception:
        log.exception("set_phase failed for %s phase=%s", linear_id, phase)


def history(linear_id: str) -> list[dict]:
    """Return phase_events rows (oldest first) for an issue."""
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id, linear_id, attempt, phase, note, at "
                "  FROM phase_events "
                " WHERE linear_id = ? "
                " ORDER BY id ASC",
                (linear_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        log.exception("phase history failed for %s", linear_id)
        return []


def snapshot(linear_id: str) -> dict:
    """Current phase + ISO-8601 timestamp of when we entered it.

    Returns an empty dict if the issue is unknown.
    """
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT current_phase, current_phase_at "
                "  FROM issues WHERE linear_id = ?",
                (linear_id,),
            ).fetchone()
            if row is None:
                return {}
            return {
                "linear_id": linear_id,
                "phase": row["current_phase"],
                "since": row["current_phase_at"],
            }
        finally:
            conn.close()
    except Exception:
        log.exception("phase snapshot failed for %s", linear_id)
        return {}
