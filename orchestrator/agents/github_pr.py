"""
github_pr — thin async wrapper around the `gh` CLI for PR open/view/merge/comment.

`gh` is configured via the `GITHUB_TOKEN` environment variable (or `gh auth
login`); we do not pass tokens on the command line.  All calls are subprocess-
based: no extra HTTP client dependency.

State strings normalised to ``open`` / ``merged`` / ``closed`` for callers.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


@dataclass
class PullRequest:
    number: int
    url: str
    state: str           # 'open' | 'merged' | 'closed'
    head_sha: str


class GhError(RuntimeError):
    """Raised when `gh` exits non-zero in a way the caller should see."""

    def __init__(self, cmd: list[str], rc: int, stdout: str, stderr: str) -> None:
        self.cmd = cmd
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"gh failed (rc={rc}): {shlex.join(cmd)}\nstderr: {stderr.strip()}"
        )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def open_pr(
    repo: str,
    base: str,
    head: str,
    title: str,
    body: str,
) -> PullRequest:
    """Open a PR via ``gh pr create``.

    ``head`` can be a plain branch (assumed in ``repo``) or
    ``owner:branch`` for cross-fork PRs.  Returns a :class:`PullRequest`.
    Raises :class:`GhError` on failure.
    """
    cmd = [
        "gh", "pr", "create",
        "--repo", repo,
        "--base", base,
        "--head", head,
        "--title", title,
        "--body", body,
    ]
    rc, out, err = await _run(cmd)
    if rc != 0:
        raise GhError(cmd, rc, out, err)

    # `gh pr create` prints the PR URL on the last non-empty line.
    url = ""
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line.startswith("http"):
            url = line
            break
    if not url:
        raise GhError(cmd, rc, out, err or "gh pr create produced no URL")

    number = _pr_number_from_url(url)
    return await get_pr(repo, number)


async def get_pr(repo: str, number: int) -> PullRequest:
    """Fetch PR state via ``gh pr view --json``."""
    cmd = [
        "gh", "pr", "view", str(number),
        "--repo", repo,
        "--json", "number,url,state,headRefOid,mergedAt",
    ]
    rc, out, err = await _run(cmd)
    if rc != 0:
        raise GhError(cmd, rc, out, err)

    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhError(cmd, rc, out, f"json decode: {exc}") from exc

    return PullRequest(
        number=int(data["number"]),
        url=str(data["url"]),
        state=_normalise_state(data.get("state", ""), data.get("mergedAt")),
        head_sha=str(data.get("headRefOid") or ""),
    )


async def merge_pr(
    repo: str,
    number: int,
    method: str = "squash",
    admin: bool = False,
) -> bool:
    """Merge a PR.

    ``method`` ∈ {"squash", "merge", "rebase"}.  ``--auto`` lets GitHub merge
    once required checks pass; combined with ``admin=True`` we bypass any
    required-reviews protection.  Returns ``True`` if `gh` exited 0.
    """
    if method not in {"squash", "merge", "rebase"}:
        raise ValueError(f"invalid merge method: {method!r}")
    cmd = [
        "gh", "pr", "merge", str(number),
        "--repo", repo,
        f"--{method}",
        "--auto",
        "--delete-branch",
    ]
    if admin:
        cmd.append("--admin")
    rc, out, err = await _run(cmd)
    if rc != 0:
        # gh sometimes returns 1 if auto-merge is "already enabled" — treat
        # that as success since the desired state is reached.
        if "already" in (err.lower() + out.lower()):
            return True
        raise GhError(cmd, rc, out, err)
    return True


async def comment_on_pr(repo: str, number: int, body: str) -> None:
    cmd = [
        "gh", "pr", "comment", str(number),
        "--repo", repo,
        "--body", body,
    ]
    rc, out, err = await _run(cmd)
    if rc != 0:
        raise GhError(cmd, rc, out, err)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _normalise_state(state: str, merged_at: str | None) -> str:
    s = (state or "").upper()
    if s == "MERGED" or merged_at:
        return "merged"
    if s == "CLOSED":
        return "closed"
    if s == "OPEN":
        return "open"
    return state.lower() or "unknown"


def _pr_number_from_url(url: str) -> int:
    # URL form: https://github.com/<owner>/<repo>/pull/<n>
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(tail)
    except ValueError as exc:
        raise GhError(
            ["<parse>"], -1, url, f"could not parse PR number from {url!r}"
        ) from exc


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    out_b, err_b = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
    )
