"""Acceptance-criteria check via Kimi-K2 (`pi -p --provider kimi-coding --model k2p5`).

Asks the cheap fast model whether the current diff satisfies the issue's AC.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from typing import Protocol

    class WorktreeRef(Protocol):
        worktree_path: str
        branch_name: str


MAX_DIFF_BYTES = 200_000
PI_BIN = os.environ.get("PI_BIN", "pi")


@dataclass
class AcVerdict:
    passed: bool
    confidence: str   # 'high' | 'medium' | 'low'
    reasoning: str    # 2-4 sentences from Kimi
    raw_response: str # debug: raw model output


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

async def _git(cwd: Path, *args: str, timeout: int = 30) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return ""
    return out.decode("utf-8", errors="replace")


async def _detect_base(cwd: Path) -> str:
    """Best-effort base-branch detection. Tries origin/HEAD, then main/master."""
    out = await _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    out = out.strip()
    if out:
        # e.g. "origin/main" — keep as-is for diff target
        return out
    for cand in ("origin/main", "origin/master", "main", "master"):
        check = await _git(cwd, "rev-parse", "--verify", cand)
        if check.strip():
            return cand
    return "HEAD~1"


async def _diff_stat(cwd: Path, base: str) -> str:
    return (await _git(cwd, "diff", "--stat", f"{base}...HEAD")).strip()


async def _diff_body(cwd: Path, base: str) -> str:
    out = await _git(cwd, "diff", f"{base}...HEAD")
    if len(out.encode("utf-8")) <= MAX_DIFF_BYTES:
        return out
    # Truncate at the byte budget, slice back to a newline.
    truncated = out.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]
    return truncated + "\n\n[... diff truncated at ~200KB ...]\n"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_INSTR = (
    "You are a strict QA reviewer. Given an issue and the diff a developer produced, "
    "decide whether the diff satisfies the acceptance criteria. "
    "Respond ONLY with a single JSON object on one line, no prose, no code fences:\n"
    '{"passed": true|false, "confidence": "low"|"medium"|"high", "reasoning": "<2-4 sentences>"}\n'
    "Be strict: if the diff is unrelated, partial, or only touches docs/comments when "
    "code changes were required, return passed=false. If criteria are vague, judge by "
    "the spirit of the issue description."
)


def _build_prompt(issue: dict, ac_text: str, diff_stat: str, diff_body: str) -> str:
    title = (issue.get("title") or "").strip()
    description = (issue.get("description") or "").strip()
    identifier = (issue.get("identifier") or "").strip()
    return (
        f"{_SYSTEM_INSTR}\n\n"
        f"## Issue {identifier}: {title}\n\n"
        f"### Description\n{description or '(no description)'}\n\n"
        f"### Acceptance Criteria\n{ac_text}\n\n"
        f"### Diff Stat\n```\n{diff_stat or '(empty)'}\n```\n\n"
        f"### Diff\n```diff\n{diff_body or '(empty)'}\n```\n\n"
        "Now output the JSON verdict."
    )


# ---------------------------------------------------------------------------
# `pi` invocation
# ---------------------------------------------------------------------------

async def _call_pi(prompt: str, timeout_s: int) -> tuple[str, int]:
    """Invoke `pi -p --provider kimi-coding --model k2p5` with the prompt on stdin.

    Returns (stdout, returncode). `-p` is the print-and-exit / pipe mode.
    """
    argv = [PI_BIN, "-p", "--provider", "kimi-coding", "--model", "k2p5"]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        return "", -1
    rc = proc.returncode if proc.returncode is not None else -1
    return out.decode("utf-8", errors="replace"), rc


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def _extract_first_json(text: str) -> Optional[dict]:
    """Find and parse the first balanced {...} object in text."""
    if not text:
        return None
    # Fast path — whole thing is JSON.
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # Strip common fences.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Scan for the first balanced object, respecting string escapes.
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start = -1  # keep scanning
    return None


def _coerce_verdict(obj: dict, raw: str) -> AcVerdict:
    passed = bool(obj.get("passed", False))
    confidence = str(obj.get("confidence", "low")).lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    reasoning = str(obj.get("reasoning") or "").strip()
    if not reasoning:
        reasoning = "(model returned no reasoning)"
    return AcVerdict(
        passed=passed,
        confidence=confidence,
        reasoning=reasoning,
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def check_acceptance_criteria(
    ref: "WorktreeRef",
    issue: dict,
    timeout_s: int = 120,
) -> AcVerdict:
    root = Path(ref.worktree_path)

    ac_raw = issue.get("acceptance_criteria")
    if ac_raw and str(ac_raw).strip():
        ac_text = str(ac_raw).strip()
    else:
        ac_text = "Inferred from description."

    base = await _detect_base(root)
    diff_stat = await _diff_stat(root, base)
    diff_body = await _diff_body(root, base)

    prompt = _build_prompt(issue, ac_text, diff_stat, diff_body)

    raw, rc = await _call_pi(prompt, timeout_s)

    if rc != 0 and not raw.strip():
        return AcVerdict(
            passed=False,
            confidence="low",
            reasoning=f"`pi` invocation failed (rc={rc}) and produced no output.",
            raw_response=raw,
        )

    obj = _extract_first_json(raw)
    if obj is None:
        return AcVerdict(
            passed=False,
            confidence="low",
            reasoning=(
                "Could not parse a JSON verdict from the model output. "
                "Treating as fail so a senior reviewer can intervene."
            ),
            raw_response=raw,
        )

    return _coerce_verdict(obj, raw)
