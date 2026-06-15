"""Comment.create dispatcher for the Linear triage + orchestrator stack.

Pure dispatcher. Parses Linear comment payloads, classifies them as command /
mention / feedback / ignore, and either:
  * calls out to triage-now / orchestrator HTTP endpoints, or
  * writes a row into orchestrator.db's state_events table, or
  * posts a bot reply via LinearClient.

Wire-up: see README.md in this directory.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from linear_client import LinearClient  # type: ignore

log = logging.getLogger("triage.comment_handler")

# --------------------------------------------------------------------------- #
# Orchestrator state model — import if available, else fall back to a local   #
# whitelist. Documented so a drift between orchestrator.py and this file is   #
# obvious in code review.                                                     #
# --------------------------------------------------------------------------- #
try:
    from orchestrator import TRANSITIONS, IssueState  # type: ignore

    _HAVE_ORCH_ENUM = True
except Exception:  # pragma: no cover - orchestrator not importable in tests
    _HAVE_ORCH_ENUM = False

    class IssueState:  # type: ignore[no-redef]
        TRIAGE = "TRIAGE"
        DEV = "DEV"
        JUNIOR_QA = "JUNIOR_QA"
        SENIOR_QA = "SENIOR_QA"
        DESIGN_QA = "DESIGN_QA"
        BLOCKED = "BLOCKED"
        DONE = "DONE"
        CANCELLED = "CANCELLED"

    # Hard-coded fallback. Mirrors orchestrator.TRANSITIONS as of 2026-06.
    # Reason: this module must be importable in isolation for unit tests and
    # for the webhook process when orchestrator isn't on sys.path.
    TRANSITIONS: dict[str, set[str]] = {  # type: ignore[no-redef]
        IssueState.TRIAGE: {IssueState.DEV, IssueState.BLOCKED, IssueState.CANCELLED},
        IssueState.DEV: {
            IssueState.JUNIOR_QA,
            IssueState.BLOCKED,
            IssueState.CANCELLED,
        },
        IssueState.JUNIOR_QA: {
            IssueState.SENIOR_QA,
            IssueState.DEV,
            IssueState.BLOCKED,
            IssueState.CANCELLED,
        },
        IssueState.SENIOR_QA: {
            IssueState.DESIGN_QA,
            IssueState.DEV,
            IssueState.BLOCKED,
            IssueState.CANCELLED,
        },
        IssueState.DESIGN_QA: {
            IssueState.DONE,
            IssueState.DEV,
            IssueState.BLOCKED,
            IssueState.CANCELLED,
        },
        IssueState.BLOCKED: {IssueState.TRIAGE, IssueState.CANCELLED},
    }


# --------------------------------------------------------------------------- #
# Data types                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class CommentIntent:
    kind: str  # 'command' | 'mention' | 'feedback' | 'ignore'
    command: str | None  # 'triage' | 'block' | 'retry' | 'pass' | 'cancel' | 'use' | 'help' | None
    args: dict = field(default_factory=dict)
    raw_text: str = ""
    author_id: str = ""
    author_name: str | None = None
    issue_id: str | None = None
    comment_id: str | None = None


# --------------------------------------------------------------------------- #
# Bot identity cache                                                          #
# --------------------------------------------------------------------------- #
_BOT_USER_ID: str | None = None


async def get_bot_user_id(linear: "LinearClient") -> str:
    """Return the bot's Linear user id, cached after the first call."""
    global _BOT_USER_ID
    if _BOT_USER_ID:
        return _BOT_USER_ID
    try:
        res = await linear._gql("query { viewer { id } }", {})
        _BOT_USER_ID = res["viewer"]["id"]
        log.info("bot_user_id resolved", extra={"bot_user_id": _BOT_USER_ID})
        return _BOT_USER_ID  # type: ignore[return-value]
    except Exception as exc:
        log.exception("get_bot_user_id_failed: %s", exc)
        raise


# --------------------------------------------------------------------------- #
# Parser                                                                      #
# --------------------------------------------------------------------------- #
_KNOWN_COMMANDS = {"triage", "block", "retry", "pass", "cancel", "use", "help"}
_RETRY_STAGES = {"dev", "junior-qa", "senior-qa", "design-qa"}
_MENTION_RE = re.compile(r"@([A-Za-z0-9_\-\.]+)")
_CMD_RE = re.compile(r"^\s*/([a-zA-Z]+)\b\s*(.*)$", re.DOTALL)


def _extract_comment(payload: dict) -> dict:
    """Linear delivers the comment under `data` for Comment.create."""
    return payload.get("data") or payload.get("comment") or {}


async def parse_comment_intent(payload: dict, bot_user_id: str) -> CommentIntent:
    comment = _extract_comment(payload)
    body: str = (comment.get("body") or "").strip()
    user = comment.get("user") or {}
    author_id = user.get("id") or ""
    author_name = user.get("name")
    issue = comment.get("issue") or {}
    issue_id = issue.get("id") or comment.get("issueId")
    comment_id = comment.get("id")

    intent = CommentIntent(
        kind="ignore",
        command=None,
        raw_text=body,
        author_id=author_id,
        author_name=author_name,
        issue_id=issue_id,
        comment_id=comment_id,
    )

    if not body:
        return intent
    if author_id and author_id == bot_user_id:
        log.debug("skip self-comment", extra={"comment_id": comment_id})
        return intent  # kind='ignore'

    # 1) command?
    m = _CMD_RE.match(body)
    if m:
        cmd = m.group(1).lower()
        rest = (m.group(2) or "").strip()
        if cmd in _KNOWN_COMMANDS:
            intent.kind = "command"
            intent.command = cmd
            intent.args = _parse_command_args(cmd, rest)
            return intent

    # 2) @-mention without a command -> kind='mention'
    if _MENTION_RE.search(body):
        intent.kind = "mention"
        return intent

    # 3) plain text -> feedback (the caller decides whether to drop it based on state)
    intent.kind = "feedback"
    return intent


def _parse_command_args(cmd: str, rest: str) -> dict:
    if cmd == "block":
        return {"reason": rest or "blocked via /block"}
    if cmd == "retry":
        stage = rest.strip().lower() or "dev"
        if stage not in _RETRY_STAGES:
            return {"stage": "dev", "stage_invalid": stage}
        return {"stage": stage}
    if cmd == "use":
        harness = rest.strip().split()[0] if rest.strip() else ""
        return {"harness": harness}
    return {}


# --------------------------------------------------------------------------- #
# DB helpers                                                                  #
# --------------------------------------------------------------------------- #
def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _current_state(con: sqlite3.Connection, linear_id: str) -> str | None:
    row = con.execute(
        "SELECT state FROM issues WHERE linear_id = ?", (linear_id,)
    ).fetchone()
    return row["state"] if row else None


def _can_transition(from_state: str | None, to_state: str) -> bool:
    if from_state is None:
        return False
    allowed = TRANSITIONS.get(from_state, set())
    return to_state in allowed


def _record_state_event(
    con: sqlite3.Connection,
    linear_id: str,
    from_state: str | None,
    to_state: str | None,
    actor: str,
    reason: str,
    payload: dict,
) -> None:
    con.execute(
        "INSERT INTO state_events (linear_id, from_state, to_state, actor, reason, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (linear_id, from_state, to_state, actor, reason, json.dumps(payload)),
    )
    if to_state is not None:
        con.execute(
            "UPDATE issues SET state = ? WHERE linear_id = ?",
            (to_state, linear_id),
        )
    con.commit()


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                #
# --------------------------------------------------------------------------- #
async def _post_json(url: str, body: dict, *, timeout: float = 10.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            r = await cx.post(url, json=body)
            r.raise_for_status()
            return {"status": "ok", "code": r.status_code, "url": url}
    except httpx.HTTPError as exc:
        log.exception(
            "downstream_post_failed url=%s err=%s", url, exc
        )
        return {"status": "error", "url": url, "error": str(exc)}


async def _reply(linear: "LinearClient", issue_id: str, body: str) -> None:
    try:
        await linear.post_comment(issue_id, body)
    except Exception as exc:
        log.exception("post_reply_failed issue=%s err=%s", issue_id, exc)


# --------------------------------------------------------------------------- #
# Command executors                                                           #
# --------------------------------------------------------------------------- #
HELP_TEXT = (
    "**Triage bot commands**\n"
    "- `/triage` — re-run the PM triage agent on this issue\n"
    "- `/block <reason>` — force state → BLOCKED\n"
    "- `/retry <dev|junior-qa|senior-qa|design-qa>` — re-fire a stage\n"
    "- `/pass` — manually pass the current QA stage\n"
    "- `/cancel` — transition to CANCELLED\n"
    "- `/use <harness>` — override harness for the next dev run (e.g. `codex-gpt5`)\n"
    "- `/help` — show this cheat-sheet\n"
)


async def _do_triage(
    intent: CommentIntent, *, linear: "LinearClient", triage_now_url: str
) -> dict:
    res = await _post_json(triage_now_url, {"issue_id": intent.issue_id, "trigger": "comment-command"})
    if intent.issue_id:
        await _reply(linear, intent.issue_id, "Re-running triage… (`/triage` received)")
    return res


async def _do_block(
    intent: CommentIntent, *, linear: "LinearClient", db_path: Path
) -> dict:
    if not intent.issue_id:
        return {"status": "error", "error": "no issue_id"}
    reason = intent.args.get("reason", "")
    con = _connect(db_path)
    try:
        cur = _current_state(con, intent.issue_id)
        if not _can_transition(cur, IssueState.BLOCKED):
            msg = f"Cannot block from `{cur}` — transition not allowed."
            await _reply(linear, intent.issue_id, msg)
            return {"status": "rejected", "from_state": cur}
        _record_state_event(
            con,
            intent.issue_id,
            cur,
            IssueState.BLOCKED,
            actor=f"user:{intent.author_id}",
            reason=reason,
            payload={"comment_id": intent.comment_id, "author": intent.author_name},
        )
        await _reply(linear, intent.issue_id, f"Issue blocked: {reason}")
        return {"status": "ok", "from_state": cur, "to_state": IssueState.BLOCKED}
    finally:
        con.close()


async def _do_cancel(
    intent: CommentIntent, *, linear: "LinearClient", db_path: Path
) -> dict:
    if not intent.issue_id:
        return {"status": "error", "error": "no issue_id"}
    con = _connect(db_path)
    try:
        cur = _current_state(con, intent.issue_id)
        if not _can_transition(cur, IssueState.CANCELLED):
            await _reply(linear, intent.issue_id, f"Cannot cancel from `{cur}`.")
            return {"status": "rejected", "from_state": cur}
        _record_state_event(
            con,
            intent.issue_id,
            cur,
            IssueState.CANCELLED,
            actor=f"user:{intent.author_id}",
            reason="cancelled via /cancel",
            payload={"comment_id": intent.comment_id, "author": intent.author_name},
        )
        await _reply(linear, intent.issue_id, "Issue cancelled.")
        return {"status": "ok", "from_state": cur, "to_state": IssueState.CANCELLED}
    finally:
        con.close()


async def _do_retry(
    intent: CommentIntent, *, linear: "LinearClient", orchestrator_url: str
) -> dict:
    stage = intent.args.get("stage", "dev")
    if intent.args.get("stage_invalid"):
        if intent.issue_id:
            await _reply(
                linear,
                intent.issue_id,
                f"Unknown stage `{intent.args['stage_invalid']}`. Try one of: "
                + ", ".join(sorted(_RETRY_STAGES)),
            )
        return {"status": "rejected", "reason": "invalid_stage"}
    res = await _post_json(
        f"{orchestrator_url.rstrip('/')}/retry",
        {"issue_id": intent.issue_id, "stage": stage, "actor": intent.author_id},
    )
    if intent.issue_id:
        await _reply(linear, intent.issue_id, f"Re-firing stage `{stage}`.")
    return res


async def _do_pass(
    intent: CommentIntent, *, linear: "LinearClient", orchestrator_url: str
) -> dict:
    res = await _post_json(
        f"{orchestrator_url.rstrip('/')}/manual-pass",
        {"issue_id": intent.issue_id, "actor": intent.author_id},
    )
    if intent.issue_id:
        await _reply(linear, intent.issue_id, "QA manually passed.")
    return res


async def _do_use(
    intent: CommentIntent, *, linear: "LinearClient", db_path: Path
) -> dict:
    harness = intent.args.get("harness") or ""
    if not harness:
        if intent.issue_id:
            await _reply(linear, intent.issue_id, "Usage: `/use <harness>` e.g. `/use codex-gpt5`")
        return {"status": "rejected", "reason": "no_harness"}
    if not intent.issue_id:
        return {"status": "error", "error": "no issue_id"}
    con = _connect(db_path)
    try:
        cur = _current_state(con, intent.issue_id)
        _record_state_event(
            con,
            intent.issue_id,
            cur,
            None,  # no state transition; this is a hint for the next dev invocation
            actor=f"user:{intent.author_id}",
            reason=f"harness override: {harness}",
            payload={
                "harness": harness,
                "comment_id": intent.comment_id,
                "author": intent.author_name,
                "kind": "harness_override",
            },
        )
        await _reply(linear, intent.issue_id, f"Next dev run will use harness `{harness}`.")
        return {"status": "ok", "harness": harness}
    finally:
        con.close()


async def _do_help(intent: CommentIntent, *, linear: "LinearClient") -> dict:
    if intent.issue_id:
        await _reply(linear, intent.issue_id, HELP_TEXT)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Feedback recorder                                                           #
# --------------------------------------------------------------------------- #
_ACTIVE_FEEDBACK_STATES = {
    "dev_assigned", "dev_in_progress", "dev_done",
    "junior_qa", "acceptance_check",
    "senior_qa", "design_qa",
    "triaging", "triaged",
}


def _record_feedback(intent: CommentIntent, db_path: Path) -> dict:
    if not intent.issue_id:
        return {"status": "ignored", "reason": "no issue_id"}
    con = _connect(db_path)
    try:
        cur = _current_state(con, intent.issue_id)
        if cur not in _ACTIVE_FEEDBACK_STATES:
            log.info(
                "feedback_dropped issue=%s state=%s", intent.issue_id, cur
            )
            return {"status": "ignored", "reason": "inactive_state", "state": cur}
        _record_state_event(
            con,
            intent.issue_id,
            cur,
            None,
            actor="user-feedback",
            reason=intent.raw_text,
            payload={
                "comment_id": intent.comment_id,
                "author": intent.author_name,
                "author_id": intent.author_id,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {"status": "ok", "state": cur}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Top-level dispatcher                                                        #
# --------------------------------------------------------------------------- #
async def handle_comment_event(
    payload: dict,
    *,
    linear: "LinearClient",
    orch_db_path: Path,
    triage_now_url: str = "http://127.0.0.1:8088/triage-now",
    orchestrator_url: str = "http://127.0.0.1:8089",
) -> dict:
    bot_id = await get_bot_user_id(linear)
    intent = await parse_comment_intent(payload, bot_id)

    log.info(
        "comment_intent kind=%s command=%s issue=%s author=%s",
        intent.kind,
        intent.command,
        intent.issue_id,
        intent.author_id,
    )

    if intent.kind == "ignore":
        return {"status": "ignored", "reason": "self_or_empty"}

    if intent.kind == "mention":
        if intent.issue_id:
            await _reply(
                linear,
                intent.issue_id,
                "Hi! I respond to slash commands — try `/help`.",
            )
        return {"status": "ok", "kind": "mention"}

    if intent.kind == "feedback":
        res = _record_feedback(intent, orch_db_path)
        return {"status": res["status"], "kind": "feedback", "detail": res}

    # kind == 'command'
    cmd = intent.command
    try:
        if cmd == "help":
            return {"kind": "command", "command": cmd, **await _do_help(intent, linear=linear)}
        if cmd == "triage":
            return {"kind": "command", "command": cmd, **await _do_triage(intent, linear=linear, triage_now_url=triage_now_url)}
        if cmd == "block":
            return {"kind": "command", "command": cmd, **await _do_block(intent, linear=linear, db_path=orch_db_path)}
        if cmd == "cancel":
            return {"kind": "command", "command": cmd, **await _do_cancel(intent, linear=linear, db_path=orch_db_path)}
        if cmd == "retry":
            return {"kind": "command", "command": cmd, **await _do_retry(intent, linear=linear, orchestrator_url=orchestrator_url)}
        if cmd == "pass":
            return {"kind": "command", "command": cmd, **await _do_pass(intent, linear=linear, orchestrator_url=orchestrator_url)}
        if cmd == "use":
            return {"kind": "command", "command": cmd, **await _do_use(intent, linear=linear, db_path=orch_db_path)}
    except Exception as exc:
        log.exception("command_dispatch_failed cmd=%s err=%s", cmd, exc)
        return {"status": "error", "kind": "command", "command": cmd, "error": str(exc)}

    return {"status": "ignored", "reason": "unknown_command", "command": cmd}
