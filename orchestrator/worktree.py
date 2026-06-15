"""Per-issue git worktree manager.

Wraps `gtr` (CodeRabbit's git-worktree-runner) to give each Linear issue an
isolated checkout. Lifecycle:

    acquire(issue, base="dev") -> WorktreeRef         # idempotent
    release(ref, merge=True)                          # merge agent/<id> into base then rm
    release(ref, merge=False)                         # discard the branch + worktree

A "repo" is identified by `repo_full_name` (e.g. "premAI-io/foo"). The main
clone lives at /root/work/repos/<safe_name>/main/. Each per-issue worktree is
a sibling folder: /root/work/repos/<safe_name>/<issue_identifier>/.

If the main clone doesn't exist yet, we clone it from
https://x-access-token:$GITHUB_TOKEN@github.com/<repo_full_name>.git so future
fetch/push uses the orchestrator's PAT.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

REPOS_ROOT = Path("/root/work/repos")
DEFAULT_REMOTE = "origin"


@dataclass
class WorktreeRef:
    repo_full_name: str           # "premAI-io/foo"
    issue_identifier: str         # "ENG-42"
    base_branch: str              # "dev"
    branch_name: str              # "agent/ENG-42"
    main_repo_path: Path          # /root/work/repos/premAI-io_foo/main
    worktree_path: Path           # /root/work/repos/premAI-io_foo/ENG-42

    def as_dict(self) -> dict[str, str]:
        return {
            "repo_full_name": self.repo_full_name,
            "issue_identifier": self.issue_identifier,
            "base_branch": self.base_branch,
            "branch_name": self.branch_name,
            "main_repo_path": str(self.main_repo_path),
            "worktree_path": str(self.worktree_path),
        }


def _safe_repo_name(repo_full_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", repo_full_name)


def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
         env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, env=env,
        capture_output=True, text=True, check=check,
    )


def _clone_url(repo_full_name: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return f"https://x-access-token:{token}@github.com/{repo_full_name}.git"
    return f"https://github.com/{repo_full_name}.git"


async def _arun(cmd: list[str], *, cwd: Path | None = None,
                env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(_run, cmd, cwd=cwd, env_extra=env_extra)


# ---------------------------------------------------------------------------
# Main clone lifecycle
# ---------------------------------------------------------------------------
async def ensure_main_clone(repo_full_name: str) -> Path:
    """Idempotently clone the main repo to /root/work/repos/<safe>/main/."""
    safe = _safe_repo_name(repo_full_name)
    main_path = REPOS_ROOT / safe / "main"
    if (main_path / ".git").exists():
        log.debug("main clone present: %s", main_path)
        # Best-effort fetch so worktree creation off origin/<base> finds the ref.
        try:
            await _arun(["git", "fetch", "--all", "--prune", "--quiet"], cwd=main_path)
        except subprocess.CalledProcessError:
            log.warning("git fetch failed in %s — continuing with what's local", main_path)
        return main_path

    main_path.parent.mkdir(parents=True, exist_ok=True)
    url = _clone_url(repo_full_name)
    log.info("cloning %s -> %s", repo_full_name, main_path)
    await _arun(["git", "clone", "--quiet", url, str(main_path)])
    # Pre-configure user for future commits made by agents.
    await _arun(["git", "config", "user.name",  "Hetzner Orchestrator"], cwd=main_path)
    await _arun(["git", "config", "user.email", "orchestrator@local-pcci.org"], cwd=main_path)
    return main_path


# ---------------------------------------------------------------------------
# Worktree lifecycle
# ---------------------------------------------------------------------------
async def acquire(issue: dict, base: str = "dev") -> WorktreeRef:
    """Idempotent: return a WorktreeRef for this issue, creating if missing."""
    repo_full_name = issue.get("repo_full_name") or issue["repo_full_name"]  # surface KeyError early
    identifier = issue["identifier"]                                          # e.g. "ENG-42"
    branch_name = identifier

    main_path = await ensure_main_clone(repo_full_name)
    safe = _safe_repo_name(repo_full_name)
    worktree_path = main_path.parent / "main-worktrees" / identifier

    if worktree_path.exists():
        log.info("worktree exists, reusing: %s", worktree_path)
    else:
        log.info("creating worktree %s on branch %s (base=%s)",
                 worktree_path, branch_name, base)
        # Prefer gtr (handles config copy, post-hooks); fall back to raw git worktree.
        try:
            await _arun(
                ["git", "gtr", "new", identifier,
                 "--from", f"{DEFAULT_REMOTE}/{base}",
                 "--folder", identifier,
                 "--no-fetch"],
                cwd=main_path,
            )
        except subprocess.CalledProcessError as exc:
            log.warning("gtr new failed (stderr=%r); falling back to git worktree add",
                        (exc.stderr or "")[:200])
            await _arun(
                ["git", "worktree", "add", "-b", branch_name,
                 str(worktree_path), f"{DEFAULT_REMOTE}/{base}"],
                cwd=main_path,
            )

    return WorktreeRef(
        repo_full_name=repo_full_name, issue_identifier=identifier,
        base_branch=base, branch_name=branch_name,
        main_repo_path=main_path, worktree_path=worktree_path,
    )


async def commit_and_push(ref: WorktreeRef, message: str) -> str | None:
    """Stage all changes in the worktree, commit, push. Returns commit SHA or None."""
    # Anything to commit?
    proc = await _arun(["git", "status", "--porcelain"], cwd=ref.worktree_path)
    if not proc.stdout.strip():
        log.info("nothing to commit in %s", ref.worktree_path)
        return None
    await _arun(["git", "add", "-A"], cwd=ref.worktree_path)
    await _arun(["git", "commit", "-m", message], cwd=ref.worktree_path)
    sha_proc = await _arun(["git", "rev-parse", "HEAD"], cwd=ref.worktree_path)
    sha = sha_proc.stdout.strip()
    await _arun(["git", "push", "-u", DEFAULT_REMOTE, ref.branch_name], cwd=ref.worktree_path)
    return sha


async def release(ref: WorktreeRef, *, merge: bool) -> None:
    """Merge into base (if merge=True) and remove the worktree. Idempotent."""
    if merge:
        # Merge happens on the main checkout (which sits on base after fetch).
        log.info("merging %s into %s on %s", ref.branch_name, ref.base_branch, ref.main_repo_path)
        await _arun(["git", "fetch", DEFAULT_REMOTE, "--quiet"], cwd=ref.main_repo_path)
        await _arun(["git", "checkout", ref.base_branch], cwd=ref.main_repo_path)
        await _arun(["git", "pull", "--ff-only", DEFAULT_REMOTE, ref.base_branch], cwd=ref.main_repo_path)
        await _arun(["git", "merge", "--no-ff", "-m", f"merge {ref.branch_name}", ref.branch_name],
                    cwd=ref.main_repo_path)
        await _arun(["git", "push", DEFAULT_REMOTE, ref.base_branch], cwd=ref.main_repo_path)

    # Remove the worktree (gtr first, then raw git as fallback).
    try:
        await _arun(["git", "gtr", "rm", ref.issue_identifier, "--force"],
                    cwd=ref.main_repo_path)
    except subprocess.CalledProcessError:
        log.warning("gtr rm failed, falling back to git worktree remove")
        try:
            await _arun(["git", "worktree", "remove", "--force", str(ref.worktree_path)],
                        cwd=ref.main_repo_path)
        except subprocess.CalledProcessError:
            log.exception("worktree remove also failed — rm -rf as last resort")
            if ref.worktree_path.exists():
                shutil.rmtree(ref.worktree_path, ignore_errors=True)
            await _arun(["git", "worktree", "prune"], cwd=ref.main_repo_path)

    # If not merging, also delete the branch locally so the worktree slot is free.
    if not merge:
        try:
            await _arun(["git", "branch", "-D", ref.branch_name], cwd=ref.main_repo_path)
        except subprocess.CalledProcessError:
            pass


async def list_active() -> list[dict]:
    """List active worktrees across all known main repos for debugging."""
    out: list[dict] = []
    if not REPOS_ROOT.exists():
        return out
    for safe in REPOS_ROOT.iterdir():
        main = safe / "main"
        if not (main / ".git").exists():
            continue
        try:
            p = await _arun(["git", "worktree", "list", "--porcelain"], cwd=main)
        except subprocess.CalledProcessError:
            continue
        entry: dict = {}
        for line in p.stdout.split("\n"):
            if line.startswith("worktree "):
                if entry:
                    out.append(entry); entry = {}
                entry["path"] = line[len("worktree "):]
            elif line.startswith("branch "):
                entry["branch"] = line[len("branch "):]
        if entry:
            out.append(entry)
    return out
