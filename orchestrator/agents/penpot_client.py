"""Penpot client for fetching design tokens.

Approach chosen: Direct Penpot RPC API (POST /api/rpc/command/get-file).
Rationale:
  - One fewer external dependency (no Node/npx required at runtime).
  - We already speak HTTP from the rest of the triage system.
  - DTCG export from Penpot's `get-file-export` RPC is stable since Penpot 2.x.

Fallback: If the RPC shape changes (Penpot has historically renamed commands),
the `_extract_tokens_via_export_cli` method shells out to the `penpot-export`
npm CLI as a backup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class DesignTokens:
    fonts: dict[str, str] = field(default_factory=dict)
    colors: dict[str, str] = field(default_factory=dict)
    spacing: dict[str, str] = field(default_factory=dict)
    radii: dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.fonts or self.colors or self.spacing or self.radii)


class PenpotClient:
    """Talks to a self-hosted Penpot instance via its RPC API.

    The Penpot REST endpoint shape is:
        POST <base>/api/rpc/command/<command-name>
        Headers: Authorization: Token <PAT>
        Body: JSON with command-specific params.

    We use:
        - get-file               -> raw file structure (fallback for token mining)
        - get-file-export        -> DTCG-format export when available
    """

    def __init__(
        self,
        base_url: str = "https://penpot.local-pcci.org",
        api_key: str | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("PENPOT_API_KEY")
        self.timeout = timeout

    # ------------------------------------------------------------------ public

    def public_view_url(self, file_id: str) -> str:
        """Tokenless viewer URL — safe to paste into a Linear comment."""
        return f"{self.base_url}/#/view/{file_id}"

    async def get_file_tokens(self, file_id: str) -> DesignTokens | None:
        """Fetch design tokens for a Penpot file. Returns None on failure.

        Failure modes (each logged, none raised):
          - PENPOT_API_KEY missing
          - Penpot host unreachable
          - 401/403 (bad PAT)
          - 404 (file id not visible to this PAT)
          - Unparseable response body
        """
        if not self.api_key:
            log.warning("Penpot not configured: PENPOT_API_KEY missing")
            return None

        # Try DTCG export first — that's the format we actually want.
        tokens = await self._try_dtcg_export(file_id)
        if tokens is not None:
            return tokens

        # Fallback: mine the raw file structure.
        raw = await self._rpc("get-file", {"id": file_id})
        if raw is None:
            return None
        return self._extract_tokens_from_raw_file(raw)

    # --------------------------------------------------------------- internals

    async def _rpc(self, command: str, params: dict[str, Any]) -> dict | None:
        url = f"{self.base_url}/api/rpc/command/{command}"
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=params)
        except httpx.RequestError as e:
            log.warning("Penpot %s unreachable: %s", url, e)
            return None

        if resp.status_code in (401, 403):
            log.warning("Penpot rejected PAT (%s) — check PENPOT_API_KEY scope", resp.status_code)
            return None
        if resp.status_code == 404:
            log.warning("Penpot file %s not found / not visible to this PAT", params.get("id"))
            return None
        if resp.status_code >= 400:
            log.warning("Penpot %s -> HTTP %s: %s", command, resp.status_code, resp.text[:200])
            return None

        try:
            return resp.json()
        except ValueError:
            log.warning("Penpot %s returned non-JSON body", command)
            return None

    async def _try_dtcg_export(self, file_id: str) -> DesignTokens | None:
        """Penpot 2.x exposes `get-file-export` returning a DTCG-shaped tree.

        Older instances may not have this command — we treat a 404 / 'unknown
        command' RPC error as "fall back to raw extraction".
        """
        body = await self._rpc(
            "get-file-export",
            {"file-id": file_id, "format": "dtcg"},
        )
        if not body:
            return None

        # Penpot sometimes wraps successful payloads under {"export": {...}}.
        dtcg = body.get("export") if isinstance(body, dict) else None
        dtcg = dtcg or body
        if not isinstance(dtcg, dict):
            return None

        return self._extract_tokens_from_dtcg(dtcg)

    @staticmethod
    def _extract_tokens_from_dtcg(dtcg: dict) -> DesignTokens:
        """DTCG groups tokens by `$type` — color, fontFamily, dimension, etc.

        We walk the tree, flatten dotted names ('button.primary.background'),
        and split into our 4 buckets + a 'raw' for debug.
        """
        tokens = DesignTokens(raw={"dtcg_keys": list(dtcg.keys())[:50]})

        def walk(node: dict, prefix: str = "") -> None:
            for key, value in node.items():
                if key.startswith("$"):
                    continue
                if not isinstance(value, dict):
                    continue
                name = f"{prefix}.{key}" if prefix else key
                token_type = value.get("$type")
                token_value = value.get("$value")

                if token_type and token_value is not None:
                    val_str = _stringify_value(token_value)
                    if token_type == "color":
                        tokens.colors[name] = val_str
                    elif token_type in ("fontFamily", "typography", "fontWeight", "fontSize"):
                        tokens.fonts[name] = val_str
                    elif token_type in ("dimension", "spacing"):
                        # Heuristic: 'radius' or 'rounded' in name -> radii.
                        if "radius" in name.lower() or "rounded" in name.lower():
                            tokens.radii[name] = val_str
                        else:
                            tokens.spacing[name] = val_str
                    elif token_type == "borderRadius":
                        tokens.radii[name] = val_str
                else:
                    walk(value, name)

        walk(dtcg)
        return tokens

    @staticmethod
    def _extract_tokens_from_raw_file(raw: dict) -> DesignTokens:
        """Mine the raw Penpot file structure for tokens.

        Penpot stores tokens under `data.tokens` (newer) or scattered across
        `colors`, `typographies` library shapes (older). Best-effort.
        """
        tokens = DesignTokens(raw={"raw_keys": list(raw.keys())[:50]})
        data = raw.get("data") or {}

        # Newer: `data.tokens` is a DTCG-ish blob.
        if isinstance(data.get("tokens"), dict):
            return PenpotClient._extract_tokens_from_dtcg(data["tokens"])

        # Older: library-shape colors / typographies.
        for cid, c in (data.get("colors") or {}).items():
            name = c.get("name", cid)
            value = c.get("color") or c.get("value")
            if value:
                tokens.colors[name] = value

        for tid, t in (data.get("typographies") or {}).items():
            name = t.get("name", tid)
            family = t.get("font-family") or t.get("fontFamily") or ""
            size = t.get("font-size") or t.get("fontSize") or ""
            weight = t.get("font-weight") or t.get("fontWeight") or ""
            tokens.fonts[name] = f"{size} {weight} {family}".strip()

        return tokens

    # ------------------------------------------------------ optional CLI path

    async def _extract_tokens_via_export_cli(
        self, file_id: str
    ) -> DesignTokens | None:
        """Backup: shell out to @penpot-export/cli.

        Only invoked if the caller explicitly asks for it; we don't auto-fall
        back to npx because cold-start adds ~8s.
        """
        if not self.api_key:
            return None
        if shutil.which("npx") is None:
            log.warning("penpot-export fallback unavailable: npx not on PATH")
            return None

        proc = await asyncio.create_subprocess_exec(
            "npx", "-p", "@penpot-export/cli", "penpot-export",
            "--file-id", file_id,
            "--token", self.api_key,
            "--format", "dtcg",
            "--output", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning("penpot-export CLI failed: %s", stderr.decode()[:200])
            return None
        try:
            dtcg = json.loads(stdout.decode())
        except ValueError:
            log.warning("penpot-export CLI returned non-JSON")
            return None
        return self._extract_tokens_from_dtcg(dtcg)


def _stringify_value(v: Any) -> str:
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, dict):
        # Composite tokens (typography) — flatten key=val pairs.
        return ", ".join(f"{k}={v}" for k, v in v.items())
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return json.dumps(v)
