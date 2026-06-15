"""
dev_agent_runner — launches an AI coding agent inside a per-issue git worktree
and returns a structured result describing the work it produced.

Designed for the Hetzner orchestrator. Harnesses supported:
    claude-sonnet, claude-opus-thinking   (tmux + paste-buffer)
    codex-gpt5                            (tmux + paste-buffer)
    pi-kimi                               (one-shot subprocess)
    opencode-kimi                         (one-shot subprocess)
    aider-kimi, aider-deepseek            (one-shot subprocess, self-committing)

The module is import-safe: it does not touch the network or filesystem at
import time.  All work happens inside :func:`run_dev_agent`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from worktree import WorktreeRef  # type: ignore


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class DevRunResult:
    success: bool
    commit_sha: str | None        # None if no changes were made
    summary: str                  # short markdown summary the agent produced
    transcript_path: str | None   # path to logs / tmux capture for debugging
    error: str | None


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


LOG_ROOT = Path("/var/lib/triage/dev-runs")

# Harnesses that run inside a long-lived tmux session and stream tokens.
TMUX_HARNESSES = {"claude-sonnet", "claude-opus-thinking", "codex-gpt5"}

# Harnesses that aider auto-commit; we should not commit ourselves afterwards.
SELF_COMMITTING_HARNESSES = {"aider-kimi", "aider-deepseek"}

# Polling cadence inside the tmux completion-wait loop.
TMUX_POLL_S = 4.0
# How long the tmux output must be unchanged after the sentinel fires before
# we consider the agent truly idle.
TMUX_IDLE_GRACE_S = 8.0


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


async def run_dev_agent(
    ref: "WorktreeRef",
    issue: dict,
    harness_id: str,
    subagent_id: str | None,
    feedback: str | None = None,
    timeout_s: int = 1800,
) -> DevRunResult:
    """Run a single dev-agent attempt inside ``ref.worktree_path``.

    The function is idempotent at the worktree level: it never mutates the
    base branch or shared state.  All file edits happen inside
    ``ref.worktree_path`` and any resulting commit is made on
    ``ref.branch_name``.
    """

    nonce = secrets.token_hex(4)
    sentinel = f"[[DEV_DONE_{nonce}]]"

    log_dir = LOG_ROOT / ref.issue_identifier
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{ts}-{harness_id}-{nonce}.log"

    prompt = _build_prompt(
        ref=ref,
        issue=issue,
        subagent_id=subagent_id,
        feedback=feedback,
        sentinel_literal=sentinel,
    )

    try:
        if harness_id in TMUX_HARNESSES:
            ok, err = await _run_tmux_harness(
                ref=ref,
                harness_id=harness_id,
                prompt=prompt,
                sentinel=sentinel,
                log_path=log_path,
                timeout_s=timeout_s,
            )
        elif harness_id == "pi-kimi":
            ok, err = await _run_pi(ref, prompt, log_path, timeout_s)
        elif harness_id == "opencode-kimi":
            ok, err = await _run_opencode(ref, prompt, log_path, timeout_s)
        elif harness_id == "aider-kimi":
            ok, err = await _run_aider(
                ref, prompt, log_path, timeout_s,
                model="openai/kimi-for-coding",
                api_base="https://api.kimi.com/coding/v1",
                api_key_env="KIMI_API_KEY",
            )
        elif harness_id == "aider-deepseek":
            ok, err = await _run_aider(
                ref, prompt, log_path, timeout_s,
                model="openai/deepseek-v4-pro",
                api_base="http://127.0.0.1:3000/v1",
                api_key_env="PREM_API_KEY",
            )
        else:
            return DevRunResult(
                success=False,
                commit_sha=None,
                summary="",
                transcript_path=str(log_path) if log_path.exists() else None,
                error=f"unknown harness_id: {harness_id!r}",
            )
    except asyncio.TimeoutError:
        return DevRunResult(
            success=False,
            commit_sha=None,
            summary="",
            transcript_path=str(log_path),
            error=f"harness {harness_id} timed out after {timeout_s}s",
        )
    except Exception as exc:  # noqa: BLE001 — surface any harness blow-up
        return DevRunResult(
            success=False,
            commit_sha=None,
            summary="",
            transcript_path=str(log_path) if log_path.exists() else None,
            error=f"{type(exc).__name__}: {exc}",
        )

    summary = _read_summary(ref) or _auto_summary(issue, harness_id)

    if not ok:
        return DevRunResult(
            success=False,
            commit_sha=None,
            summary=summary,
            transcript_path=str(log_path),
            error=err or f"{harness_id} reported failure",
        )

    # Detect & commit changes (aider already commits — but we still pick up
    # its sha so callers get a uniform contract).
    commit_sha = await _finalise_commit(
        ref=ref,
        issue=issue,
        harness_id=harness_id,
        summary=summary,
    )

    return DevRunResult(
        success=True,
        commit_sha=commit_sha,
        summary=summary,
        transcript_path=str(log_path),
        error=None,
    )


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def _build_prompt(
    *,
    ref: "WorktreeRef",
    issue: dict,
    subagent_id: str | None,
    feedback: str | None,
    sentinel_literal: str,
) -> str:
    description = (issue.get("description") or "").strip() or "(no description provided)"
    acceptance = (issue.get("acceptance_criteria") or "").strip()
    title = issue.get("title") or "(no title)"

    feedback_section = ""
    if feedback:
        feedback_section = (
            "Previous QA feedback (address before finishing this loop):\n"
            "```\n"
            f"{feedback.strip()}\n"
            "```\n"
        )

    subagent_line = ""
    if subagent_id:
        subagent_line = (
            f"\nUse the subagent persona: `{subagent_id}` "
            "(see `.claude/agents/` in the repo).\n"
        )

    prompt = f"""You are working on Linear issue {issue.get("identifier")}: {title}.

Description:
{description}

Acceptance criteria:
{acceptance or "Use your judgement based on the description."}

Repository: {ref.repo_full_name}
Branch: {ref.branch_name} (off {ref.base_branch})
Worktree: {ref.worktree_path}
{subagent_line}
{feedback_section}
Task:
1. Read the description carefully.
2. Make the smallest correct change to address the issue.
3. Add or update tests where appropriate for this codebase.
4. When done, write a one-paragraph summary in `_AGENT_SUMMARY.md` at the worktree root.

When you have finished, output the literal token {sentinel_literal} on its own line and stop.
"""
    return prompt


def _auto_summary(issue: dict, harness_id: str) -> str:
    return (
        f"Automated dev run for **{issue.get('identifier')}** "
        f"({issue.get('title', '')}) via `{harness_id}`. "
        "No `_AGENT_SUMMARY.md` was produced by the agent."
    )


def _read_summary(ref: "WorktreeRef") -> str | None:
    p = Path(ref.worktree_path) / "_AGENT_SUMMARY.md"
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


# --------------------------------------------------------------------------- #
# Commit handling
# --------------------------------------------------------------------------- #


async def _finalise_commit(
    *,
    ref: "WorktreeRef",
    issue: dict,
    harness_id: str,
    summary: str,
) -> str | None:
    """Commit any pending changes via the worktree manager.

    For self-committing harnesses (aider) we still call the worktree's
    ``commit_and_push`` so the manager can pick up the existing HEAD and
    push it.  ``commit_and_push`` returns ``None`` if there is nothing
    new — that's a legitimate "no-op" outcome.
    """
    # Late import to avoid hard coupling at module import time.
    from worktree import commit_and_push  # type: ignore

    first_line = (summary.splitlines() or [""])[0].strip()
    if not first_line:
        first_line = f"{issue.get('identifier')} dev run"
    # Conventional-ish subject; trailer includes harness + linear id.
    message = (
        f"{first_line}\n\n"
        f"Linear-Id: {issue.get('identifier')}\n"
        f"Harness: {harness_id}\n"
    )
    try:
        sha = await commit_and_push(ref, message)
    except Exception as exc:  # noqa: BLE001
        # Don't lose the dev result over a push failure — surface in summary.
        raise RuntimeError(f"commit_and_push failed: {exc}") from exc
    return sha


# --------------------------------------------------------------------------- #
# Tmux harnesses (claude / codex)
# --------------------------------------------------------------------------- #


def _tmux_session_name(harness_id: str, identifier: str) -> str:
    # Tmux session names must be unique per worktree AND tolerate retries.
    # We include a short random suffix to prevent collisions if a previous
    # run was orphaned. Sanitised: tmux disallows '.', ':' in session names.
    safe_id = identifier.replace(":", "-").replace(".", "-").lower()
    suffix = secrets.token_hex(2)
    short = "claude" if harness_id.startswith("claude") else "codex"
    return f"dev-{short}-{safe_id}-{suffix}"


def _tmux_invoke_command(harness_id: str) -> str:
    if harness_id == "claude-sonnet":
        return "IS_SANDBOX=1 claude --model sonnet --dangerously-skip-permissions"
    if harness_id == "claude-opus-thinking":
        return (
            "IS_SANDBOX=1 claude --model opus "
            "--thinking --dangerously-skip-permissions"
        )
    if harness_id == "codex-gpt5":
        # codex has its own sandboxing; no --dangerously-skip-permissions.
        return "codex --model gpt-5"
    raise ValueError(f"no tmux invoke for {harness_id}")


async def _run_tmux_harness(
    *,
    ref: "WorktreeRef",
    harness_id: str,
    prompt: str,
    sentinel: str,
    log_path: Path,
    timeout_s: int,
) -> tuple[bool, str | None]:
    session = _tmux_session_name(harness_id, ref.issue_identifier)
    invoke = _tmux_invoke_command(harness_id)
    workdir = ref.worktree_path

    # 1. Start the tmux session in the worktree dir.
    start = [
        "tmux", "new-session", "-d", "-s", session,
        "-c", workdir,
        invoke,
    ]
    rc, _, err = await _exec(start)
    if rc != 0:
        return False, f"tmux new-session failed: {err.strip()}"

    try:
        # 2. Give the agent a beat to draw its UI before we paste.
        await asyncio.sleep(2.0)

        # 3. Inject the prompt via paste-buffer.  This is markedly more reliable
        #    than `send-keys -l` for long multi-line strings.
        await _tmux_paste(session, prompt + "\n")

        # 4. Wait for the sentinel + an idle window.
        ok, err = await _await_sentinel(
            session=session,
            sentinel=sentinel,
            log_path=log_path,
            timeout_s=timeout_s,
        )
        return ok, err
    finally:
        # 5. Capture final pane then kill the session.
        await _tmux_capture(session, log_path, append=True)
        await _exec(["tmux", "kill-session", "-t", session])


async def _tmux_paste(session: str, text: str) -> None:
    buf_name = f"buf-{secrets.token_hex(3)}"
    # load-buffer reads from stdin when path is '-'.
    proc = await asyncio.create_subprocess_exec(
        "tmux", "load-buffer", "-b", buf_name, "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(text.encode("utf-8"))
    await _exec(["tmux", "paste-buffer", "-b", buf_name, "-t", session])
    await _exec(["tmux", "delete-buffer", "-b", buf_name])
    # Some TUIs need an Enter keypress to submit after a paste.
    await _exec(["tmux", "send-keys", "-t", session, "Enter"])


async def _tmux_capture(session: str, log_path: Path, *, append: bool) -> str:
    rc, out, _ = await _exec(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", "-100000"]
    )
    if rc != 0:
        return ""
    mode = "ab" if append else "wb"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, mode) as f:
        f.write(out.encode("utf-8", errors="replace"))
    return out


async def _await_sentinel(
    *,
    session: str,
    sentinel: str,
    log_path: Path,
    timeout_s: int,
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + timeout_s
    sentinel_seen_at: float | None = None
    last_capture = ""

    while time.monotonic() < deadline:
        await asyncio.sleep(TMUX_POLL_S)
        capture = await _tmux_capture(session, log_path, append=False)

        if sentinel in capture:
            if sentinel_seen_at is None:
                sentinel_seen_at = time.monotonic()
            # Once we've seen the sentinel, wait for output to settle.
            if capture == last_capture and (
                time.monotonic() - sentinel_seen_at >= TMUX_IDLE_GRACE_S
            ):
                return True, None
        last_capture = capture

    return False, f"sentinel {sentinel!r} not seen within {timeout_s}s"


# --------------------------------------------------------------------------- #
# One-shot harnesses
# --------------------------------------------------------------------------- #


async def _run_pi(
    ref: "WorktreeRef",
    prompt: str,
    log_path: Path,
    timeout_s: int,
) -> tuple[bool, str | None]:
    cmd = [
        "pi", "-p",
        "--provider", "kimi-coding",
        "--model", "k2p5",
        prompt,
    ]
    return await _run_oneshot(cmd, cwd=ref.worktree_path, log_path=log_path, timeout_s=timeout_s)


async def _run_opencode(
    ref: "WorktreeRef",
    prompt: str,
    log_path: Path,
    timeout_s: int,
) -> tuple[bool, str | None]:
    cmd = [
        "opencode", "run",
        "--model", "kimi-coding/kimi-for-coding",
        prompt,
    ]
    return await _run_oneshot(cmd, cwd=ref.worktree_path, log_path=log_path, timeout_s=timeout_s)


async def _run_aider(
    ref: "WorktreeRef",
    prompt: str,
    log_path: Path,
    timeout_s: int,
    *,
    model: str,
    api_base: str,
    api_key_env: str,
) -> tuple[bool, str | None]:
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return False, f"{api_key_env} not set in environment"

    cmd = [
        "aider",
        "--model", model,
        "--openai-api-base", api_base,
        "--openai-api-key", api_key,
        "--yes-always",
        "--message", prompt,
    ]
    # Check aider is installed before spawning a tmux/process. Cleaner error.
    rc, _, _ = await _exec(["which", "aider"])
    if rc != 0:
        return False, "aider CLI not found on PATH (pip install aider-chat)"
    return await _run_oneshot(cmd, cwd=ref.worktree_path, log_path=log_path, timeout_s=timeout_s)


async def _run_oneshot(
    cmd: list[str],
    *,
    cwd: str,
    log_path: Path,
    timeout_s: int,
) -> tuple[bool, str | None]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as logf:
        logf.write(
            f"\n# {' '.join(shlex.quote(c) for c in cmd[:6])} ...\n".encode()
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )

        async def _drain() -> None:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    return
                logf.write(chunk)
                logf.flush()

        drain_task = asyncio.create_task(_drain())
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await drain_task
            return False, f"process exceeded {timeout_s}s and was killed"
        await drain_task

    if proc.returncode != 0:
        return False, f"exit {proc.returncode}"
    return True, None


# --------------------------------------------------------------------------- #
# Subprocess helper
# --------------------------------------------------------------------------- #


async def _exec(cmd: list[str]) -> tuple[int, str, str]:
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
