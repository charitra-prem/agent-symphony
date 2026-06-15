"""Persistent Claude tmux session — drives a long-lived claude REPL via send-keys / paste-buffer.

Same pattern the primeline-ai orchestrator and omnigent's claude_native_bridge use.
One PM-claude session stays warm; triage prompts inject via paste-buffer.

Per-request nonce sentinel so the parser distinguishes "the instruction telling
Claude to emit the sentinel" from "Claude actually emitting it". Without this,
the pasted prompt text matches before Claude has responded.

Concurrency is serialised by a module-level asyncio.Lock — multiple webhook
events queue against the single warm pane.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
import uuid

log = logging.getLogger(__name__)

SESSION = "pm-claude"
PANE = f"{SESSION}:0.0"

# Idle marker = the empty input prompt.
PROMPT_REGEX = re.compile(r"❯|>\s*$")

# Spinner words Claude Code uses while reasoning. Without these, our stall
# detector fires during normal long thinks. Curated from observed pane output;
# new spinner words appear as Claude updates — keep this list permissive.
SPINNER_REGEX = re.compile(
    r"(Running|Thinking|Searching|Reading|Writing|Editing|"
    r"Crunching|Crunched|Crafting|Crafted|Cooking|Cooked|"
    r"Pondering|Pondered|Brewing|Brewed|Simmering|Simmered|"
    r"Considering|Considered|Analyzing|Analyzed|Synthesizing|Synthesized|"
    r"Planning|Planned|Plotting|Plotted|Deliberating|Deliberated|"
    r"Investigating|Investigated|Wrangling|Wrangled|Hmm|Sleuthing|Sleuthed)"
)

ANSI_REGEX = re.compile(
    r"\x1b\[[0-9;:?<=>]*[a-zA-Z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1bP[^\x1b]*(?:\x1b\\|$)"
    r"|\x1b[()][0-9A-Za-z]"
    r"|[\x0e\x0f]"
)

# Default — pm_prompt.md uses this literal placeholder; we substitute per-request.
DONE_PLACEHOLDER = "[[TRIAGE_DONE]]"

_inject_lock = asyncio.Lock()


def _strip_ansi(s: str) -> str:
    return ANSI_REGEX.sub("", s)


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def session_exists() -> bool:
    return _tmux("has-session", "-t", SESSION, check=False).returncode == 0


def capture(lines: int = 200) -> str:
    r = _tmux("capture-pane", "-t", PANE, "-p", "-S", f"-{lines}", check=False)
    return _strip_ansi(r.stdout)


def is_idle() -> bool:
    snap = capture(lines=20)
    if SPINNER_REGEX.search(snap):
        return False
    return bool(PROMPT_REGEX.search(snap))


def ensure_session(model: str = "sonnet") -> None:
    if session_exists():
        log.info("pm-claude tmux session already exists")
        return

    log.info("creating pm-claude tmux session with model=%s", model)
    cmd = f"IS_SANDBOX=1 claude --model {model} --dangerously-skip-permissions"
    _tmux(
        "new-session", "-d", "-s", SESSION, "-c", "/root",
        "-e", "IS_SANDBOX=1",
        "bash", "-lc", cmd,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(2)
        if is_idle():
            log.info("pm-claude reached idle prompt")
            return
    raise RuntimeError("pm-claude failed to reach idle within 60s")


def kill_session() -> None:
    _tmux("kill-session", "-t", SESSION, check=False)


async def ask(prompt: str, timeout_s: int = 240) -> str:
    """Inject prompt into pm-claude, return the text between Claude's response
    start and end markers.

    The caller's `prompt` may contain the literal placeholder
    ``[[TRIAGE_DONE]]`` (typical for system-prompt files that document the
    expected sentinel). We substitute a per-request nonce so the placeholder
    in the user's text doesn't confuse the parser.
    """
    nonce = uuid.uuid4().hex[:12]
    done = f"[[TRIAGE_DONE_{nonce}]]"
    # Substitute the placeholder, regardless of where it appears (including
    # inside the embedded `pm_prompt.md` instructions).
    sent_prompt = prompt.replace(DONE_PLACEHOLDER, done)

    async with _inject_lock:
        if not session_exists():
            ensure_session()

        # Wait until Claude is idle (no spinner, prompt visible).
        idle_deadline = time.time() + 30
        while time.time() < idle_deadline:
            if is_idle():
                break
            await asyncio.sleep(1)

        # Clear input line: Ctrl-A then Ctrl-K.
        _tmux("send-keys", "-t", PANE, "C-a", check=False)
        _tmux("send-keys", "-t", PANE, "C-k", check=False)

        # Paste prompt via load-buffer (preserves newlines).
        buf_name = f"pm-{int(time.time() * 1000)}"
        subprocess.run(
            ["tmux", "load-buffer", "-b", buf_name, "-"],
            input=sent_prompt, text=True, capture_output=True, check=True,
        )
        _tmux("paste-buffer", "-p", "-d", "-b", buf_name, "-t", PANE)
        await asyncio.sleep(0.3)
        _tmux("send-keys", "-t", PANE, "Enter")

        # Poll for the nonce sentinel + stability. Claude's TUI keeps a
        # completion banner ("✻ Cooked for 4s") AFTER it finishes, which
        # confuses any spinner-keyword detector. Instead we use sentinel-count
        # stability: once the count stops changing for 5s, Claude is done
        # writing — read everything before the LAST sentinel.
        STABILITY_S = 5
        deadline = time.time() + timeout_s
        last_count = -1
        last_change_t = time.time()
        while time.time() < deadline:
            await asyncio.sleep(2)
            snap = capture(lines=5000)
            cur = snap.count(done)
            if cur >= 1 and cur == last_count:
                if time.time() - last_change_t >= STABILITY_S:
                    last_idx = snap.rfind(done)
                    return snap[:last_idx].strip()
            else:
                last_count = cur
                last_change_t = time.time()

        raise TimeoutError(
            f"pm-claude did not stabilise {done} within {timeout_s}s "
            f"(last count={last_count})"
        )
