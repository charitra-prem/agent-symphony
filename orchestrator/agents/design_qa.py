"""Design QA stage — compares a live deploy against the canonical Penpot design.

Tolerance philosophy ("smart native deviations")
-------------------------------------------------
A design source-of-truth pixel-matched against a live web UI is a losing game.
Real frontends legitimately deviate:
  - System font fallbacks render at slightly different metrics
  - Antialiasing shifts perceived colors by a couple of ΔE units
  - Native form controls (buttons, inputs) have OS-specific paddings
  - Responsive grids round half-pixel gutters

We therefore allow per-axis tolerances:
  - color:    ΔE(CIE76) <= 5      (env DESIGN_QA_COLOR_TOLERANCE)
  - font-px:  |Δ| <= 2 px           (env DESIGN_QA_FONT_PX)
  - spacing:  |Δ| <= 4 px OR 12.5% (env DESIGN_QA_SPACING_PX)

Anything inside tolerance is "no drift". Anything outside is a DriftItem,
graded by relative magnitude:
  - minor:    delta is < 10% over the tolerance
  - moderate: 10% .. 50%
  - major:    > 50%

`drift_pct` measures the fraction of *compared* tokens that ended up as
DriftItems (any severity). It is not weighted by severity; a single
'minor' drift counts the same as a 'major' one. The release-gate is
`drift_pct > drift_threshold_pct` (default 15%).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .penpot_client import DesignTokens, PenpotClient

log = logging.getLogger(__name__)

STATE_DB = Path("/var/lib/triage/state.db")
VISUAL_QA_BIN = "/usr/local/bin/visual-qa"
DESIGN_PARITY_SPEC = "/etc/visual-qa/task_specs/design_parity.yaml"

PENPOT_VIEW_URL_RE = re.compile(
    r"penpot\.local-pcci\.org/#/view/([0-9a-f-]{8,})", re.IGNORECASE
)
PENPOT_LABEL_RE = re.compile(r"^penpot:([0-9a-f-]{8,})$", re.IGNORECASE)


# --------------------------------------------------------------------- types

@dataclass
class DriftItem:
    token: str
    design_value: str
    live_value: str
    severity: str  # 'minor' | 'moderate' | 'major'


@dataclass
class DesignVerdict:
    passed: bool
    drift_pct: float
    drifts: list[DriftItem] = field(default_factory=list)
    summary: str = ""
    screenshots: list[str] = field(default_factory=list)
    feedback: str = ""


# ----------------------------------------------------------------- tolerances

def _tolerances() -> dict[str, float]:
    return {
        "color_de": float(os.environ.get("DESIGN_QA_COLOR_TOLERANCE", 5.0)),
        "font_px": float(os.environ.get("DESIGN_QA_FONT_PX", 2.0)),
        "spacing_px": float(os.environ.get("DESIGN_QA_SPACING_PX", 4.0)),
        "spacing_pct": 0.125,
    }


# ----------------------------------------------------------- file-id resolution

def _resolve_penpot_file_id(issue: dict) -> str | None:
    """Resolve Penpot file id from (in order):
        1. URL in issue description
        2. Linear label `penpot:<file-id>`
        3. project_penpot_default mapping in state.db
    """
    desc = issue.get("description") or ""
    m = PENPOT_VIEW_URL_RE.search(desc)
    if m:
        return m.group(1)

    for label in issue.get("labels") or []:
        label_name = label.get("name") if isinstance(label, dict) else str(label)
        if not label_name:
            continue
        m = PENPOT_LABEL_RE.match(label_name)
        if m:
            return m.group(1)

    project_id = (
        (issue.get("project") or {}).get("id")
        if isinstance(issue.get("project"), dict)
        else issue.get("project_id")
    )
    if project_id:
        return _lookup_project_default(project_id)
    return None


def _lookup_project_default(project_id: str) -> str | None:
    try:
        STATE_DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(STATE_DB) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_penpot_default (
                    project_id   TEXT PRIMARY KEY,
                    penpot_file_id TEXT NOT NULL,
                    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            row = conn.execute(
                "SELECT penpot_file_id FROM project_penpot_default WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return row[0] if row else None
    except sqlite3.Error as e:
        log.warning("state.db lookup failed: %s", e)
        return None


def set_project_default(project_id: str, penpot_file_id: str) -> None:
    """Public helper for the admin CLI."""
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(STATE_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_penpot_default (
                project_id   TEXT PRIMARY KEY,
                penpot_file_id TEXT NOT NULL,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO project_penpot_default (project_id, penpot_file_id)
            VALUES (?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                penpot_file_id = excluded.penpot_file_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (project_id, penpot_file_id),
        )


# --------------------------------------------------------------- visual-qa hop

async def _run_visual_qa(deploy_url: str) -> dict:
    """Invoke `visual-qa` with the design_parity spec.

    Returns the JSON the CLI emits on stdout. Shape (per spec):
        {
            "screenshots": ["/tmp/...png", ...],
            "computed_styles": {
                "heading": {"color": "rgb(...)", "font-size": "24px", ...},
                "nav":     {...},
                "button":  {...},
                "body":    {...},
                "footer":  {...},
            }
        }
    """
    proc = await asyncio.create_subprocess_exec(
        VISUAL_QA_BIN,
        "--spec", DESIGN_PARITY_SPEC,
        "--url", deploy_url,
        "--format", "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("visual-qa failed (%s): %s", proc.returncode, stderr.decode()[:400])
        return {"screenshots": [], "computed_styles": {}}
    try:
        return json.loads(stdout.decode())
    except ValueError:
        log.warning("visual-qa produced non-JSON output")
        return {"screenshots": [], "computed_styles": {}}


# --------------------------------------------------------------------- diffing

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_RE = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)"
)


def _parse_color(value: str) -> tuple[int, int, int] | None:
    if not value:
        return None
    value = value.strip()
    m = _HEX_RE.match(value)
    if m:
        hx = m.group(1)
        if len(hx) == 3:
            hx = "".join(c * 2 for c in hx)
        return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))
    m = _RGB_RE.match(value)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _delta_e_cie76(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    """Crude but sufficient — convert sRGB -> Lab via D65 and Euclidean distance."""
    def _to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
        def _linear(c: float) -> float:
            c /= 255.0
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = (_linear(x) for x in rgb)
        x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
        y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.00000
        z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

        def _f(t: float) -> float:
            return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

        fx, fy, fz = _f(x), _f(y), _f(z)
        return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))

    L1, a1, b1 = _to_lab(c1)
    L2, a2, b2 = _to_lab(c2)
    return math.sqrt((L1 - L2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)


_PX_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*px")


def _parse_px(value: str) -> float | None:
    if not value:
        return None
    m = _PX_RE.search(value)
    return float(m.group(1)) if m else None


def _grade(delta: float, tolerance: float) -> str:
    """Severity grade based on how far past tolerance the delta sits."""
    if tolerance <= 0:
        over = delta
    else:
        over = (delta - tolerance) / tolerance
    if over < 0.10:
        return "minor"
    if over < 0.50:
        return "moderate"
    return "major"


# ---------------------------------------------- token<->selector reconciliation

# What we expect each visual-qa selector to map to in design tokens.
SELECTOR_TOKEN_MAP: dict[str, dict[str, list[str]]] = {
    "heading": {
        "color":     ["heading", "foreground", "text", "title"],
        "font-size": ["heading", "h1", "title", "font-size-heading"],
    },
    "nav": {
        "color":            ["nav", "foreground", "text"],
        "background-color": ["nav", "background", "surface"],
    },
    "button": {
        "background-color": ["button", "primary", "accent", "brand"],
        "color":            ["button-foreground", "primary-foreground", "on-primary"],
        "border-radius":    ["button", "radius", "rounded"],
    },
    "body": {
        "color":            ["body", "foreground", "text"],
        "background-color": ["background", "surface", "body"],
        "font-size":        ["body", "base", "font-size-body"],
    },
    "footer": {
        "color":            ["footer", "muted", "subtle"],
        "background-color": ["footer", "background", "surface"],
    },
}


def _pick_design_value(
    tokens: DesignTokens,
    bucket: str,
    keywords: list[str],
) -> tuple[str, str] | None:
    """Return (token_name, value) where token_name fuzzy-matches any keyword."""
    pool = {
        "color": tokens.colors,
        "background-color": tokens.colors,
        "font-size": tokens.fonts,
        "border-radius": tokens.radii,
    }.get(bucket, {})
    if not pool:
        return None
    lowered = {k.lower(): (k, v) for k, v in pool.items()}
    for kw in keywords:
        kw = kw.lower()
        for k_lower, (k, v) in lowered.items():
            if kw in k_lower:
                return (k, v)
    return None


def _diff_tokens(
    tokens: DesignTokens,
    computed_styles: dict[str, dict[str, str]],
    tol: dict[str, float],
) -> tuple[list[DriftItem], int]:
    """Return (drifts, compared_count)."""
    drifts: list[DriftItem] = []
    compared = 0

    for selector, properties in SELECTOR_TOKEN_MAP.items():
        live_props = computed_styles.get(selector) or {}
        for css_prop, keywords in properties.items():
            picked = _pick_design_value(tokens, css_prop, keywords)
            live_value = live_props.get(css_prop)
            if not picked or not live_value:
                continue
            token_name, design_value = picked
            compared += 1
            full_token = f"{selector}.{css_prop}"

            drift = _compare_property(
                full_token, css_prop, design_value, live_value, tol
            )
            if drift:
                drifts.append(drift)

    return drifts, compared


def _compare_property(
    full_token: str,
    css_prop: str,
    design_value: str,
    live_value: str,
    tol: dict[str, float],
) -> DriftItem | None:
    if "color" in css_prop:
        d = _parse_color(design_value)
        l = _parse_color(live_value)
        if d is None or l is None:
            return None  # Unparseable — skip rather than scream.
        de = _delta_e_cie76(d, l)
        if de <= tol["color_de"]:
            return None
        return DriftItem(
            token=full_token,
            design_value=design_value,
            live_value=live_value,
            severity=_grade(de, tol["color_de"]),
        )

    if css_prop == "font-size":
        d = _parse_px(design_value)
        l = _parse_px(live_value)
        if d is None or l is None:
            return None
        delta = abs(d - l)
        if delta <= tol["font_px"]:
            return None
        return DriftItem(
            token=full_token,
            design_value=design_value,
            live_value=live_value,
            severity=_grade(delta, tol["font_px"]),
        )

    if css_prop in ("border-radius", "padding", "margin", "gap"):
        d = _parse_px(design_value)
        l = _parse_px(live_value)
        if d is None or l is None:
            return None
        delta = abs(d - l)
        pct_tol = max(d, l) * tol["spacing_pct"]
        allowed = max(tol["spacing_px"], pct_tol)
        if delta <= allowed:
            return None
        return DriftItem(
            token=full_token,
            design_value=design_value,
            live_value=live_value,
            severity=_grade(delta, allowed),
        )

    return None


# --------------------------------------------------------------- entry point

async def run_design_qa(
    issue: dict,
    deploy_url: str,
    penpot_file_id: str | None = None,
    *,
    drift_threshold_pct: float = 0.15,
) -> DesignVerdict:
    file_id = penpot_file_id or _resolve_penpot_file_id(issue)
    tol = _tolerances()

    # No design source available — skip rather than block release.
    if not file_id:
        return DesignVerdict(
            passed=True,
            drift_pct=0.0,
            summary=(
                "### Design QA skipped\n\n"
                "No Penpot file resolved for this issue.\n\n"
                "Add a `penpot:<file-id>` label, paste a "
                "`penpot.local-pcci.org/#/view/<id>` URL into the description, "
                "or set a project default via `set_project_default(project_id, file_id)`."
            ),
            feedback="Wire this issue's project to a Penpot file to enable design parity checks.",
        )

    client = PenpotClient(api_key=os.environ.get("PENPOT_API_KEY"))
    tokens = await client.get_file_tokens(file_id)

    if tokens is None or tokens.is_empty():
        return DesignVerdict(
            passed=True,
            drift_pct=0.0,
            summary=(
                "### Design QA skipped\n\n"
                "Penpot is unreachable or returned no tokens — not blocking the release.\n\n"
                f"Penpot view: {client.public_view_url(file_id)}"
            ),
            feedback=(
                "Set PENPOT_API_KEY (a Personal Access Token from Penpot account "
                "settings) so the design agent can fetch tokens."
            ),
        )

    # Run visual-qa to extract live computed styles.
    vqa = await _run_visual_qa(deploy_url)
    computed_styles = vqa.get("computed_styles") or {}
    screenshots = vqa.get("screenshots") or []

    if not computed_styles:
        return DesignVerdict(
            passed=True,
            drift_pct=0.0,
            screenshots=screenshots,
            summary=(
                "### Design QA inconclusive\n\n"
                "visual-qa produced no computed-style snapshot for the deploy. "
                "Treating as non-blocking."
            ),
            feedback="Check that visual-qa can reach the deploy URL.",
        )

    drifts, compared = _diff_tokens(tokens, computed_styles, tol)
    drift_pct = (len(drifts) / compared) if compared else 0.0
    passed = drift_pct <= drift_threshold_pct

    return DesignVerdict(
        passed=passed,
        drift_pct=drift_pct,
        drifts=drifts,
        screenshots=screenshots,
        summary=_render_summary(
            file_id, client.public_view_url(file_id),
            drifts, compared, drift_pct, drift_threshold_pct,
            screenshots, passed,
        ),
        feedback=_render_feedback(drifts),
    )


# ------------------------------------------------------------- markdown render

def _render_summary(
    file_id: str,
    view_url: str,
    drifts: list[DriftItem],
    compared: int,
    drift_pct: float,
    threshold: float,
    screenshots: list[str],
    passed: bool,
) -> str:
    status = "PASS" if passed else "FAIL"
    lines = [
        f"### Design QA — {status}",
        "",
        f"- Penpot file: [{file_id}]({view_url})",
        f"- Tokens compared: **{compared}**",
        f"- Drifts: **{len(drifts)}**",
        f"- Drift fraction: **{drift_pct:.1%}** (threshold {threshold:.0%})",
        "",
    ]
    if drifts:
        lines.append("| Token | Design | Live | Severity |")
        lines.append("| --- | --- | --- | --- |")
        for d in drifts[:25]:
            lines.append(
                f"| `{d.token}` | `{d.design_value}` | `{d.live_value}` | {d.severity} |"
            )
        if len(drifts) > 25:
            lines.append(f"| _... and {len(drifts) - 25} more_ | | | |")
        lines.append("")

    if screenshots:
        lines.append("#### Screenshots")
        for s in screenshots:
            lines.append(f"- ![]({s})")
    return "\n".join(lines)


def _render_feedback(drifts: list[DriftItem]) -> str:
    if not drifts:
        return "Live UI matches the canonical design within tolerance. Ship it."

    by_sev: dict[str, list[DriftItem]] = {"major": [], "moderate": [], "minor": []}
    for d in drifts:
        by_sev.setdefault(d.severity, []).append(d)

    parts: list[str] = []
    if by_sev["major"]:
        parts.append(
            "**Major drift — fix these first:**\n"
            + "\n".join(
                f"- `{d.token}`: design says `{d.design_value}`, live is "
                f"`{d.live_value}`. Update the corresponding CSS variable / "
                f"Tailwind theme token."
                for d in by_sev["major"][:8]
            )
        )
    if by_sev["moderate"]:
        parts.append(
            "**Moderate drift — worth fixing in this PR:**\n"
            + "\n".join(
                f"- `{d.token}`: {d.design_value} -> {d.live_value}"
                for d in by_sev["moderate"][:8]
            )
        )
    if by_sev["minor"]:
        parts.append(
            f"**Minor drift** ({len(by_sev['minor'])} items) — acceptable; "
            "tighten tolerances if you care."
        )
    return "\n\n".join(parts)
