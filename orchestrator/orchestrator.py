"""Pipeline orchestrator — FastAPI skeleton.

Drives a Linear issue through dev → junior QA → dev branch → senior + design QA → main.

Sibling to triage_service.py — that service still handles the PM-triage stage.
This orchestrator subscribes to the same Linear webhook *plus* GitHub webhooks, and
drives the state machine defined in state_machine.md.

Run:
    uvicorn orchestrator_skeleton:app --host 127.0.0.1 --port 8089

Environment:
    LINEAR_API_KEY                Linear personal API key
    LINEAR_WEBHOOK_SECRET         shared secret for HMAC verification
    GITHUB_WEBHOOK_SECRET         shared secret for HMAC verification
    GITHUB_TOKEN                  fine-grained PAT (PR read, label write, merge)
    ORCHESTRATOR_DB               sqlite path, default /var/lib/triage/orchestrator.db
    MAX_PARALLEL_ISSUES           default 8
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

import worktree
import workflow
from agents import dev_agent_runner, github_pr, junior_qa, ac_check, dev_deploy, senior_qa, design_qa, linear_status  # WIRED-IMPORTS
from agents.phase import Phase, set_phase
log = logging.getLogger("orchestrator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DB_PATH = Path(os.environ.get("ORCHESTRATOR_DB", "/var/lib/triage/orchestrator.db"))
SCHEMA_PATH = Path(__file__).parent / "db_schema.sql"
MAX_PARALLEL_ISSUES = int(os.environ.get("MAX_PARALLEL_ISSUES", "8"))
VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# State enum + transition table
# ---------------------------------------------------------------------------
class IssueState(str, Enum):
    NEW = "new"
    TRIAGING = "triaging"
    TRIAGED = "triaged"
    DEV_ASSIGNED = "dev_assigned"
    DEV_IN_PROGRESS = "dev_in_progress"
    DEV_DONE = "dev_done"
    JUNIOR_QA = "junior_qa"
    JUNIOR_QA_FAIL = "junior_qa_fail"
    ACCEPTANCE_CHECK = "acceptance_check"
    MERGED_TO_DEV = "merged_to_dev"
    DEV_DEPLOY = "dev_deploy"
    SENIOR_QA = "senior_qa"
    DESIGN_QA = "design_qa"
    QA_FAIL = "qa_fail"
    READY_FOR_MAIN = "ready_for_main"
    MERGED_TO_MAIN = "merged_to_main"
    SANITY_CHECK = "sanity_check"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


S = IssueState
TRANSITIONS: dict[IssueState, set[IssueState]] = {
    S.NEW: {S.TRIAGING},
    S.TRIAGING: {S.TRIAGED, S.BLOCKED},
    S.TRIAGED: {S.DEV_ASSIGNED},
    S.DEV_ASSIGNED: {S.DEV_IN_PROGRESS, S.BLOCKED},
    S.DEV_IN_PROGRESS: {S.DEV_DONE, S.BLOCKED},
    S.DEV_DONE: {S.JUNIOR_QA},
    S.JUNIOR_QA: {S.JUNIOR_QA_FAIL, S.ACCEPTANCE_CHECK},
    S.JUNIOR_QA_FAIL: {S.DEV_IN_PROGRESS, S.BLOCKED},
    S.ACCEPTANCE_CHECK: {S.MERGED_TO_DEV, S.JUNIOR_QA_FAIL},
    S.MERGED_TO_DEV: {S.DEV_DEPLOY},
    S.DEV_DEPLOY: {S.SENIOR_QA},   # SENIOR_QA fans out to DESIGN_QA internally
    S.SENIOR_QA: {S.READY_FOR_MAIN, S.QA_FAIL, S.DESIGN_QA},
    S.DESIGN_QA: {S.READY_FOR_MAIN, S.QA_FAIL},
    S.QA_FAIL: {S.DEV_IN_PROGRESS, S.BLOCKED},
    S.READY_FOR_MAIN: {S.MERGED_TO_MAIN},
    S.MERGED_TO_MAIN: {S.SANITY_CHECK},
    S.SANITY_CHECK: {S.DONE, S.QA_FAIL},
    S.DONE: set(),
    S.BLOCKED: set(IssueState) - {S.DONE, S.CANCELLED},   # human-unblockable to most states
    S.CANCELLED: set(),
}
MAX_LOOPS = 3


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)   # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA_PATH.read_text())


def transition(linear_id: str, expected: IssueState, new: IssueState,
               actor: str, reason: str = "", payload: dict | None = None) -> bool:
    """Atomic, idempotent transition. Returns True if we won the race."""
    if new not in TRANSITIONS.get(expected, set()):
        raise ValueError(f"illegal transition {expected} -> {new}")
    now = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
    with db() as conn:
        cur = conn.execute(
            f"UPDATE issues SET prev_state=state, state=?, updated_at={now} "
            f"WHERE linear_id=? AND state=?",
            (new.value, linear_id, expected.value),
        )
        if cur.rowcount == 0:
            log.info("lost transition race for %s (%s -> %s)", linear_id, expected, new)
            return False
        conn.execute(
            "INSERT INTO state_events(linear_id, from_state, to_state, actor, reason, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (linear_id, expected.value, new.value, actor, reason,
             json.dumps(payload) if payload else None),
        )
    return True



# ---------------------------------------------------------------------------
# Worktree DB helpers (added by db_migration_001_worktree.sql)
# ---------------------------------------------------------------------------
def update_issue_worktree(linear_id: str, ref) -> None:
    """Persist the worktree ref on the issue row."""
    with db() as conn:
        conn.execute(
            "UPDATE issues SET worktree_path=?, worktree_branch=?, "
            "                  repo_full_name=COALESCE(repo_full_name, ?), "
            "                  base_branch=COALESCE(base_branch, ?) "
            "WHERE linear_id=?",
            (str(ref.worktree_path), ref.branch_name, ref.repo_full_name,
             ref.base_branch, linear_id),
        )


def worktree_ref_from_issue(issue: dict):
    """Reconstruct a worktree.WorktreeRef from a persisted issue row, or None."""
    if not issue.get("worktree_path") or not issue.get("worktree_branch"):
        return None
    from pathlib import Path as _P
    return worktree.WorktreeRef(
        repo_full_name=issue.get("repo_full_name") or "",
        issue_identifier=issue["identifier"],
        base_branch=issue.get("base_branch") or "dev",
        branch_name=issue["worktree_branch"],
        main_repo_path=_P(issue["worktree_path"]).parent / "main",
        worktree_path=_P(issue["worktree_path"]),
    )


def get_issue(linear_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM issues WHERE linear_id=?", (linear_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Webhook dedupe
# ---------------------------------------------------------------------------
def webhook_seen(source: str, delivery_id: str, event_type: str, body: bytes) -> bool:
    """Returns True if this delivery was already seen (so caller can no-op)."""
    h = hashlib.sha256(body).hexdigest()
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO webhook_inbox(source, delivery_id, event_type, payload_hash) "
                "VALUES (?, ?, ?, ?)",
                (source, delivery_id, event_type, h),
            )
            return False
        except sqlite3.IntegrityError:
            return True


# ---------------------------------------------------------------------------
# Per-issue lock pool — guarantees serialized state transitions per issue.
# ---------------------------------------------------------------------------
_issue_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_parallel_sem = asyncio.Semaphore(MAX_PARALLEL_ISSUES)


async def issue_lock(linear_id: str) -> asyncio.Lock:
    async with _locks_guard:
        return _issue_locks.setdefault(linear_id, asyncio.Lock())


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def commit_release_and_open_pr(issue: dict, *, summary: str) -> dict | None:
    """At ACCEPTANCE_CHECK -> MERGED_TO_DEV: commit, push, open PR, then release worktree.

    Returns {"commit_sha": str, "pr_number": int|None} or None if nothing to do.
    """
    ref = worktree_ref_from_issue(issue)
    if ref is None:
        log.error("no worktree to release for %s", issue["identifier"])
        return None
    sha = await worktree.commit_and_push(ref, message=summary or f"{issue['identifier']}: agent work")
    if sha is None:
        log.info("no changes to release for %s -- discarding worktree", issue["identifier"])
        await worktree.release(ref, merge=False)
        return None
    await worktree.release(ref, merge=True)
    return {"commit_sha": sha, "pr_number": None}


async def discard_worktree(issue: dict, reason: str) -> None:
    """At BLOCKED/CANCELLED: tear down the worktree without merging."""
    ref = worktree_ref_from_issue(issue)
    if ref is None:
        return
    log.info("discarding worktree for %s -- %s", issue["identifier"], reason)
    await worktree.release(ref, merge=False)


async def next_action(linear_id: str) -> None:
    """Pick what to do next given an issue's current state. Re-entrant safe."""
    lock = await issue_lock(linear_id)
    async with _parallel_sem, lock:
        issue = get_issue(linear_id)
        if not issue:
            log.warning("next_action: unknown issue %s", linear_id)
            return
        state = IssueState(issue["state"])
        log.info("next_action %s state=%s", issue["identifier"], state)

        try:
            match state:
                case S.NEW:
                    if transition(linear_id, S.NEW, S.TRIAGING, "driver"):
                        await invoke_pm_triage(issue)
                case S.TRIAGED:
                    if transition(linear_id, S.TRIAGED, S.DEV_ASSIGNED, "driver"):
                        await invoke_dev_agent(issue)
                case S.DEV_ASSIGNED:
                    # Re-enter: idempotent. Blocks if repo missing, otherwise
                    # picks up where dev work left off (or no-ops if in flight).
                    await invoke_dev_agent(issue)
                case S.DEV_DONE:
                    if transition(linear_id, S.DEV_DONE, S.JUNIOR_QA, "driver"):
                        pr = latest_pr(linear_id)
                        await invoke_junior_qa(issue, pr)
                case S.JUNIOR_QA_FAIL:
                    if issue["loop_count"] >= MAX_LOOPS:
                        force_block(linear_id, "max loops without convergence")
                        return
                    bump_loop(linear_id)
                    if transition(linear_id, S.JUNIOR_QA_FAIL, S.DEV_IN_PROGRESS, "driver"):
                        feedback = latest_qa_feedback(linear_id, kind="junior")
                        await invoke_dev_agent(issue, feedback=feedback)
                case S.ACCEPTANCE_CHECK:
                    # AC check is synchronous-ish; done by a small agent. Stubbed.
                    passed = await run_ac_check(issue)
                    if passed:
                        if transition(linear_id, S.ACCEPTANCE_CHECK, S.MERGED_TO_DEV, "driver"):
                            await merge_pr_to_dev(issue)
                    else:
                        transition(linear_id, S.ACCEPTANCE_CHECK, S.JUNIOR_QA_FAIL, "driver",
                                   reason="AC check failed")
                case S.MERGED_TO_DEV:
                    if transition(linear_id, S.MERGED_TO_DEV, S.DEV_DEPLOY, "driver"):
                        await spin_up_dev_deploy(issue)
                case S.DEV_DEPLOY:
                    if transition(linear_id, S.DEV_DEPLOY, S.SENIOR_QA, "driver"):
                        pr = latest_pr(linear_id)
                        # Senior + design fan out in parallel.
                        await asyncio.gather(
                            invoke_senior_qa(issue, pr),
                            invoke_design_qa(issue, pr),
                        )
                case S.QA_FAIL:
                    if issue["loop_count"] >= MAX_LOOPS:
                        force_block(linear_id, "max loops without convergence")
                        return
                    bump_loop(linear_id)
                    if transition(linear_id, S.QA_FAIL, S.DEV_IN_PROGRESS, "driver"):
                        feedback = latest_qa_feedback(linear_id, kind="any")
                        await invoke_dev_agent(issue, feedback=feedback)
                case S.READY_FOR_MAIN:
                    if transition(linear_id, S.READY_FOR_MAIN, S.MERGED_TO_MAIN, "driver"):
                        await merge_pr_to_main(issue)
                case S.MERGED_TO_MAIN:
                    if transition(linear_id, S.MERGED_TO_MAIN, S.SANITY_CHECK, "driver"):
                        await invoke_senior_qa(issue, latest_pr(linear_id), sanity=True)
                # in-flight states (waiting on external) — no-op
                case (S.TRIAGING | S.DEV_ASSIGNED | S.DEV_IN_PROGRESS | S.JUNIOR_QA
                      | S.SENIOR_QA | S.DESIGN_QA | S.SANITY_CHECK
                      | S.DONE | S.BLOCKED | S.CANCELLED):
                    pass
                case _:
                    log.warning("no-op for state %s on %s", state, linear_id)
        except Exception:
            log.exception("driver failed on %s", linear_id)


# ---------------------------------------------------------------------------
# Agent invocation stubs (real implementations live in separate services)
# ---------------------------------------------------------------------------
async def invoke_pm_triage(issue: dict) -> None:
    """Hand off to existing triage_service.py via internal call/queue.

    Expected: PM agent posts triage comment, sets issues.tier + harness, then transitions
    state to TRIAGED via callback POST /agent-callback.
    """
    log.info("invoke_pm_triage %s", issue["identifier"])


async def invoke_dev_agent(issue: dict, feedback: str | None = None) -> None:
    """Real dev agent: worktree -> harness -> commit -> PR -> transition DEV_DONE."""
    log.info("invoke_dev_agent %s harness=%s feedback=%s",
             issue["identifier"], issue.get("harness"), bool(feedback))
    if not issue.get("repo_full_name"):
        log.warning("issue %s has no repo_full_name -- blocking", issue["identifier"])
        set_phase(issue["linear_id"], Phase.Failed, note="no repo_full_name on issue")
        try:
            transition(issue["linear_id"], IssueState.DEV_ASSIGNED, IssueState.BLOCKED,
                       actor="dev-agent", reason="no repo_full_name on issue")
        except Exception:
            log.exception("could not transition %s to BLOCKED", issue["identifier"])
        return

    # Acquire worktree (auto-detect base branch if not set on issue).
    ref = worktree_ref_from_issue(issue)
    if ref is None:
        base = issue.get("base_branch") or _detect_default_branch(issue["repo_full_name"]) or "main"
        ref = await worktree.acquire(issue, base=base)
        update_issue_worktree(issue["linear_id"], ref)
        log.info("worktree ready at %s on branch %s", ref.worktree_path, ref.branch_name)
    set_phase(issue["linear_id"], Phase.PreparingWorkspace, note=f"worktree {ref.branch_name}")

    # Transition to DEV_IN_PROGRESS so the sweep won't double-fire.
    if not transition(issue["linear_id"], IssueState.DEV_ASSIGNED, IssueState.DEV_IN_PROGRESS,
                      actor="dev-agent"):
        log.info("dev_assigned->dev_in_progress lost race for %s — skipping", issue["identifier"])
        return
    set_phase(issue["linear_id"], Phase.LaunchingAgentProcess,
              note=f"harness={issue.get('harness')}")

    try:
        set_phase(issue["linear_id"], Phase.StreamingTurn)
        result = await dev_agent_runner.run_dev_agent(
            ref=ref, issue=issue,
            harness_id=issue.get("harness") or "claude-sonnet",
            subagent_id=issue.get("subagent"),
            feedback=feedback,
        )
        log.info("dev_agent_runner returned success=%s commit=%s", result.success, result.commit_sha)
        if not result.success:
            set_phase(issue["linear_id"], Phase.Failed, note=f"dev runner failed: {result.error}")
            transition(issue["linear_id"], IssueState.DEV_IN_PROGRESS, IssueState.BLOCKED,
                       actor="dev-agent", reason=f"dev runner failed: {result.error}")
            return
        if not result.commit_sha:
            log.info("dev agent made no changes for %s — blocking", issue["identifier"])
            set_phase(issue["linear_id"], Phase.Failed, note="agent produced no commit")
            transition(issue["linear_id"], IssueState.DEV_IN_PROGRESS, IssueState.BLOCKED,
                       actor="dev-agent", reason="agent produced no commit")
            return
        set_phase(issue["linear_id"], Phase.Finishing, note="opening PR")
        # Open PR.
        try:
            pr = await github_pr.open_pr(
                repo=issue["repo_full_name"], base=ref.base_branch, head=ref.branch_name,
                title=f"{issue['identifier']}: {issue.get('title') or 'agent work'}",
                body=result.summary or f"Auto-generated by {issue.get('harness')} for {issue['identifier']}.",
            )
            with db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO pr_links(linear_id, pr_number, repo, head_sha, opened_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (issue["linear_id"], pr.number, issue["repo_full_name"], result.commit_sha),
                )
            log.info("opened PR %s#%d for %s", issue["repo_full_name"], pr.number, issue["identifier"])
        except Exception as exc:
            log.exception("PR open failed for %s: %s", issue["identifier"], exc)
            # Don't block — we still want junior QA to run on the worktree even if PR push failed.
        transition(issue["linear_id"], IssueState.DEV_IN_PROGRESS, IssueState.DEV_DONE,
                   actor="dev-agent", reason="dev complete + PR (or local) ready")
        set_phase(issue["linear_id"], Phase.Succeeded, note="PR opened, DEV_DONE")
    except Exception as exc:
        log.exception("dev agent invocation crashed for %s", issue["identifier"])
        set_phase(issue["linear_id"], Phase.Failed, note=f"dev crash: {exc!r}")
        transition(issue["linear_id"], IssueState.DEV_IN_PROGRESS, IssueState.BLOCKED,
                   actor="dev-agent", reason=f"dev crash: {exc!r}")


def _detect_default_branch(repo_full_name: str) -> str | None:
    """Best-effort auto-detect: rely on `git symbolic-ref origin/HEAD` after clone."""
    try:
        import subprocess, re as _re
        safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", repo_full_name)
        main = Path("/root/work/repos") / safe / "main"
        if not (main / ".git").exists():
            return None
        head = subprocess.check_output(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=main, text=True,
        ).strip()
        return head.rsplit("/", 1)[-1] if head else None
    except Exception:
        return None



async def invoke_junior_qa(issue: dict, pr: dict | None) -> None:
    """Junior QA: detect framework, run tests, do AC check, persist verdict."""
    log.info("invoke_junior_qa %s pr=%s", issue["identifier"], pr.get("pr_number") if pr else None)
    set_phase(issue["linear_id"], Phase.PreparingWorkspace, note="junior QA")
    ref = worktree_ref_from_issue(issue)
    if ref is None:
        log.warning("junior_qa: no worktree for %s — blocking", issue["identifier"])
        set_phase(issue["linear_id"], Phase.Failed, note="no worktree at junior QA")
        transition(issue["linear_id"], IssueState.JUNIOR_QA, IssueState.BLOCKED,
                   actor="junior-qa", reason="no worktree at junior QA")
        return
    set_phase(issue["linear_id"], Phase.LaunchingAgentProcess, note="junior QA test runner")
    set_phase(issue["linear_id"], Phase.StreamingTurn, note="running tests + AC check")
    qa = await junior_qa.run_junior_qa(ref, issue)
    ac = await ac_check.check_acceptance_criteria(ref, issue)
    passed = qa.passed and ac.passed
    feedback_md = (
        f"### Tests ({qa.test_runner}) — {qa.duration_s:.1f}s\n\n{qa.feedback or qa.summary}\n\n"
        f"### AC check — {ac.confidence}\n\n{ac.reasoning}"
    )
    set_phase(issue["linear_id"], Phase.Finishing, note="persisting junior verdict")
    with db() as conn:
        conn.execute(
            "INSERT INTO qa_runs(linear_id, kind, verdict, feedback, payload, ran_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (issue["linear_id"], "junior",
             "pass" if passed else "fail", feedback_md,
             json.dumps({"junior_qa": qa.__dict__, "ac_check": ac.__dict__})),
        )
    if passed:
        transition(issue["linear_id"], IssueState.JUNIOR_QA, IssueState.ACCEPTANCE_CHECK,
                   actor="junior-qa")
        set_phase(issue["linear_id"], Phase.Succeeded, note="junior QA pass")
    else:
        transition(issue["linear_id"], IssueState.JUNIOR_QA, IssueState.JUNIOR_QA_FAIL,
                   actor="junior-qa", reason=feedback_md[:200])
        set_phase(issue["linear_id"], Phase.Failed, note="junior QA fail")


async def invoke_senior_qa(issue: dict, pr: dict | None, *, sanity: bool = False) -> None:
    """Senior QA: spin up dev deploy, run visual-qa, record verdict."""
    log.info("invoke_senior_qa %s sanity=%s", issue["identifier"], sanity)
    set_phase(issue["linear_id"], Phase.PreparingWorkspace,
              note=f"senior QA sanity={sanity}")
    ref = worktree_ref_from_issue(issue)
    if ref is None:
        log.warning("senior_qa: no worktree for %s — blocking", issue["identifier"])
        set_phase(issue["linear_id"], Phase.Failed, note="no worktree at senior QA")
        transition(issue["linear_id"], IssueState.SENIOR_QA, IssueState.BLOCKED,
                   actor="senior-qa", reason="no worktree at senior QA")
        return
    try:
        set_phase(issue["linear_id"], Phase.LaunchingAgentProcess, note="spin up dev deploy")
        deploy = await dev_deploy.spin_up(ref, issue)
        set_phase(issue["linear_id"], Phase.StreamingTurn, note="visual-qa running")
        verdict = await senior_qa.run_senior_qa(deploy, issue, sanity=sanity)
        set_phase(issue["linear_id"], Phase.Finishing, note="persisting senior verdict")
        with db() as conn:
            conn.execute(
                "INSERT INTO qa_runs(linear_id, kind, verdict, feedback, payload, ran_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (issue["linear_id"], "senior_sanity" if sanity else "senior",
                 "pass" if verdict.passed else "fail",
                 verdict.summary + "\n\n" + verdict.feedback,
                 json.dumps({"run_id": verdict.visual_qa_run_id, "screenshots": verdict.screenshots})),
            )
        set_phase(
            issue["linear_id"],
            Phase.Succeeded if verdict.passed else Phase.Failed,
            note=f"senior QA {'pass' if verdict.passed else 'fail'}",
        )
    except Exception as exc:
        set_phase(issue["linear_id"], Phase.Failed, note=f"senior QA crash: {exc!r}")
        raise
    finally:
        try:
            if "deploy" in locals():
                await dev_deploy.tear_down(deploy)
        except Exception:
            log.exception("tear_down failed (non-fatal) for %s", issue["identifier"])


async def invoke_design_qa(issue: dict, pr: dict | None) -> None:
    """Design QA: Penpot tokens vs live UI, drift check."""
    log.info("invoke_design_qa %s", issue["identifier"])
    set_phase(issue["linear_id"], Phase.PreparingWorkspace, note="design QA")
    # We expect a deploy URL by this point — spun up by senior_qa or earlier stage.
    # For now we redo spin_up (cheap if cached) so design can run independently.
    ref = worktree_ref_from_issue(issue)
    if ref is None:
        set_phase(issue["linear_id"], Phase.Failed, note="no worktree at design QA")
        return
    deploy = None
    try:
        set_phase(issue["linear_id"], Phase.LaunchingAgentProcess, note="spin up dev deploy")
        deploy = await dev_deploy.spin_up(ref, issue)
        set_phase(issue["linear_id"], Phase.StreamingTurn, note="design drift check")
        verdict = await design_qa.run_design_qa(issue, deploy.url)
        set_phase(issue["linear_id"], Phase.Finishing, note="persisting design verdict")
        with db() as conn:
            conn.execute(
                "INSERT INTO qa_runs(linear_id, kind, verdict, feedback, payload, ran_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (issue["linear_id"], "design",
                 "pass" if verdict.passed else "fail",
                 verdict.summary + "\n\n" + verdict.feedback,
                 json.dumps({"drift_pct": verdict.drift_pct,
                             "screenshots": verdict.screenshots,
                             "drifts": [d.__dict__ for d in verdict.drifts]})),
            )
        set_phase(
            issue["linear_id"],
            Phase.Succeeded if verdict.passed else Phase.Failed,
            note=f"design QA {'pass' if verdict.passed else 'fail'}",
        )
    except Exception as exc:
        set_phase(issue["linear_id"], Phase.Failed, note=f"design QA crash: {exc!r}")
        raise
    finally:
        try:
            if deploy is not None:
                await dev_deploy.tear_down(deploy)
        except Exception:
            log.exception("tear_down failed (non-fatal) for %s", issue["identifier"])


async def run_ac_check(issue: dict) -> bool:
    """Strict acceptance-criteria gate. Stubbed — real impl calls a small Claude agent."""
    log.info("run_ac_check %s", issue["identifier"])
    return True


async def merge_pr_to_dev(issue: dict) -> None:
    log.info("merge_pr_to_dev %s", issue["identifier"])


async def merge_pr_to_main(issue: dict) -> None:
    log.info("merge_pr_to_main %s", issue["identifier"])


async def spin_up_dev_deploy(issue: dict) -> None:
    """docker compose -p dev-<id> -f docker-compose.dev.yml up -d, set deploy_url."""
    log.info("spin_up_dev_deploy %s", issue["identifier"])


# ---------------------------------------------------------------------------
# Tiny helpers (stubs)
# ---------------------------------------------------------------------------
def latest_pr(linear_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM pr_links WHERE linear_id=? ORDER BY id DESC LIMIT 1",
            (linear_id,),
        ).fetchone()
    return dict(row) if row else None


def latest_qa_feedback(linear_id: str, kind: str = "any") -> str:
    sql = ("SELECT feedback FROM qa_runs WHERE linear_id=? "
           + ("" if kind == "any" else "AND kind=? ")
           + "ORDER BY id DESC LIMIT 1")
    params = (linear_id,) if kind == "any" else (linear_id, kind)
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
    return (row["feedback"] if row else "") or ""


def bump_loop(linear_id: str) -> None:
    with db() as conn:
        conn.execute("UPDATE issues SET loop_count = loop_count + 1, "
                     "current_attempt = current_attempt + 1 WHERE linear_id=?", (linear_id,))


def force_block(linear_id: str, reason: str) -> None:
    with db() as conn:
        conn.execute("UPDATE issues SET state=?, blocked_reason=? WHERE linear_id=?",
                     (S.BLOCKED.value, reason, linear_id))
        conn.execute("INSERT INTO state_events(linear_id, from_state, to_state, actor, reason) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (linear_id, None, S.BLOCKED.value, "driver", reason))


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------
def verify_sig(body: bytes, sig: str | None, env: str) -> bool:
    secret = os.environ.get(env, "")
    if not secret:
        log.warning("%s unset — accepting (DEV ONLY)", env)
        return True
    if not sig:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # GitHub prefixes with "sha256="
    sig = sig.removeprefix("sha256=")
    return hmac.compare_digest(expected, sig)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="orchestrator")

# Symphony-style operator control surface (added by Phase 1 upgrade).
try:
    from agents.control_api import router as _control_router, dashboard as _dashboard
    app.include_router(_control_router, prefix="/api/v1")
    app.add_api_route("/", _dashboard, methods=["GET"], include_in_schema=False)
except Exception as _e:  # noqa: BLE001
    log.exception("control_api mount failed -- dashboard unavailable: %s", _e)


@app.on_event("startup")
async def _startup() -> None:
    init_db()
    workflow.start_watcher()
    log.info("orchestrator up -- db=%s max_parallel=%d workflow=%s",
             DB_PATH, workflow.max_concurrent(), workflow.WORKFLOW_PATH)
    # Periodic sweep -- catches dropped webhooks / stuck issues.
    asyncio.create_task(_sweep_loop())


async def _sweep_loop() -> None:
    while True:
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT linear_id FROM issues WHERE state NOT IN (?, ?, ?)",
                    (S.DONE.value, S.CANCELLED.value, S.BLOCKED.value),
                ).fetchall()
            for r in rows:
                asyncio.create_task(next_action(r["linear_id"]))
        except Exception:
            log.exception("sweep failed")
        await asyncio.sleep(workflow.polling_interval_s())


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhook/linear")
async def webhook_linear(
    request: Request,
    background: BackgroundTasks,
    linear_signature: str | None = Header(default=None, alias="Linear-Signature"),
    linear_delivery: str | None = Header(default=None, alias="Linear-Delivery"),
) -> dict:
    body = await request.body()
    if not verify_sig(body, linear_signature, "LINEAR_WEBHOOK_SECRET"):
        raise HTTPException(401, "bad signature")
    event = json.loads(body)
    event_type = f"{event.get('type')}.{event.get('action')}"
    delivery_id = linear_delivery or hashlib.sha256(body).hexdigest()[:16]
    if webhook_seen("linear", delivery_id, event_type, body):
        return {"ok": True, "deduped": True}

    if event.get("type") == "Issue" and event.get("action") == "create":
        data = event.get("data") or {}
        linear_id = data.get("id")
        identifier = data.get("identifier")
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO issues(linear_id, identifier, title, team_key, state) "
                "VALUES (?, ?, ?, ?, ?)",
                (linear_id, identifier, data.get("title", ""),
                 (data.get("team") or {}).get("key", ""), S.NEW.value),
            )
        background.add_task(next_action, linear_id)
    # other event types (Comment.create with !rerun etc.) can hook in here.
    return {"ok": True}


@app.post("/webhook/github")
async def webhook_github(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
) -> dict:
    body = await request.body()
    if not verify_sig(body, x_hub_signature_256, "GITHUB_WEBHOOK_SECRET"):
        raise HTTPException(401, "bad signature")
    if webhook_seen("github", x_github_delivery or "", x_github_event or "", body):
        return {"ok": True, "deduped": True}
    payload = json.loads(body)

    # Pull request lifecycle.
    if x_github_event == "pull_request":
        pr = payload.get("pull_request") or {}
        action = payload.get("action")
        linear_id = _find_linear_id_for_pr(pr)
        if not linear_id:
            return {"ok": True, "unlinked": True}
        if action == "opened":
            _record_pr(linear_id, payload)
            background.add_task(_advance_on_pr_opened, linear_id)
        elif action == "closed" and pr.get("merged"):
            background.add_task(_advance_on_pr_merged, linear_id, pr)

    # Senior/junior QA agents (running outside the orchestrator) report verdicts here.
    return {"ok": True}


@app.post("/agent-callback")
async def agent_callback(request: Request, background: BackgroundTasks) -> dict:
    """Called by dev/QA agents to report results.

    Body:
      {linear_id, kind: 'dev'|'junior_qa'|'senior_qa'|'design_qa'|'ac'|'sanity',
       attempt, verdict?, feedback?, artifacts_url?, pr_number?, commit_sha?,
       cost_usd?, duration_s?}
    """
    cb = await request.json()
    linear_id = cb["linear_id"]
    kind = cb["kind"]
    # ... record qa_runs row, then drive state machine forward ...
    log.info("agent-callback %s kind=%s verdict=%s", linear_id, kind, cb.get("verdict"))
    background.add_task(next_action, linear_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Small internal helpers (stubs)
# ---------------------------------------------------------------------------
def _find_linear_id_for_pr(pr: dict) -> str | None:
    # Heuristic: branch name starts with <identifier>-<slug>. Falls back to PR body magic words.
    branch = pr.get("head", {}).get("ref", "")
    import re
    m = re.match(r"([A-Z]+-\d+)", branch.upper())
    if m:
        identifier = m.group(1)
        with db() as conn:
            row = conn.execute("SELECT linear_id FROM issues WHERE identifier=?",
                               (identifier,)).fetchone()
        if row:
            return row["linear_id"]
    return None


def _record_pr(linear_id: str, payload: dict) -> None:
    pr = payload["pull_request"]
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO pr_links(linear_id, repo_full_name, pr_number, "
            "base_branch, head_branch, state) VALUES (?, ?, ?, ?, ?, ?)",
            (linear_id, payload["repository"]["full_name"], pr["number"],
             pr["base"]["ref"], pr["head"]["ref"], "open"),
        )


async def _advance_on_pr_opened(linear_id: str) -> None:
    issue = get_issue(linear_id)
    if issue and issue["state"] == S.DEV_IN_PROGRESS.value:
        transition(linear_id, S.DEV_IN_PROGRESS, S.DEV_DONE, "webhook:github")
    await next_action(linear_id)


async def _advance_on_pr_merged(linear_id: str, pr: dict) -> None:
    base = pr.get("base", {}).get("ref")
    issue = get_issue(linear_id)
    if not issue:
        return
    if base == "dev" and issue["state"] == S.ACCEPTANCE_CHECK.value:
        transition(linear_id, S.ACCEPTANCE_CHECK, S.MERGED_TO_DEV, "webhook:github")
    elif base == "main" and issue["state"] == S.READY_FOR_MAIN.value:
        transition(linear_id, S.READY_FOR_MAIN, S.MERGED_TO_MAIN, "webhook:github")
    await next_action(linear_id)
