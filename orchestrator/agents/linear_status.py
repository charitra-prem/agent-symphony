"""Sync orchestrator state → Linear workflow state.

Caches each team's workflow states (id/name/type), then maps orchestrator-side
state strings to Linear `state.type` buckets and issues `issueUpdate` mutations.
BLOCKED has no native Linear equivalent — we keep the issue 'unstarted' and add
an `agent:blocked` label instead.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from linear_client import LinearClient  # type: ignore[import-not-found]

LINEAR_GQL = "https://api.linear.app/graphql"
BLOCKED_LABEL = "agent:blocked"

# orchestrator state -> Linear state.type bucket
STATE_MAP: dict[str, str] = {
    "new": "triage",
    "triaging": "triage",
    "triaged": "unstarted",
    "dev_assigned": "unstarted",
    "dev_in_progress": "started",
    "dev_done": "started",
    "junior_qa": "started",
    "junior_qa_fail": "started",
    "acceptance_check": "started",
    "merged_to_dev": "started",
    "dev_deploy": "started",
    "senior_qa": "started",
    "design_qa": "started",
    "qa_fail": "started",
    "ready_for_main": "started",
    "merged_to_main": "started",
    "sanity_check": "started",
    "done": "completed",
    "blocked": "unstarted",
    "cancelled": "canceled",
}

_STATES_QUERY = """
query TeamStates($id: String!) {
  team(id: $id) {
    id
    states { nodes { id name type } }
  }
}
"""

_UPDATE_MUTATION = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { id state { id type } } }
}
"""

_LABEL_LOOKUP_QUERY = """
query LabelByName($filter: IssueLabelFilter) {
  issueLabels(filter: $filter, first: 1) { nodes { id name } }
}
"""

_LABEL_CREATE_MUTATION = """
mutation LabelCreate($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { success issueLabel { id name } }
}
"""

_ISSUE_ADD_LABEL_MUTATION = """
mutation IssueAddLabel($id: String!, $labelId: String!) {
  issueAddLabel(id: $id, labelId: $labelId) { success }
}
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _api_key(linear: LinearClient | None) -> str:
    key = getattr(linear, "api_key", None) if linear is not None else None
    key = key or os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY not set")
    return key


async def _gql(
    linear: LinearClient | None,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """Use the LinearClient's _gql if available; else direct httpx call."""
    if linear is not None and hasattr(linear, "_gql"):
        return await linear._gql(query, variables)  # type: ignore[no-any-return]
    headers = {
        "Authorization": _api_key(linear),
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            LINEAR_GQL,
            headers=headers,
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"linear graphql error: {body['errors']}")
    return body["data"]


async def cache_team_states(
    linear: LinearClient,
    team_id: str,
    db_path: Path,
) -> dict[str, str]:
    """Fetch team.states, upsert into team_workflow_states, return {type: state_id}.

    Returns one id per type bucket (triage/backlog/unstarted/started/completed/canceled).
    When multiple states share a type, picks the first returned by Linear (its UI order).
    """
    data = await _gql(linear, _STATES_QUERY, {"id": team_id})
    team = data.get("team")
    if not team:
        raise RuntimeError(f"team {team_id} not found")
    nodes = (team.get("states") or {}).get("nodes") or []

    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO team_workflow_states (team_id, state_id, state_name, state_type, cached_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(team_id, state_id) DO UPDATE SET
                state_name = excluded.state_name,
                state_type = excluded.state_type,
                cached_at  = excluded.cached_at
            """,
            [(team_id, n["id"], n["name"], n["type"], now) for n in nodes],
        )
        conn.commit()

    by_type: dict[str, str] = {}
    for n in nodes:
        by_type.setdefault(n["type"], n["id"])
    return by_type


def _lookup_state_id(
    db_path: Path, team_id: str, state_type: str
) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT state_id FROM team_workflow_states
            WHERE team_id = ? AND state_type = ?
            ORDER BY cached_at DESC LIMIT 1
            """,
            (team_id, state_type),
        ).fetchone()
    return row["state_id"] if row else None


async def add_label(
    linear: LinearClient | None,
    issue_id: str,
    label_name: str,
    *,
    team_id: str | None = None,
) -> bool:
    """Find-or-create a label by name (team-scoped if team_id given) and attach it."""
    filt: dict[str, Any] = {"name": {"eq": label_name}}
    if team_id:
        filt["team"] = {"id": {"eq": team_id}}
    data = await _gql(linear, _LABEL_LOOKUP_QUERY, {"filter": filt})
    nodes = (data.get("issueLabels") or {}).get("nodes") or []
    label_id = nodes[0]["id"] if nodes else None
    if not label_id:
        create_input: dict[str, Any] = {"name": label_name, "color": "#E11D48"}
        if team_id:
            create_input["teamId"] = team_id
        created = await _gql(linear, _LABEL_CREATE_MUTATION, {"input": create_input})
        payload = created.get("issueLabelCreate") or {}
        if not payload.get("success"):
            return False
        label_id = (payload.get("issueLabel") or {}).get("id")
    if not label_id:
        return False
    result = await _gql(
        linear,
        _ISSUE_ADD_LABEL_MUTATION,
        {"id": issue_id, "labelId": label_id},
    )
    return bool((result.get("issueAddLabel") or {}).get("success"))


async def update_issue_status(
    linear: LinearClient,
    issue_db_row: dict,
    orchestrator_state: str,
    db_path: Path,
) -> bool:
    state_type = STATE_MAP.get(orchestrator_state)
    if not state_type:
        raise ValueError(f"unknown orchestrator state: {orchestrator_state!r}")

    issue_id = issue_db_row.get("linear_id")
    team_id = issue_db_row.get("team_id") or issue_db_row.get("team", {}).get("id")
    if not issue_id:
        raise ValueError("issue_db_row missing linear_id")
    if not team_id:
        raise ValueError("issue_db_row missing team_id (needed for state lookup)")

    state_id = _lookup_state_id(db_path, team_id, state_type)
    if not state_id:
        # Cache miss — refresh and retry once.
        await cache_team_states(linear, team_id, db_path)
        state_id = _lookup_state_id(db_path, team_id, state_type)
    if not state_id:
        raise RuntimeError(
            f"no Linear state of type {state_type!r} for team {team_id}"
        )

    data = await _gql(
        linear,
        _UPDATE_MUTATION,
        {"id": issue_id, "input": {"stateId": state_id}},
    )
    ok = bool((data.get("issueUpdate") or {}).get("success"))

    if ok and orchestrator_state == "blocked":
        # Best-effort label; don't fail the status update if labeling fails.
        try:
            await add_label(linear, issue_id, BLOCKED_LABEL, team_id=team_id)
        except Exception:
            pass

    return ok
