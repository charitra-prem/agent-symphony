"""Resolve which GitHub repo a Linear issue targets.

Resolution order:
  1. Labels matching `repo:owner/name`
  2. github.com/owner/name URL in description
  3. project_repo_map table
  4. team_repo_default table
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from linear_client import LinearClient  # type: ignore[import-not-found]

DB_PATH = Path("/var/lib/triage/state.db")

# owner/name: GitHub allows alnum, hyphen, underscore, period; owner no leading hyphen.
_REPO_SLUG = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}"
_REPO_LABEL_RE = re.compile(rf"^repo:({_REPO_SLUG})$")
_GH_URL_RE = re.compile(
    rf"https?://(?:www\.)?github\.com/({_REPO_SLUG})(?=[/#?\s)]|$)",
    re.IGNORECASE,
)


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def provision_db(db_path: Path = DB_PATH) -> None:
    """Create mapping tables if missing. Idempotent."""
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS project_repo_map (
                project_id TEXT PRIMARY KEY,
                repo_full_name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS team_repo_default (
                team_key TEXT PRIMARY KEY,
                repo_full_name TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()


def set_project_repo(project_id: str, repo: str, db_path: Path = DB_PATH) -> None:
    if not _is_valid_repo(repo):
        raise ValueError(f"invalid repo slug: {repo!r}")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO project_repo_map (project_id, repo_full_name) VALUES (?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                repo_full_name = excluded.repo_full_name,
                updated_at = datetime('now')
            """,
            (project_id, repo),
        )
        conn.commit()


def set_team_default_repo(team_key: str, repo: str, db_path: Path = DB_PATH) -> None:
    if not _is_valid_repo(repo):
        raise ValueError(f"invalid repo slug: {repo!r}")
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO team_repo_default (team_key, repo_full_name) VALUES (?, ?)
            ON CONFLICT(team_key) DO UPDATE SET
                repo_full_name = excluded.repo_full_name,
                updated_at = datetime('now')
            """,
            (team_key, repo),
        )
        conn.commit()


def _is_valid_repo(s: str) -> bool:
    return re.fullmatch(_REPO_SLUG, s) is not None


def _from_labels(issue: dict) -> str | None:
    labels = issue.get("labels") or {}
    nodes = labels.get("nodes") if isinstance(labels, dict) else labels
    if not nodes:
        return None
    for node in nodes:
        name = (node.get("name") if isinstance(node, dict) else str(node)) or ""
        m = _REPO_LABEL_RE.match(name.strip())
        if m:
            return m.group(1)
    return None


def _from_description(issue: dict) -> str | None:
    desc = issue.get("description") or ""
    if not desc:
        return None
    m = _GH_URL_RE.search(desc)
    if not m:
        return None
    repo = m.group(1)
    # Strip trailing `.git` if URL pointed at clone form.
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo if _is_valid_repo(repo) else None


def _from_project_map(issue: dict, db_path: Path) -> str | None:
    project = issue.get("project") or {}
    project_id = project.get("id") if isinstance(project, dict) else None
    if not project_id:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT repo_full_name FROM project_repo_map WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return row["repo_full_name"] if row else None


def _from_team_default(issue: dict, db_path: Path) -> str | None:
    team = issue.get("team") or {}
    team_key = team.get("key") if isinstance(team, dict) else None
    if not team_key:
        # Fall back to identifier prefix (e.g. ENG-123 → ENG).
        ident = issue.get("identifier") or ""
        team_key = ident.split("-", 1)[0] if "-" in ident else None
    if not team_key:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT repo_full_name FROM team_repo_default WHERE team_key = ?",
            (team_key,),
        ).fetchone()
    return row["repo_full_name"] if row else None


async def resolve_repo_full_name(
    issue: dict,
    *,
    linear: LinearClient | None = None,
    db_path: Path = DB_PATH,
) -> str | None:
    """Determine the GitHub `owner/name` for a Linear issue, or None.

    `linear` is accepted for future enrichment (e.g. fetching missing fields)
    but is unused today; callers should pass the issue dict pre-populated with
    `labels.nodes{name}`, `description`, `project{id}`, and `team{key}`.
    """
    for resolver in (
        _from_labels,
        _from_description,
        lambda i: _from_project_map(i, db_path),
        lambda i: _from_team_default(i, db_path),
    ):
        try:
            repo = resolver(issue)  # type: ignore[arg-type]
        except sqlite3.Error:
            repo = None
        if repo and _is_valid_repo(repo):
            return repo
    return None


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Manage repo resolution mappings.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create tables")

    sp = sub.add_parser("set-project", help="Map project_id → repo")
    sp.add_argument("project_id")
    sp.add_argument("repo")

    st = sub.add_parser("set-team", help="Map team_key → default repo")
    st.add_argument("team_key")
    st.add_argument("repo")

    sr = sub.add_parser("resolve", help="Resolve a JSON issue from stdin")

    args = ap.parse_args()
    if args.cmd == "init":
        provision_db()
        print(f"provisioned {DB_PATH}")
    elif args.cmd == "set-project":
        provision_db()
        set_project_repo(args.project_id, args.repo)
        print(f"{args.project_id} -> {args.repo}")
    elif args.cmd == "set-team":
        provision_db()
        set_team_default_repo(args.team_key, args.repo)
        print(f"{args.team_key} -> {args.repo}")
    elif args.cmd == "resolve":
        import asyncio

        issue = json.load(sys.stdin)
        print(asyncio.run(resolve_repo_full_name(issue)) or "")
