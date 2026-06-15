"""dev_deploy.py — ephemeral per-issue container deploys for senior QA.

Spins up the dev branch (a `WorktreeRef.worktree_path` checkout) as a docker
compose stack joined to the shared `agent-deploys` network, registers a Traefik
file-provider route at `<identifier>.local-pcci.org`, and waits for health.

Tear-down removes the stack and the Traefik label file. A background reaper
loop kills deploys older than `ttl_seconds`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)

# ----- constants ---------------------------------------------------------
AGENT_NETWORK = "agent-deploys"
TRAEFIK_DYNAMIC = Path("/etc/traefik/dynamic")
DEPLOY_ROOT = Path("/var/lib/triage/dev-deploys")
DOMAIN = os.environ.get("TRAEFIK_DOMAIN", "local-pcci.org")
HEALTH_PATHS = ("/health", "/healthz", "/")
HEALTH_TIMEOUT_S = 90


# ----- public dataclass --------------------------------------------------
@dataclass
class DevDeploy:
    url: str                # https://<id>.local-pcci.org/
    container_prefix: str   # dev-<id-lower>
    compose_path: str       # absolute path to (possibly synthesised) compose file
    logs_path: str          # /var/lib/triage/dev-deploys/<id>/.log
    healthy: bool
    identifier: str = ""    # raw identifier (case preserved)
    port: int = 3000        # container port Traefik routes to
    created_at: float = field(default_factory=time.time)


# ----- helpers -----------------------------------------------------------
def _identifier(issue: dict) -> str:
    """Stable per-issue identifier. issue['identifier'] is e.g. 'ENG-42'."""
    raw = (
        issue.get("identifier")
        or issue.get("key")
        or issue.get("number")
        or issue.get("id")
        or "unknown"
    )
    return str(raw).replace("-", "").replace("_", "").lower()


async def _run(*cmd: str, cwd: Optional[Path] = None, log_to: Optional[Path] = None) -> tuple[int, str, str]:
    """Run a subprocess, optionally appending its output to `log_to`."""
    log.debug("exec: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", "replace")
    err = stderr.decode("utf-8", "replace")
    if log_to:
        log_to.parent.mkdir(parents=True, exist_ok=True)
        with log_to.open("a") as fh:
            fh.write(f"$ {' '.join(cmd)}\n{out}{err}\n")
    return proc.returncode or 0, out, err


# ----- compose discovery / synthesis -------------------------------------
def _detect_compose(worktree: Path) -> Optional[Path]:
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        p = worktree / name
        if p.exists():
            return p
    return None


def _detect_project_type(worktree: Path) -> str:
    if (worktree / "Dockerfile").exists():
        return "dockerfile"
    if (worktree / "package.json").exists():
        return "node"
    if (worktree / "pyproject.toml").exists():
        return "python"
    return "unknown"


def _synthesise_compose(worktree: Path, identifier: str, port: int) -> Path:
    """Write a minimal docker-compose.synth.yml at the worktree root.

    Heuristic — adequate for typical dev branches but absolutely not airtight.
    The README documents what we will fall back to per project type.
    """
    ptype = _detect_project_type(worktree)
    service_name = f"app"
    common = {
        "container_name": f"dev-{identifier}-app",
        "working_dir": "/app",
        "volumes": [f"{worktree}:/app"],
        "networks": [AGENT_NETWORK],
        "expose": [str(port)],
        "labels": [
            "triage.synthesised=true",
            f"triage.identifier={identifier}",
        ],
        "environment": {
            "PORT": str(port),
            "NODE_ENV": "development",
        },
    }

    if ptype == "dockerfile":
        service = {**common, "build": {"context": "."}}
    elif ptype == "node":
        service = {
            **common,
            "image": "node:22-bookworm-slim",
            "command": [
                "bash",
                "-lc",
                "corepack enable && (pnpm install || npm install) && "
                "(pnpm dev || npm run dev)",
            ],
        }
    elif ptype == "python":
        service = {
            **common,
            "image": "python:3.12-slim",
            "command": [
                "bash",
                "-lc",
                "pip install uv && uv pip install --system -e . && "
                f"uvicorn app:app --host 0.0.0.0 --port {port}",
            ],
        }
    else:
        raise RuntimeError(
            f"cannot synthesise compose: no Dockerfile / package.json / pyproject.toml in {worktree}"
        )

    compose = {
        "services": {service_name: service},
        "networks": {AGENT_NETWORK: {"external": True}},
    }
    out = worktree / "docker-compose.synth.yml"
    out.write_text(yaml.safe_dump(compose, sort_keys=False))
    return out


# ----- traefik dynamic file ---------------------------------------------
def _write_traefik_route(identifier: str, container: str, port: int) -> Path:
    """Drop a Traefik file-provider YAML routing <id>.local-pcci.org -> container."""
    TRAEFIK_DYNAMIC.mkdir(parents=True, exist_ok=True)
    router_name = f"dev-{identifier}"
    cfg = {
        "http": {
            "routers": {
                router_name: {
                    "rule": f"Host(`{identifier}.{DOMAIN}`)",
                    "service": router_name,
                    "entryPoints": ["web"],
                }
            },
            "services": {
                router_name: {
                    "loadBalancer": {
                        "servers": [{"url": f"http://{container}:{port}"}],
                    }
                }
            },
        }
    }
    path = TRAEFIK_DYNAMIC / f"{identifier}.yml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def _remove_traefik_route(identifier: str) -> None:
    path = TRAEFIK_DYNAMIC / f"{identifier}.yml"
    if path.exists():
        path.unlink()


# ----- health-poll -------------------------------------------------------
async def _wait_healthy(url: str, log_file: Path, timeout: int = HEALTH_TIMEOUT_S) -> bool:
    import urllib.request
    import urllib.error

    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        for path in HEALTH_PATHS:
            try:
                req = urllib.request.Request(url.rstrip("/") + path, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:  # nosec - internal
                    if 200 <= resp.status < 300:
                        with log_file.open("a") as fh:
                            fh.write(f"[health] OK {path} -> {resp.status}\n")
                        return True
            except Exception as exc:  # noqa: BLE001 - want broad
                last_err = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(2)
    with log_file.open("a") as fh:
        fh.write(f"[health] timed out after {timeout}s, last error: {last_err}\n")
    return False


# ----- public API --------------------------------------------------------
async def spin_up(
    ref: "WorktreeRef",  # noqa: F821 - typed by caller
    issue: dict,
    *,
    port_hint: int = 3000,
    ttl_seconds: int = 4 * 3600,
) -> DevDeploy:
    identifier = _identifier(issue)
    worktree = Path(ref.worktree_path)  # type: ignore[attr-defined]
    deploy_dir = DEPLOY_ROOT / identifier
    deploy_dir.mkdir(parents=True, exist_ok=True)
    logs_path = deploy_dir / ".log"
    logs_path.touch()

    # 1. compose file: detect or synthesise.
    existing = _detect_compose(worktree)
    if existing:
        compose_path = existing
        synthesised = False
    else:
        compose_path = _synthesise_compose(worktree, identifier, port_hint)
        synthesised = True

    # 2. docker compose up.
    project = f"dev-{identifier}"
    container_name = f"dev-{identifier}-app"  # matches _synthesise_compose
    rc, out, err = await _run(
        "docker", "compose",
        "-p", project,
        "-f", str(compose_path),
        "up", "-d", "--build",
        cwd=worktree,
        log_to=logs_path,
    )
    if rc != 0:
        raise RuntimeError(
            f"docker compose up failed for {identifier}: {err.strip() or out.strip()}"
        )

    # 3. ensure container is on the shared network (detected compose may not be).
    if not synthesised:
        # best-effort attach
        rc2, _, _ = await _run(
            "docker", "network", "connect", AGENT_NETWORK, container_name,
            log_to=logs_path,
        )
        # ignore failure: container may already be attached or compose used a different service name

    # 4. write traefik route.
    _write_traefik_route(identifier, container_name, port_hint)

    # 5. wait for health on the public URL.
    public_url = f"https://{identifier}.{DOMAIN}/"
    healthy = await _wait_healthy(public_url, logs_path)

    # Persist a manifest for the reaper.
    (deploy_dir / "manifest.json").write_text(json.dumps({
        "identifier": identifier,
        "project": project,
        "compose_path": str(compose_path),
        "container": container_name,
        "port": port_hint,
        "url": public_url,
        "created_at": time.time(),
        "ttl_seconds": ttl_seconds,
        "synthesised": synthesised,
    }, indent=2))

    return DevDeploy(
        url=public_url,
        container_prefix=project,
        compose_path=str(compose_path),
        logs_path=str(logs_path),
        healthy=healthy,
        identifier=identifier,
        port=port_hint,
    )


async def tear_down(deploy: DevDeploy) -> None:
    identifier = deploy.identifier or deploy.container_prefix.removeprefix("dev-")
    deploy_dir = DEPLOY_ROOT / identifier
    logs_path = Path(deploy.logs_path) if deploy.logs_path else (deploy_dir / ".log")

    # remove traefik route first so it stops serving 502s while the stack drains
    _remove_traefik_route(identifier)

    if Path(deploy.compose_path).exists():
        await _run(
            "docker", "compose",
            "-p", deploy.container_prefix,
            "-f", deploy.compose_path,
            "down", "-v", "--remove-orphans",
            log_to=logs_path,
        )
    else:
        # fallback: kill by project label
        await _run(
            "docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={deploy.container_prefix}",
            log_to=logs_path,
        )
        await _run("bash", "-lc",
                   f"docker rm -f $(docker ps -aq --filter label=com.docker.compose.project={deploy.container_prefix}) || true",
                   log_to=logs_path)

    # remove synthesised compose, keep logs for postmortem
    synth = Path(deploy.compose_path)
    if synth.name == "docker-compose.synth.yml" and synth.exists():
        try:
            synth.unlink()
        except OSError:
            pass


async def reaper_loop(interval_s: int = 600) -> None:
    """Background task: tear down deploys past their TTL. Idempotent on errors."""
    while True:
        try:
            now = time.time()
            if DEPLOY_ROOT.exists():
                for manifest in DEPLOY_ROOT.glob("*/manifest.json"):
                    try:
                        m = json.loads(manifest.read_text())
                    except Exception:
                        continue
                    age = now - m.get("created_at", now)
                    if age < m.get("ttl_seconds", 4 * 3600):
                        continue
                    log.info("reaper: tearing down %s (age=%ds)", m["identifier"], age)
                    fake = DevDeploy(
                        url=m["url"],
                        container_prefix=m["project"],
                        compose_path=m["compose_path"],
                        logs_path=str(manifest.parent / ".log"),
                        healthy=False,
                        identifier=m["identifier"],
                        port=m.get("port", 3000),
                    )
                    try:
                        await tear_down(fake)
                    except Exception:
                        log.exception("reaper: tear_down failed for %s", m["identifier"])
                    # keep manifest+logs under a .reaped/ archive for triage history
                    archive = DEPLOY_ROOT / ".reaped" / m["identifier"]
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(str(manifest.parent), str(archive))
                    except Exception:
                        pass
        except Exception:
            log.exception("reaper_loop iteration crashed")
        await asyncio.sleep(interval_s)
