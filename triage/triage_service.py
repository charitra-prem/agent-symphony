"""FastAPI webhook for Linear → PM-triage agent → Linear comment.

POST /webhook
  Linear-Signature header: HMAC-SHA256(body, LINEAR_WEBHOOK_SECRET) hex
  body: Linear event JSON. Handles Issue.create.

GET /healthz
  liveness probe.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from pathlib import Path

import yaml
import sqlite3
from contextlib import closing
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

import sys as _sys
_sys.path.insert(0, '/root/work/orchestrator')
from agents import repo_resolver, linear_status, comment_handler  # WIRED-IMPORTS
from claude_session import ask as ask_claude
from claude_session import ensure_session
from linear_client import LinearClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("triage")

ROOT = Path(__file__).resolve().parent
ROUTING_PATH = ROOT / "routing_matrix.yaml"
PROMPT_PATH = ROOT / "pm_prompt.md"

ROUTING = yaml.safe_load(ROUTING_PATH.read_text())
PM_SYSTEM_PROMPT = PROMPT_PATH.read_text()

# ===========================================================================
# Persistence — webhook dedupe + pending triage tracker.
# ===========================================================================
STATE_DB = Path("/var/lib/triage/state.db")
ORCH_DB  = Path("/root/work/orchestrator/orchestrator.db")


def _init_state_db() -> None:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(STATE_DB)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS webhook_inbox (
            delivery_id TEXT NOT NULL,
            body_hash   TEXT NOT NULL,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (delivery_id, body_hash)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS pending_triages (
            linear_id   TEXT PRIMARY KEY,
            comment_id  TEXT,
            identifier  TEXT,
            started_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        )""")
        db.commit()


def _is_duplicate_webhook(delivery_id: str, body_hash: str) -> bool:
    """True if (delivery_id, body_hash) already seen — caller should no-op."""
    with closing(sqlite3.connect(STATE_DB)) as db:
        try:
            db.execute(
                "INSERT INTO webhook_inbox(delivery_id, body_hash) VALUES (?, ?)",
                (delivery_id, body_hash),
            )
            db.commit()
            return False
        except sqlite3.IntegrityError:
            return True


def _pending_register(linear_id: str, identifier: str, comment_id: str | None) -> None:
    with closing(sqlite3.connect(STATE_DB)) as db:
        db.execute(
            "INSERT OR REPLACE INTO pending_triages(linear_id, comment_id, identifier, started_at, completed_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, NULL)",
            (linear_id, comment_id, identifier),
        )
        db.commit()


def _pending_complete(linear_id: str) -> None:
    with closing(sqlite3.connect(STATE_DB)) as db:
        db.execute(
            "UPDATE pending_triages SET completed_at=CURRENT_TIMESTAMP WHERE linear_id=?",
            (linear_id,),
        )
        db.commit()


def _pending_orphans() -> list[tuple[str, str]]:
    """Returns [(linear_id, comment_id), ...] for triages never marked complete."""
    with closing(sqlite3.connect(STATE_DB)) as db:
        rows = db.execute(
            "SELECT linear_id, comment_id FROM pending_triages WHERE completed_at IS NULL"
        ).fetchall()
    return list(rows)


# ===========================================================================
# Orchestrator handoff — INSERT a row + state_events when PM recommends.
# ===========================================================================
def _handoff_to_orchestrator(issue: dict, rec: dict) -> bool:
    """Insert issue into orchestrator DB at state=triaged. Returns True on insert."""
    if not ORCH_DB.exists():
        log.warning("orchestrator DB missing — skipping handoff")
        return False
    try:
        with closing(sqlite3.connect(ORCH_DB)) as db:
            existing = db.execute(
                "SELECT state FROM issues WHERE linear_id=?", (issue["id"],)
            ).fetchone()
            if existing:
                log.info("orchestrator already has %s at state=%s — skipping",
                         issue.get("identifier"), existing[0])
                return False
            team = (issue.get("team") or {}).get("key", "")
            db.execute(
                """INSERT INTO issues(linear_id, identifier, title, team_key,
                                       task_type, complexity, harness, tier, state,
                                       triaged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'triaged', CURRENT_TIMESTAMP)""",
                (
                    issue["id"], issue.get("identifier"), issue.get("title"), team,
                    rec.get("task_type"), rec.get("complexity"), rec.get("harness"),
                    rec.get("complexity"),  # tier mirrors complexity for now
                ),
            )
            db.execute(
                """INSERT INTO state_events(linear_id, from_state, to_state, actor, reason, payload)
                   VALUES (?, NULL, 'triaged', 'pm-agent', ?, ?)""",
                (issue["id"], rec.get("reasoning", ""), json.dumps(rec)),
            )
            db.commit()
            log.info("orchestrator handoff: %s → triaged (harness=%s)",
                     issue.get("identifier"), rec.get("harness"))
            return True
    except Exception:
        log.exception("handoff to orchestrator failed for %s", issue.get("identifier"))
        return False




app = FastAPI(title="linear-triage")


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------
def _verify_linear_signature(body: bytes, signature: str | None) -> bool:
    secret = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
    if not secret:
        log.warning("LINEAR_WEBHOOK_SECRET unset — accepting all webhooks (DEV ONLY)")
        return True
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Triage flow
# ---------------------------------------------------------------------------
def _build_triage_user_message(issue: dict) -> str:
    labels = ", ".join(n["name"] for n in (issue.get("labels", {}).get("nodes") or []))
    team = (issue.get("team") or {}).get("name", "?")
    project = (issue.get("project") or {}).get("name", "—")
    creator = (issue.get("creator") or {}).get("name", "?")
    desc = (issue.get("description") or "").strip()[:6000]  # cap to keep prompt small

    return f"""\
Triage this Linear issue. Reply with the JSON schema you were instructed to follow, then [[TRIAGE_DONE]].

Identifier: {issue.get('identifier')}
Title: {issue.get('title')}
Priority: {issue.get('priorityLabel')} ({issue.get('priority')})
State: {(issue.get('state') or {}).get('name')}
Team: {team}
Project: {project}
Labels: {labels or '—'}
Creator: {creator}

Description:
{desc or '(empty)'}
"""


_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)



def _escape_newlines_in_strings(s: str) -> str:
    """Replace literal `\n` inside JSON string values with the `\\n` escape.

    Claude Code's TUI word-wraps long JSON output, inserting raw newlines
    inside string values (notably `reasoning`). Strict json.loads rejects this.
    Walk the input char by char, tracking string + escape state, and emit
    `\\n` for any newline that appears inside an open string.
    """
    out = []
    in_string = False
    escape = False
    for ch in s:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch == "\n":
            out.append("\\n")
            continue
        if in_string and ch == "\r":
            out.append("\\r")
            continue
        out.append(ch)
    return "".join(out)


def _parse_pm_response(raw: str) -> dict | None:
    # Prefer the LAST fenced ```json block — that's Claude's actual answer
    # when the pasted prompt also contains a JSON example.
    matches = list(_JSON_BLOCK_RE.finditer(raw))
    if matches:
        chunk = matches[-1].group(1)
    else:
        # Fallback: last {...} blob in the raw text.
        i = raw.rfind("{")
        j = raw.rfind("}")
        if i < 0 or j <= i:
            return None
        chunk = raw[i:j + 1]
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        pass
    # Second attempt — sanitise TUI-wrapped newlines inside strings.
    fixed = _escape_newlines_in_strings(chunk)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        log.exception("pm response not valid JSON even after newline fix: %r", fixed[:400])
        return None


def _harness_invoke(harness_id: str) -> str:
    h = (ROUTING.get("harnesses") or {}).get(harness_id) or {}
    return h.get("invoke", "(unknown harness — check routing_matrix.yaml)")


def _format_linear_comment(rec: dict) -> str:
    harness = rec.get("harness", "?")
    alt = rec.get("alternative", "?")
    return f"""\
**Triage recommendation**

→ Run: `{_harness_invoke(harness)}` — **{harness}** ({rec.get('confidence', '?')} confidence)
→ Fallback: `{_harness_invoke(alt)}` — **{alt}**

- **Task type**: {rec.get('task_type', '?')}
- **Complexity**: {rec.get('complexity', '?')}
- **Estimated time**: ~{rec.get('estimated_time_min', '?')} min

**Reasoning**: {rec.get('reasoning', '').strip()}

<sub>posted by linear-triage. Edit `routing_matrix.yaml` on the box to change recommendations.</sub>
"""


STAGE_SEEN = "👀 **Triage agent**: seen, picking up shortly…"
STAGE_PICKED = "🤖 **Triage agent**: PM (Claude Sonnet, Max plan) analyzing the issue…"
STAGE_ANALYZED = "🧠 **Triage agent**: classification done, forming recommendation…"
STAGE_FAIL = "⚠️ **Triage agent**: PM returned malformed output. Falling back to default routing."


async def _triage_issue(issue_id_or_identifier: str) -> None:
    """Background task: fetch issue, walk through stages live-updating a single comment."""
    linear = LinearClient()
    comment_id: str | None = None
    try:
        issue = await linear.get_issue(issue_id_or_identifier)
        if not issue:
            log.warning("issue %s not found via Linear API", issue_id_or_identifier)
            return
        log.info("triaging %s — %s", issue.get("identifier"), issue.get("title"))

        # Stage 1: post initial "seen" comment
        seen_result = await linear.post_comment(issue["id"], STAGE_SEEN)
        comment_id = (seen_result.get("comment") or {}).get("id")
        log.info("posted SEEN comment %s for %s", comment_id, issue.get("identifier"))
        _pending_register(issue["id"], issue.get("identifier") or "", comment_id)

        # Stage 1b: emoji reaction on issue for at-a-glance "agent saw this"
        try:
            await linear.react_to_issue(issue["id"], "eyes")
        except Exception:
            log.debug("reaction failed (non-fatal)", exc_info=True)

        # Stage 2: edit comment → "picked up"
        if comment_id:
            await linear.edit_comment(comment_id, STAGE_PICKED)

        # Send the prompt to Claude
        user_msg = _build_triage_user_message(issue)
        prompt = f"<system>\n{PM_SYSTEM_PROMPT}\n</system>\n\n{user_msg}"
        raw = await ask_claude(prompt, timeout_s=180)

        # Stage 3: edit → "analyzing"
        if comment_id:
            await linear.edit_comment(comment_id, STAGE_ANALYZED)

        rec = _parse_pm_response(raw)
        if not rec:
            log.error("could not parse PM response for %s; raw=%r", issue.get("identifier"), raw[:400])
            fallback = STAGE_FAIL + f"\n\n<details><summary>Raw response head</summary>\n\n```\n{raw[:1200]}\n```\n\n</details>"
            if comment_id:
                await linear.edit_comment(comment_id, fallback)
            return

        final = _format_linear_comment(rec)
        if comment_id:
            await linear.edit_comment(comment_id, final)
        else:
            await linear.post_comment(issue["id"], final)
        log.info("posted recommendation on %s (comment %s)", issue.get("identifier"), comment_id)
        _pending_complete(issue["id"])
        # Resolve repo from issue before handoff (best-effort).
        try:
            issue["repo_full_name"] = await repo_resolver.resolve_repo_full_name(issue, linear=linear)
        except Exception:
            log.exception("repo_resolver crashed (non-fatal)")
        _handoff_to_orchestrator(issue, rec)
    except Exception as exc:
        log.exception("triage failed for %s", issue_id_or_identifier)
        if comment_id:
            try:
                await linear.edit_comment(comment_id, f"❌ **Triage agent**: errored — `{exc}`")
                _pending_complete(issue.get("id", ""))
            except Exception:
                pass
    finally:
        await linear.aclose()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _startup() -> None:
    _init_state_db()
    # Mark orphaned in-flight triages with a service-restart message so
    # users aren't staring at "PM analyzing…" forever.
    orphans = _pending_orphans()
    if orphans:
        log.warning("found %d orphan triage(s) at startup — posting restart notices", len(orphans))
        async def _notify_orphans():
            client = LinearClient()
            try:
                for linear_id, cid in orphans:
                    if cid:
                        try:
                            await client.edit_comment(
                                cid,
                                "❌ **Triage agent**: service was restarted mid-triage. "
                                "Re-trigger if you still need a recommendation.",
                            )
                        except Exception:
                            log.exception("could not annotate orphan comment %s", cid)
                    _pending_complete(linear_id)
            finally:
                await client.aclose()
        asyncio.create_task(_notify_orphans())
    # Pre-warm the Claude session.
    try:
        await asyncio.to_thread(ensure_session, "sonnet")
    except Exception:
        log.exception("could not pre-warm pm-claude session — will retry on first webhook")

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> dict[str, str]:
    body = await request.body()
    sig = request.headers.get("linear-signature")
    if not _verify_linear_signature(body, sig):
        raise HTTPException(status_code=401, detail="bad signature")
    delivery_id = request.headers.get("linear-delivery", "")
    body_hash = hashlib.sha256(body).hexdigest()
    if _is_duplicate_webhook(delivery_id or body_hash[:16], body_hash):
        log.info("dropping duplicate webhook delivery=%s", delivery_id[:16] or "(no-id)")
        return {"status": "duplicate"}
    payload = json.loads(body or b"{}")

    action = payload.get("action")
    event_type = payload.get("type")
    if event_type == "Comment" and action == "create":
        background.add_task(_dispatch_comment, payload)
        return {"status": "comment-queued"}
    if event_type != "Issue" or action != "create":
        log.info("ignoring linear event: type=%s action=%s", event_type, action)
        return {"status": "ignored"}

    data = payload.get("data") or {}
    issue_id = data.get("id")
    if not issue_id:
        raise HTTPException(status_code=400, detail="payload missing data.id")

    background.add_task(_triage_issue, issue_id)
    return {"status": "queued", "issue_id": issue_id}


@app.post("/triage-now")
async def triage_now(req: Request, background: BackgroundTasks) -> dict[str, str]:
    """Manual trigger — body: {"id": "<linear issue id or identifier>"}"""
    payload = await req.json()
    issue = payload.get("id")
    if not issue:
        raise HTTPException(status_code=400, detail="missing id")
    background.add_task(_triage_issue, issue)
    return {"status": "queued", "issue": issue}


async def _dispatch_comment(payload: dict) -> None:
    """Run the comment-handler dispatcher with our LinearClient + orchestrator DB."""
    linear = LinearClient()
    try:
        bot_id = await comment_handler.get_bot_user_id(linear)
        await comment_handler.handle_comment_event(
            payload,
            linear=linear,
            orch_db_path=Path("/root/work/orchestrator/orchestrator.db"),
        )
    except Exception:
        log.exception("comment dispatcher crashed")
    finally:
        await linear.aclose()
