"""HTTP control surface + dashboard for the orchestrator.

Mounted at /api/v1 (router) plus a `/` dashboard handler.
Does NOT touch transition / IssueState / webhook / sweep — only reads from the
DB and dispatches via existing helpers (orchestrator.transition, next_action,
force_block, discard_worktree).
"""

from __future__ import annotations

import logging
import sqlite3
from html import escape
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

import workflow

log = logging.getLogger("control_api")

router = APIRouter()


# ---------------------------------------------------------------------------
# DB helpers (use orchestrator.db connection helper — same DB_PATH)
# ---------------------------------------------------------------------------
def _conn():
    # Late import: orchestrator imports this module, so avoid a top-level cycle.
    from orchestrator import db
    return db()


def _rows_to_dicts(rows) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# POST /refresh — kick a dispatcher tick
# ---------------------------------------------------------------------------
@router.post("/refresh")
async def refresh() -> dict[str, Any]:
    try:
        from agents import dispatcher  # type: ignore
    except ImportError:
        raise HTTPException(status_code=503, detail="dispatcher not yet wired")
    try:
        result = await dispatcher.dispatcher_tick()
    except Exception as e:
        log.exception("dispatcher_tick failed")
        raise HTTPException(status_code=500, detail=f"dispatcher_tick failed: {e}")
    return {"ok": True, "result": result}


# ---------------------------------------------------------------------------
# GET /state — global snapshot
# ---------------------------------------------------------------------------
@router.get("/state")
def state() -> dict[str, Any]:
    from orchestrator import IssueState

    with _conn() as conn:
        by_state_rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM issues GROUP BY state"
        ).fetchall()
        issues_by_state = {r["state"]: r["n"] for r in by_state_rows}

        running_states = (
            IssueState.TRIAGING.value,
            IssueState.DEV_ASSIGNED.value,
            IssueState.DEV_IN_PROGRESS.value,
            IssueState.JUNIOR_QA.value,
            IssueState.ACCEPTANCE_CHECK.value,
            IssueState.DEV_DEPLOY.value,
            IssueState.SENIOR_QA.value,
            IssueState.DESIGN_QA.value,
            IssueState.SANITY_CHECK.value,
        )
        placeholders = ",".join("?" * len(running_states))
        running = [
            r["identifier"]
            for r in conn.execute(
                f"SELECT identifier FROM issues WHERE state IN ({placeholders}) "
                "ORDER BY updated_at DESC",
                running_states,
            ).fetchall()
        ]
        blocked = [
            r["identifier"]
            for r in conn.execute(
                "SELECT identifier FROM issues WHERE state=? ORDER BY updated_at DESC",
                (IssueState.BLOCKED.value,),
            ).fetchall()
        ]
        cancelled_count = conn.execute(
            "SELECT COUNT(*) AS n FROM issues WHERE state=?",
            (IssueState.CANCELLED.value,),
        ).fetchone()["n"]
        done_count = conn.execute(
            "SELECT COUNT(*) AS n FROM issues WHERE state=?",
            (IssueState.DONE.value,),
        ).fetchone()["n"]

    from orchestrator import VERSION

    return {
        "version": VERSION,
        "config": workflow.status_snapshot(),
        "issues_by_state": issues_by_state,
        "running": running,
        "blocked": blocked,
        "cancelled_count": cancelled_count,
        "done_count": done_count,
    }


# ---------------------------------------------------------------------------
# GET /issue/{identifier} — full detail
# ---------------------------------------------------------------------------
@router.get("/issue/{identifier}")
def get_issue(identifier: str) -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM issues WHERE identifier=?", (identifier,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"issue {identifier} not found")
        issue = dict(row)
        linear_id = issue["linear_id"]

        events = _rows_to_dicts(
            conn.execute(
                "SELECT id, from_state, to_state, actor, reason, created_at "
                "FROM state_events WHERE linear_id=? ORDER BY id DESC LIMIT 50",
                (linear_id,),
            ).fetchall()
        )
        qa = _rows_to_dicts(
            conn.execute(
                "SELECT id, attempt, kind, pr_number, commit_sha, verdict, "
                "feedback, agent_name, started_at, finished_at "
                "FROM qa_runs WHERE linear_id=? ORDER BY id DESC LIMIT 10",
                (linear_id,),
            ).fetchall()
        )

    phase_history: Optional[list[dict[str, Any]]] = None
    try:
        from agents import phase  # type: ignore
        getter = getattr(phase, "phase_history", None)
        if getter is not None:
            try:
                phase_history = getter(linear_id)
            except Exception as e:
                phase_history = [{"error": f"phase.phase_history failed: {e}"}]
    except ImportError:
        phase_history = None

    return {
        "issue": issue,
        "state_events": events,
        "qa_runs": qa,
        "phase_history": phase_history,
    }


# ---------------------------------------------------------------------------
# GET /issues — list with optional state filter
# ---------------------------------------------------------------------------
@router.get("/issues")
def list_issues(
    state: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    with _conn() as conn:
        if state:
            rows = conn.execute(
                "SELECT linear_id, identifier, title, state, tier, harness, "
                "current_owner_agent, loop_count, updated_at, blocked_reason "
                "FROM issues WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT linear_id, identifier, title, state, tier, harness, "
                "current_owner_agent, loop_count, updated_at, blocked_reason "
                "FROM issues ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return {"issues": _rows_to_dicts(rows), "count": len(rows)}


# ---------------------------------------------------------------------------
# POST /issue/{identifier}/retry — unblock by transitioning back
# ---------------------------------------------------------------------------
@router.post("/issue/{identifier}/retry")
async def retry_issue(identifier: str) -> dict[str, Any]:
    import orchestrator
    from orchestrator import IssueState

    with _conn() as conn:
        row = conn.execute(
            "SELECT linear_id, state, prev_state FROM issues WHERE identifier=?",
            (identifier,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"issue {identifier} not found")
    if row["state"] != IssueState.BLOCKED.value:
        raise HTTPException(
            status_code=409,
            detail=f"issue {identifier} is in state {row['state']}, not BLOCKED",
        )

    # Pick a sensible re-entry state.
    prev = row["prev_state"]
    target: IssueState
    if prev == IssueState.DEV_ASSIGNED.value:
        target = IssueState.TRIAGED  # re-assign cleanly
    elif prev and prev not in (IssueState.DONE.value, IssueState.CANCELLED.value):
        try:
            target = IssueState(prev)
        except ValueError:
            target = IssueState.TRIAGED
    else:
        target = IssueState.TRIAGED

    ok = orchestrator.transition(
        row["linear_id"], IssueState.BLOCKED, target,
        actor="control_api",
        reason=f"manual retry via /api/v1/issue/{identifier}/retry",
    )
    if not ok:
        raise HTTPException(status_code=409, detail="lost transition race")

    # Kick the next action so it picks up immediately.
    try:
        await orchestrator.next_action(row["linear_id"])
    except Exception as e:
        log.exception("next_action after retry failed")
        return {"ok": True, "new_state": target.value, "warning": f"next_action: {e}"}

    return {"ok": True, "new_state": target.value}


# ---------------------------------------------------------------------------
# POST /issue/{identifier}/cancel — force_block + discard worktree
# ---------------------------------------------------------------------------
@router.post("/issue/{identifier}/cancel")
async def cancel_issue(identifier: str) -> dict[str, Any]:
    import orchestrator
    from orchestrator import IssueState

    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM issues WHERE identifier=?", (identifier,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"issue {identifier} not found")
    issue = dict(row)

    orchestrator.force_block(issue["linear_id"], "cancelled via control_api")
    try:
        await orchestrator.discard_worktree(issue, "cancelled via control_api")
    except Exception as e:
        log.exception("discard_worktree failed for %s", identifier)
        worktree_err = str(e)
    else:
        worktree_err = None

    with _conn() as conn:
        conn.execute(
            "UPDATE issues SET state=?, blocked_reason=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE linear_id=?",
            (IssueState.CANCELLED.value, "cancelled via control_api", issue["linear_id"]),
        )
        conn.execute(
            "INSERT INTO state_events(linear_id, from_state, to_state, actor, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (issue["linear_id"], IssueState.BLOCKED.value,
             IssueState.CANCELLED.value, "control_api", "manual cancel"),
        )

    out = {"ok": True, "new_state": IssueState.CANCELLED.value}
    if worktree_err:
        out["worktree_warning"] = worktree_err
    return out


# ---------------------------------------------------------------------------
# Dashboard (registered separately via app.add_api_route)
# ---------------------------------------------------------------------------
_DASH_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 24px; background: #0f1115; color: #e6e6e6; }
  h1 { margin: 0 0 8px 0; font-size: 22px; }
  h2 { margin: 28px 0 8px 0; font-size: 15px; text-transform: uppercase;
       letter-spacing: 0.08em; color: #9aa4b2; border-bottom: 1px solid #2a2f3a;
       padding-bottom: 4px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #1f2430; }
  th { color: #9aa4b2; font-weight: 500; }
  tr:hover td { background: #161a23; }
  code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                font-size: 12px; color: #c9d1d9; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 10px;
          background: #1f2430; font-size: 11px; color: #9aa4b2; }
  .blocked { color: #f97583; }
  .running { color: #79c0ff; }
  .done { color: #56d364; }
  .muted { color: #6b7280; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }
  .meta { font-size: 12px; color: #9aa4b2; margin-bottom: 16px; }
"""


def _row(*cells: str) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _state_class(state: str) -> str:
    if state == "blocked":
        return "blocked"
    if state in ("done",):
        return "done"
    if state == "cancelled":
        return "muted"
    return "running"


async def dashboard() -> HTMLResponse:
    from orchestrator import IssueState

    try:
        cfg = workflow.status_snapshot()
    except Exception as e:
        cfg = {"error": str(e)}

    with _conn() as conn:
        by_state = conn.execute(
            "SELECT state, COUNT(*) AS n FROM issues GROUP BY state ORDER BY n DESC"
        ).fetchall()
        active = conn.execute(
            "SELECT identifier, title, state, current_owner_agent, updated_at "
            "FROM issues "
            "WHERE state NOT IN (?, ?) "
            "ORDER BY updated_at DESC LIMIT 20",
            (IssueState.DONE.value, IssueState.CANCELLED.value),
        ).fetchall()
        failed = conn.execute(
            "SELECT identifier, title, blocked_reason, updated_at "
            "FROM issues WHERE state=? ORDER BY updated_at DESC LIMIT 10",
            (IssueState.BLOCKED.value,),
        ).fetchall()

    cfg_mtime = cfg.get("last_mtime") if isinstance(cfg, dict) else None
    cfg_data = cfg.get("config") if isinstance(cfg, dict) and "config" in cfg else cfg
    max_concurrent = "?"
    polling_ms = "?"
    if isinstance(cfg_data, dict):
        agent_cfg = cfg_data.get("agent") or {}
        polling_cfg = cfg_data.get("polling") or {}
        max_concurrent = agent_cfg.get("max_concurrent_agents", "?")
        polling_ms = polling_cfg.get("interval_ms", "?")

    state_rows = "".join(
        _row(
            f'<span class="{_state_class(r["state"])}">{escape(r["state"])}</span>',
            f'<span class="mono">{r["n"]}</span>',
        )
        for r in by_state
    ) or _row('<span class="muted">no issues yet</span>', "")

    active_rows = "".join(
        _row(
            f'<span class="mono">{escape(r["identifier"] or "")}</span>',
            escape((r["title"] or "")[:60]),
            f'<span class="{_state_class(r["state"])}">{escape(r["state"] or "")}</span>',
            escape(r["current_owner_agent"] or ""),
            f'<span class="muted mono">{escape(r["updated_at"] or "")}</span>',
        )
        for r in active
    ) or _row('<span class="muted" colspan="5">no active issues</span>', "", "", "", "")

    failed_rows = "".join(
        _row(
            f'<span class="mono">{escape(r["identifier"] or "")}</span>',
            escape((r["title"] or "")[:60]),
            f'<span class="blocked">{escape((r["blocked_reason"] or "")[:80])}</span>',
            f'<span class="muted mono">{escape(r["updated_at"] or "")}</span>',
        )
        for r in failed
    ) or _row('<span class="muted">none</span>', "", "", "")

    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>orchestrator</title>
<style>{_DASH_CSS}</style>
</head><body>
<h1>orchestrator <span class="pill">auto-refresh 5s</span></h1>
<div class="meta">
  workflow mtime: <span class="mono">{escape(str(cfg_mtime))}</span> &middot;
  max_concurrent: <span class="mono">{escape(str(max_concurrent))}</span> &middot;
  polling: <span class="mono">{escape(str(polling_ms))} ms</span>
</div>

<div class="grid">
  <div>
    <h2>Issues by state</h2>
    <table><thead><tr><th>state</th><th>count</th></tr></thead>
    <tbody>{state_rows}</tbody></table>
  </div>
  <div>
    <h2>Recently failed (BLOCKED)</h2>
    <table><thead><tr><th>id</th><th>title</th><th>reason</th><th>updated</th></tr></thead>
    <tbody>{failed_rows}</tbody></table>
  </div>
</div>

<h2>Recently active</h2>
<table><thead><tr>
<th>id</th><th>title</th><th>state</th><th>owner</th><th>updated</th>
</tr></thead><tbody>{active_rows}</tbody></table>

</body></html>"""
    return HTMLResponse(html)
