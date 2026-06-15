"""Junior QA stage: detect project test framework, run it, return a verdict.

Drop-in module for the triage orchestrator. Called after a dev agent has produced
a commit on a feature branch inside a per-issue git worktree.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # Imported lazily — the orchestrator owns this type. We only need .worktree_path.
    from typing import Protocol

    class WorktreeRef(Protocol):
        worktree_path: str
        branch_name: str


LOG_ROOT = Path("/var/lib/triage/junior-qa-runs")


@dataclass
class QaVerdict:
    passed: bool
    summary: str           # short markdown — what was run, top-level pass/fail counts
    feedback: str          # long-form — failures with file:line and the failing assertion
    test_runner: str       # 'vitest' / 'pytest' / 'cargo' / 'go' / 'none-found' / 'unknown'
    duration_s: float
    log_path: str          # absolute path to the captured run log


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _has_python_tests(root: Path) -> bool:
    # Cheap walk — bail after first hit. Avoid deep node_modules/.venv/target dirs.
    skip = {"node_modules", ".venv", "venv", ".git", "target", "dist", "build", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for f in filenames:
            if f.startswith("test_") and f.endswith(".py"):
                return True
            if f.endswith("_test.py"):
                return True
    return False


def _detect_runner(root: Path) -> tuple[str, Optional[list[str]]]:
    """Return (runner_name, command_argv) or (runner_name, None) if unrunnable."""
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(_read_text(pkg) or "{}")
        except json.JSONDecodeError:
            data = {}
        scripts = (data.get("scripts") or {})
        if "test" in scripts:
            test_script = (scripts.get("test") or "").lower()
            # Try to identify the underlying runner for the verdict label.
            if "vitest" in test_script:
                runner_label = "vitest"
            elif "jest" in test_script:
                runner_label = "jest"
            elif "mocha" in test_script:
                runner_label = "mocha"
            else:
                runner_label = "npm"
            # Prefer pnpm > bun > npm — but only if installed AND matches the lockfile.
            if (root / "pnpm-lock.yaml").exists() and _which("pnpm"):
                return runner_label, ["pnpm", "test"]
            if (root / "bun.lockb").exists() and _which("bun"):
                return runner_label, ["bun", "test"]
            if _which("pnpm"):
                return runner_label, ["pnpm", "test"]
            if _which("bun"):
                return runner_label, ["bun", "test"]
            if _which("npm"):
                return runner_label, ["npm", "test"]
            return runner_label, None

    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists() or (root / "setup.py").exists():
        if _has_python_tests(root):
            if _which("pytest"):
                return "pytest", ["pytest", "-q", "--maxfail=20"]
            if _which("python"):
                return "pytest", ["python", "-m", "pytest", "-q", "--maxfail=20"]
            if _which("python3"):
                return "pytest", ["python3", "-m", "pytest", "-q", "--maxfail=20"]
            return "pytest", None

    if (root / "Cargo.toml").exists():
        if _which("cargo"):
            return "cargo", ["cargo", "test", "--no-fail-fast"]
        return "cargo", None

    if (root / "go.mod").exists():
        if _which("go"):
            return "go", ["go", "test", "./..."]
        return "go", None

    return "none-found", None


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

async def _run_capture(
    argv: list[str],
    cwd: Path,
    log_path: Path,
    timeout_s: int,
) -> tuple[int, str, bool]:
    """Run argv, stream combined stdout+stderr to log_path, return (rc, captured, timed_out).

    Captures everything (we need it for parsing) AND tees it to disk.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "CI": "1", "FORCE_COLOR": "0", "NO_COLOR": "1"},
    )

    chunks: list[bytes] = []
    timed_out = False

    async def _pump() -> None:
        assert proc.stdout is not None
        with log_path.open("ab") as f:
            f.write(f"$ {' '.join(argv)}  (cwd={cwd})\n".encode())
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                f.write(line)
                f.flush()
                chunks.append(line)

    pump_task = asyncio.create_task(_pump())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
    finally:
        try:
            await asyncio.wait_for(pump_task, timeout=5)
        except asyncio.TimeoutError:
            pump_task.cancel()

    rc = proc.returncode if proc.returncode is not None else -1
    captured = b"".join(chunks).decode("utf-8", errors="replace")
    return rc, captured, timed_out


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_PYTEST_SUMMARY = re.compile(
    r"=+\s*"
    r"(?:(?P<failed>\d+)\s+failed[,\s]*)?"
    r"(?:(?P<passed>\d+)\s+passed[,\s]*)?"
    r"(?:(?P<skipped>\d+)\s+skipped[,\s]*)?"
    r"(?:(?P<errors>\d+)\s+error[s]?[,\s]*)?"
    r".*?in\s+[\d.]+s",
    re.IGNORECASE,
)

_PYTEST_FAIL_LINE = re.compile(r"^FAILED\s+(\S+::\S+)(?:\s+-\s+(.*))?$", re.MULTILINE)
_PYTEST_ASSERT = re.compile(r"^E\s+.*", re.MULTILINE)

_VITEST_SUMMARY = re.compile(
    r"Tests\s+(?:(?P<failed>\d+)\s+failed[\s|]*)?(?:(?P<passed>\d+)\s+passed[\s|]*)?(?:(?P<skipped>\d+)\s+skipped)?",
    re.IGNORECASE,
)

_GO_FAIL = re.compile(r"^---\s+FAIL:\s+(\S+)", re.MULTILINE)
_GO_PASS_FAIL_SUMMARY = re.compile(r"^(FAIL|PASS|ok)\s+\S+", re.MULTILINE)

_CARGO_SUMMARY = re.compile(
    r"test result:\s+(?P<verdict>\w+)\.\s+(?P<passed>\d+)\s+passed[;\s]+(?P<failed>\d+)\s+failed",
    re.IGNORECASE,
)


def _parse_pytest(out: str) -> tuple[Optional[dict], str]:
    counts: Optional[dict] = None
    # Last summary line wins.
    last = None
    for m in _PYTEST_SUMMARY.finditer(out):
        last = m
    if last:
        counts = {
            "passed": int(last.group("passed") or 0),
            "failed": int(last.group("failed") or 0),
            "skipped": int(last.group("skipped") or 0),
            "errors": int(last.group("errors") or 0),
        }
    fails: list[str] = []
    for fm in _PYTEST_FAIL_LINE.finditer(out):
        loc = fm.group(1)
        msg = (fm.group(2) or "").strip()
        fails.append(f"- `{loc}` — {msg}" if msg else f"- `{loc}`")
    # Add up to first 30 assertion lines for context.
    asserts = _PYTEST_ASSERT.findall(out)[:30]
    feedback_parts: list[str] = []
    if fails:
        feedback_parts.append("**Failed tests:**\n" + "\n".join(fails[:50]))
    if asserts:
        feedback_parts.append("**Assertion output (first 30 lines):**\n```\n" + "\n".join(asserts) + "\n```")
    return counts, "\n\n".join(feedback_parts)


def _parse_vitest(out: str) -> tuple[Optional[dict], str]:
    counts: Optional[dict] = None
    last = None
    for m in _VITEST_SUMMARY.finditer(out):
        last = m
    if last:
        counts = {
            "passed": int(last.group("passed") or 0),
            "failed": int(last.group("failed") or 0),
            "skipped": int(last.group("skipped") or 0),
        }
    # Pull lines that look like file:line failures.
    fails = re.findall(r"(?:FAIL|×)\s+(\S+\.(?:ts|tsx|js|jsx|mjs|cjs)[:\d]*)", out)
    feedback = ""
    if fails:
        feedback = "**Failing files:**\n" + "\n".join(f"- `{f}`" for f in list(dict.fromkeys(fails))[:50])
    return counts, feedback


def _parse_go(out: str) -> tuple[Optional[dict], str]:
    fails = _GO_FAIL.findall(out)
    counts = {"failed": len(fails), "passed": 0, "skipped": 0}
    # Rough pass count from package-level ok lines.
    counts["passed"] = len(re.findall(r"^ok\s+\S+", out, re.MULTILINE))
    feedback = ""
    if fails:
        feedback = "**Failed Go tests:**\n" + "\n".join(f"- `{t}`" for t in fails[:50])
    return counts, feedback


def _parse_cargo(out: str) -> tuple[Optional[dict], str]:
    last = None
    for m in _CARGO_SUMMARY.finditer(out):
        last = m
    counts: Optional[dict] = None
    if last:
        counts = {
            "passed": int(last.group("passed")),
            "failed": int(last.group("failed")),
        }
    fails = re.findall(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", out, re.MULTILINE)
    feedback = ""
    if fails:
        feedback = "**Failed Rust tests:**\n" + "\n".join(f"- `{t}`" for t in fails[:50])
    return counts, feedback


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_junior_qa(
    ref: "WorktreeRef",
    issue: dict,
    timeout_s: int = 900,
) -> QaVerdict:
    started = time.monotonic()
    root = Path(ref.worktree_path)
    identifier = str(issue.get("identifier") or "unknown")

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_ROOT / identifier / f"{ts}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    runner_label, argv = _detect_runner(root)

    # No tests / nothing runnable.
    if runner_label == "none-found":
        log_path.write_text(f"No test framework detected at {root}\n", encoding="utf-8")
        return QaVerdict(
            passed=True,
            summary="No test framework detected — skipping.",
            feedback="",
            test_runner="none-found",
            duration_s=time.monotonic() - started,
            log_path=str(log_path),
        )

    if argv is None:
        msg = f"Detected {runner_label} project but no runner binary on PATH."
        log_path.write_text(msg + "\n", encoding="utf-8")
        return QaVerdict(
            passed=False,
            summary=f"Cannot execute {runner_label}: runner not installed.",
            feedback=msg,
            test_runner=runner_label,
            duration_s=time.monotonic() - started,
            log_path=str(log_path),
        )

    rc, captured, timed_out = await _run_capture(argv, root, log_path, timeout_s)
    duration = time.monotonic() - started

    if timed_out:
        return QaVerdict(
            passed=False,
            summary=f"`{' '.join(argv)}` timed out after {timeout_s}s.",
            feedback=(
                f"Test run exceeded the {timeout_s}s budget and was killed.\n\n"
                f"Last 50 lines of output:\n```\n"
                + "\n".join(captured.splitlines()[-50:])
                + "\n```"
            ),
            test_runner=runner_label,
            duration_s=duration,
            log_path=str(log_path),
        )

    # Parse per-runner.
    counts: Optional[dict] = None
    feedback = ""
    if runner_label == "pytest":
        counts, feedback = _parse_pytest(captured)
    elif runner_label in {"vitest", "jest", "mocha", "npm"}:
        counts, feedback = _parse_vitest(captured)
    elif runner_label == "go":
        counts, feedback = _parse_go(captured)
    elif runner_label == "cargo":
        counts, feedback = _parse_cargo(captured)

    passed = rc == 0

    # Build summary line.
    if counts:
        pieces = []
        for k in ("passed", "failed", "skipped", "errors"):
            if k in counts and counts.get(k) is not None:
                pieces.append(f"{counts[k]} {k}")
        cnt_str = ", ".join(pieces) if pieces else "no counts parsed"
        summary = f"**{runner_label}** rc={rc} — {cnt_str} in {duration:.1f}s"
    else:
        summary = f"**{runner_label}** rc={rc} — exit code only (no counts parsed) in {duration:.1f}s"

    if not passed and not feedback:
        # No structured failures parsed — give the dev agent the tail of the log.
        tail = "\n".join(captured.splitlines()[-80:])
        feedback = f"Tests failed (exit code {rc}). Last 80 lines:\n```\n{tail}\n```"

    return QaVerdict(
        passed=passed,
        summary=summary,
        feedback=feedback,
        test_runner=runner_label,
        duration_s=duration,
        log_path=str(log_path),
    )
