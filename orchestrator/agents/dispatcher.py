"""Symphony-style dispatcher: reconcile + eligibility + per-state caps.

Replaces the naive sweep loop. Single tick = reconcile_running_issues() then
pick eligible rows, sort, dispatch up to global/per-state caps.

This module DOES NOT touch invoke_*_agent functions or transition()/next_action()/
IssueState — those belong to other agents. We import them and call them only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

import workflow
from agents import linear_status

log = logging.getLogger("dispatcher")


# ---------------------------------------------------------------------------
# Schema migration — idempotent
# ---------------------------------------------------------------------------
_MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("paused_by_reconcile", "INTEGER NOT NULL DEFAULT 0"),
    ("paused_reason",       "TEXT"),
    ("linear_state_name",   "TEXT"),
    ("linear_labels_json",  "TEXT"),
    ("linear_priority",     "INTEGER"),
    ("linear_checked_at",   "TEXT"),
]


def ensure_migrations(conn: sqlite3.Connection) -> None:
    """Add reconcile-related columns to `issues` if not already present.

    Uses ALTER TABLE ADD COLUMN per column, swallowing 'duplicate column' errors.
    """
    for col, decl in _MIGRATION_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE issues ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


# ---------------------------------------------------------------------------
# Linear refetch — minimal GraphQL: state.name + labels + priority + blockers
# ---------------------------------------------------------------------------
_REFETCH_QUERY = """
query OrchReconcile($id: String!) {
  issue(id: $id) {
    id
    identifier
    priority
    state { name type }
    labels { nodes { name } }
    relations(first: 50) {
      nodes {
        type
        relatedIssue { id identifier state { name type } }
      }
    }
  }
}
"""


async def _refetch_linear(linear_id: str) -> dict[str, Any] | None:
    try:
        data = await linear_status._gql(None, _REFETCH_QUERY, {"id": linear_id})
    except Exception:
        log.exception("linear refetch failed for %s", linear_id)
        return None
    return data.get("issue") or None


def _has_open_blockers(linear_issue: dict[str, Any]) -> bool:
    rels = ((linear_issue.get("relations") or {}).get("nodes")) or []
    terms = {s.lower() for s in workflow.terminal_states()}
    for r in rels:
        if (r.get("type") or "").lower() != "blocks":
            # We want issues that *block this one*. In Linear, "blocked_by" is
            # surfaced as relation type "blocks" on the related side; the
            # canonical relation type from the issue's perspective for
            # being-blocked is also exposed via blockedByRelations on newer
            # API. We accept either keyword to stay robust.
            if (r.get("type") or "").lower() not in {"blocked_by", "blocks_by", "blockedby"}:
                continue
        rel_issue = r.get("relatedIssue") or {}
        rel_state = (rel_issue.get("state") or {}).get("name") or ""
        if rel_state.lower() not in terms:
            return True
    return False


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
async def reconcile_running_issues() -> int:
    """For every non-terminal DB issue, refetch Linear state and act.

    Returns count of rows reconciled.
    """
    import orchestrator as orch  # local import to avoid cycles

    with orch.db() as conn:
        ensure_migrations(conn)
        rows = conn.execute(
            "SELECT linear_id, identifier, state, paused_by_reconcile "
            "FROM issues WHERE state NOT IN (?, ?, ?)",
            (orch.S.DONE.value, orch.S.CANCELLED.value, orch.S.BLOCKED.value),
        ).fetchall()

    terminal_lc = {s.lower() for s in workflow.terminal_states()}
    active_lc = {s.lower() for s in workflow.active_states()}

    reconciled = 0
    for r in rows:
        linear_id = r["linear_id"]
        identifier = r["identifier"]
        live = await _refetch_linear(linear_id)
        if live is None:
            continue
        reconciled += 1

        state_name = ((live.get("state") or {}).get("name") or "").strip()
        state_lc = state_name.lower()
        labels = [(n.get("name") or "") for n in ((live.get("labels") or {}).get("nodes") or [])]
        priority = live.get("priority")
        labels_json = json.dumps(labels)

        with orch.db() as conn:
            conn.execute(
                "UPDATE issues SET linear_state_name=?, linear_labels_json=?, "
                "linear_priority=?, linear_checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE linear_id=?",
                (state_name, labels_json, priority, linear_id),
            )

        # 1) terminal in Linear -> block and tear down worktree
        if state_lc and state_lc in terminal_lc:
            log.info("reconcile %s -> Linear terminal (%s); blocking + discarding worktree",
                     identifier, state_name)
            try:
                orch.force_block(linear_id, "linear-terminal")
            except Exception:
                log.exception("force_block failed for %s", identifier)
            try:
                issue = orch.get_issue(linear_id)
                if issue is not None:
                    await orch.discard_worktree(issue, "linear-terminal")
            except Exception:
                log.exception("discard_worktree failed for %s", identifier)
            continue

        # 2) non-active (and non-terminal) -> pause
        if state_lc and state_lc not in active_lc:
            if not r["paused_by_reconcile"]:
                log.info("reconcile %s -> pausing (linear state %s not active)",
                         identifier, state_name)
                with orch.db() as conn:
                    conn.execute(
                        "UPDATE issues SET paused_by_reconcile=1, paused_reason=? "
                        "WHERE linear_id=?",
                        (f"linear state '{state_name}' not active", linear_id),
                    )
            continue

        # 3) active again -> unpause
        if r["paused_by_reconcile"]:
            log.info("reconcile %s -> unpausing (linear state %s now active)",
                     identifier, state_name)
            with orch.db() as conn:
                conn.execute(
                    "UPDATE issues SET paused_by_reconcile=0, paused_reason=NULL "
                    "WHERE linear_id=?",
                    (linear_id,),
                )

    return reconciled


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def _parse_labels(issue_row: dict[str, Any]) -> list[str]:
    raw = issue_row.get("linear_labels_json")
    if not raw:
        return []
    try:
        return [str(x) for x in (json.loads(raw) or [])]
    except Exception:
        return []


def is_eligible(
    issue_row: dict[str, Any],
    running_states: dict[str, int],
) -> tuple[bool, str]:
    """Pure predicate. Returns (ok, reason).

    `running_states` is {state_value: count_currently_running}.
    """
    state = issue_row.get("state") or ""

    if issue_row.get("paused_by_reconcile"):
        return False, "paused_by_reconcile"

    # Required labels (case-insensitive)
    required = [s.lower() for s in workflow.required_labels()]
    if required:
        present = {l.lower() for l in _parse_labels(issue_row)}
        missing = [s for s in required if s not in present]
        if missing:
            return False, f"missing labels: {missing}"

    # Per-state slot
    cap = workflow.max_concurrent_for_state(state)
    in_flight = running_states.get(state, 0)
    if in_flight >= cap:
        return False, f"per-state cap reached for {state} ({in_flight}/{cap})"

    return True, "ok"


async def _check_blockers_if_needed(issue_row: dict[str, Any]) -> tuple[bool, str]:
    """Side-effecting helper: for TRIAGED/NEW with block_on_open_blockers, refetch and check."""
    if not workflow.block_on_open_blockers():
        return True, "ok"
    state = (issue_row.get("state") or "").lower()
    if state not in {"triaged", "new"}:
        return True, "ok"
    live = await _refetch_linear(issue_row["linear_id"])
    if live is None:
        return True, "ok"  # can't fetch, don't block dispatch
    if _has_open_blockers(live):
        return False, "open Linear blockers"
    return True, "ok"


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------
def _sort_key(row: dict[str, Any]) -> tuple:
    prio = row.get("linear_priority")
    # priority asc, None last: map None to a large sentinel
    prio_key = (1, 0) if prio is None else (0, int(prio))
    created = row.get("created_at") or ""
    ident = row.get("identifier") or ""
    return (prio_key, created, ident)


async def filter_and_sort(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_sort_key)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------
async def dispatcher_tick() -> dict[str, Any]:
    """Single dispatcher pass.

    Returns {reconciled, dispatched, deferred:[{id, reason}]}.
    """
    import orchestrator as orch

    reconciled = 0
    try:
        reconciled = await reconcile_running_issues()
    except Exception:
        log.exception("reconcile_running_issues failed")

    # Pull candidate rows (non-terminal).
    with orch.db() as conn:
        ensure_migrations(conn)
        rows = [
            dict(r) for r in conn.execute(
                "SELECT * FROM issues WHERE state NOT IN (?, ?, ?)",
                (orch.S.DONE.value, orch.S.CANCELLED.value, orch.S.BLOCKED.value),
            ).fetchall()
        ]

    # In-flight per-state counter from DB (anything not paused, not terminal).
    running_states: dict[str, int] = {}
    for r in rows:
        if r.get("paused_by_reconcile"):
            continue
        running_states[r["state"]] = running_states.get(r["state"], 0) + 1

    ordered = await filter_and_sort(rows)

    global_cap = workflow.max_concurrent()
    dispatched: list[str] = []
    deferred: list[dict[str, str]] = []
    # "Dispatched this tick" -- we count optimistically so we don't oversubscribe a slot.
    tick_running = dict(running_states)
    # We treat each currently-running row as already occupying a slot, so initial budget
    # is global_cap minus the number of unpaused rows that are in "in-flight" states.
    # Simpler model: cap how many new tasks we kick off per tick.
    new_tasks_left = max(0, global_cap)

    for row in ordered:
        if new_tasks_left <= 0:
            deferred.append({"id": row["identifier"], "reason": "global cap reached"})
            continue

        ok, reason = is_eligible(row, tick_running)
        if not ok:
            deferred.append({"id": row["identifier"], "reason": reason})
            continue

        ok, reason = await _check_blockers_if_needed(row)
        if not ok:
            deferred.append({"id": row["identifier"], "reason": reason})
            continue

        # Dispatch -- fire-and-forget. next_action is re-entrant safe (has its own lock).
        asyncio.create_task(orch.next_action(row["linear_id"]))
        dispatched.append(row["identifier"])
        tick_running[row["state"]] = tick_running.get(row["state"], 0) + 1
        new_tasks_left -= 1

    log.info("dispatcher_tick reconciled=%d dispatched=%d deferred=%d",
             reconciled, len(dispatched), len(deferred))
    return {
        "reconciled": reconciled,
        "dispatched": len(dispatched),
        "dispatched_ids": dispatched,
        "deferred": deferred,
    }
