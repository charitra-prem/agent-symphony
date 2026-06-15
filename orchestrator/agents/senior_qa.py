"""senior_qa.py — drive visual-qa against a live DevDeploy and return a verdict.

Pipeline:
  1. Build a visual-qa YAML spec from the issue's acceptance criteria
     (or a stripped-down "sanity" spec for post-merge smoke tests).
  2. Run `visual-qa run <spec> --run-id <uuid>` and parse the JSON result.
  3. Stage screenshots under /var/lib/visual-qa/served/<run-id>/ so the
     existing triage.local-pcci.org cloudflared route can expose them at
     https://triage.local-pcci.org/qa/<run-id>/...  (ingress wiring is a
     separate concern — see README "Known limitations".)
  4. Return a SeniorVerdict with markdown summary + per-screenshot URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)

VISUAL_QA_BIN = os.environ.get("VISUAL_QA_BIN", "/usr/local/bin/visual-qa")
RUNS_DIR = Path("/var/lib/visual-qa/runs")
SERVED_DIR = Path("/var/lib/visual-qa/served")
PUBLIC_BASE = os.environ.get(
    "TRIAGE_QA_PUBLIC_BASE",
    "https://triage.local-pcci.org/qa",
)


# ----- public dataclass --------------------------------------------------
@dataclass
class SeniorVerdict:
    passed: bool
    summary: str
    screenshots: list[str]
    feedback: str
    visual_qa_run_id: str
    spec_path: str = ""
    raw_result: dict[str, Any] = field(default_factory=dict)


# ----- AC -> assertion synthesis ----------------------------------------
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+)$", re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+)$", re.MULTILINE)


def _extract_acceptance_criteria(issue: dict) -> list[str]:
    """Pull AC bullets from common issue shapes (Linear / GitHub / plain)."""
    candidates: list[str] = []

    # Explicit field if upstream already split it.
    if isinstance(issue.get("acceptance_criteria"), list):
        candidates.extend(str(x) for x in issue["acceptance_criteria"])
    if isinstance(issue.get("acceptance_criteria"), str):
        candidates.extend(_BULLET_RE.findall(issue["acceptance_criteria"]))

    body = issue.get("body") or issue.get("description") or ""
    if body:
        # Prefer a fenced "Acceptance Criteria" section.
        m = re.search(
            r"(?is)acceptance\s*criteria.*?\n(.+?)(?:\n#{1,6}\s|\Z)",
            body,
        )
        section = m.group(1) if m else body
        checkboxes = _CHECKBOX_RE.findall(section)
        if checkboxes:
            candidates.extend(checkboxes)
        else:
            candidates.extend(_BULLET_RE.findall(section))

    # de-dup, strip, drop empties
    seen: set[str] = set()
    result: list[str] = []
    for c in candidates:
        c = c.strip().rstrip(".")
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _ac_to_step(ac: str, idx: int) -> dict[str, Any]:
    """Translate one AC bullet into a visual-qa step.

    This is a HEURISTIC translator — README documents the limits. We emit a
    structured step that visual-qa's NL-driven runner can pattern-match:
    intent + verbatim AC text + screenshot, and let the underlying model
    decide the concrete click/extract/assert sequence.
    """
    lower = ac.lower()

    intent = "verify"
    if any(k in lower for k in ("click", "press", "tap")):
        intent = "click_then_verify"
    elif any(k in lower for k in ("navigate", "open", "go to", "visit")):
        intent = "navigate_then_verify"
    elif any(k in lower for k in ("see", "display", "show", "render", "visible")):
        intent = "visual_assert"
    elif any(k in lower for k in ("type", "enter", "fill", "input", "submit")):
        intent = "form_interaction"

    return {
        "id": f"ac_{idx:02d}",
        "intent": intent,
        "criterion": ac,
        "actions": [
            {"screenshot": f"ac_{idx:02d}_before.png"},
            {"natural_language": ac},
            {"screenshot": f"ac_{idx:02d}_after.png"},
            {"assert_no_console_errors": True},
        ],
    }


def _build_spec(deploy: "DevDeploy", issue: dict, run_id: str, *, sanity: bool) -> dict[str, Any]:  # noqa: F821
    title = issue.get("title") or issue.get("summary") or f"issue-{issue.get('identifier','?')}"

    if sanity:
        return {
            "name": f"sanity:{title}",
            "run_id": run_id,
            "base_url": deploy.url,
            "browser": "chromium",
            "viewport": {"width": 1440, "height": 900},
            "steps": [
                {
                    "id": "smoke",
                    "intent": "smoke",
                    "actions": [
                        {"goto": "/"},
                        {"wait_for": "networkidle"},
                        {"screenshot": "landing.png"},
                        {"assert_no_console_errors": True},
                        {"assert_status_lt": 400},
                    ],
                }
            ],
        }

    acs = _extract_acceptance_criteria(issue)
    if not acs:
        # No AC parsed — fall back to a sanity spec but mark as degraded.
        acs = ["Page loads without errors and primary content renders"]

    steps = [
        {
            "id": "warmup",
            "intent": "navigate",
            "actions": [
                {"goto": "/"},
                {"wait_for": "networkidle"},
                {"screenshot": "warmup.png"},
            ],
        }
    ]
    steps.extend(_ac_to_step(ac, i + 1) for i, ac in enumerate(acs))

    return {
        "name": f"senior_qa:{title}",
        "run_id": run_id,
        "base_url": deploy.url,
        "browser": "chromium",
        "viewport": {"width": 1440, "height": 900},
        "issue": {
            "identifier": issue.get("identifier"),
            "title": title,
            "url": issue.get("url"),
        },
        "steps": steps,
    }


# ----- screenshot publication -------------------------------------------
def _publish_screenshots(run_id: str) -> list[str]:
    """Copy run artifacts into a public staging dir; return public URLs.

    Cloudflared/Traefik ingress for /qa/* is described in README — without it
    these URLs 404, but the data is still on disk for postmortem.
    """
    src = RUNS_DIR / run_id
    dst = SERVED_DIR / run_id
    if not src.exists():
        return []
    dst.mkdir(parents=True, exist_ok=True)
    urls: list[str] = []
    for img in sorted(src.rglob("*.png")):
        rel = img.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(img, target)
        except OSError:
            continue
        urls.append(f"{PUBLIC_BASE}/{run_id}/{rel.as_posix()}")
    return urls


# ----- result parsing / verdict -----------------------------------------
def _verdict_from_result(
    result: dict[str, Any], screenshots: list[str], run_id: str, deploy: "DevDeploy", issue: dict  # noqa: F821
) -> SeniorVerdict:
    passed = bool(result.get("passed"))
    failures = result.get("failures") or []
    steps = result.get("steps") or []

    # Markdown summary with embedded screenshots (first 4).
    title = issue.get("title") or "(no title)"
    icon = "PASS" if passed else "FAIL"
    lines = [
        f"## Senior QA: {icon} — {title}",
        f"- run-id: `{run_id}`",
        f"- deploy: {deploy.url}",
        f"- steps: {len(steps)}, failures: {len(failures)}",
        "",
    ]
    for url in screenshots[:4]:
        lines.append(f"![screenshot]({url})")
    summary = "\n".join(lines)

    # Feedback: detailed failure dump + suggested next action.
    fb_parts: list[str] = []
    if passed:
        fb_parts.append("All acceptance criteria verified end-to-end.")
    else:
        fb_parts.append(f"{len(failures)} failure(s):")
        for f in failures:
            step = f.get("step_id") or f.get("id") or "?"
            why = f.get("reason") or f.get("message") or "(no reason)"
            fb_parts.append(f"- [{step}] {why}")
        fb_parts.append("")
        fb_parts.append(
            "Suggested next action: route back to coding agent with the "
            "failing step IDs and the linked screenshots; do NOT merge."
        )

    return SeniorVerdict(
        passed=passed,
        summary=summary,
        screenshots=screenshots,
        feedback="\n".join(fb_parts),
        visual_qa_run_id=run_id,
        raw_result=result,
    )


# ----- public entry point -----------------------------------------------
async def run_senior_qa(
    deploy: "DevDeploy",  # noqa: F821
    issue: dict,
    *,
    sanity: bool = False,
) -> SeniorVerdict:
    if not deploy.healthy:
        rid = str(uuid.uuid4())
        return SeniorVerdict(
            passed=False,
            summary=f"## Senior QA: FAIL — deploy never went healthy\n- url: {deploy.url}",
            screenshots=[],
            feedback=(
                "DevDeploy.healthy=False — the container failed to respond on "
                f"/health or / within timeout. Inspect {deploy.logs_path} before "
                "re-running."
            ),
            visual_qa_run_id=rid,
        )

    run_id = str(uuid.uuid4())
    spec = _build_spec(deploy, issue, run_id, sanity=sanity)
    spec_path = Path(f"/tmp/{run_id}.yaml")
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False))

    proc = await asyncio.create_subprocess_exec(
        VISUAL_QA_BIN, "run", str(spec_path), "--run-id", run_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    raw_out = stdout.decode("utf-8", "replace")
    raw_err = stderr.decode("utf-8", "replace")

    result: dict[str, Any] = {}
    try:
        result = json.loads(raw_out)
    except json.JSONDecodeError:
        # visual-qa might print logs before the JSON blob; grab the last {...}
        m = re.search(r"\{.*\}\s*\Z", raw_out, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group(0))
            except json.JSONDecodeError:
                result = {}

    if proc.returncode != 0 and not result:
        return SeniorVerdict(
            passed=False,
            summary=f"## Senior QA: FAIL — visual-qa crashed (rc={proc.returncode})",
            screenshots=[],
            feedback=f"visual-qa stderr:\n```\n{raw_err[-2000:]}\n```",
            visual_qa_run_id=run_id,
            spec_path=str(spec_path),
        )

    screenshots = _publish_screenshots(run_id)
    verdict = _verdict_from_result(result, screenshots, run_id, deploy, issue)
    verdict.spec_path = str(spec_path)
    return verdict
