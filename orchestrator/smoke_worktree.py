"""Smoke test worktree.py against a real GitHub repo."""
import asyncio
import subprocess
import sys

sys.path.insert(0, "/root/work/orchestrator")
import worktree

FAKE_ISSUE = {
    "linear_id": "TEST-WORKTREE-002",
    "identifier": "WT-002",
    "repo_full_name": "premAI-io/premsql",
}


async def main() -> None:
    ref = await worktree.acquire(FAKE_ISSUE, base="main")
    print("worktree_path:", ref.worktree_path)
    print("branch_name:", ref.branch_name)
    print("exists?", ref.worktree_path.exists())
    status = subprocess.check_output(["git", "status", "-sb"], cwd=ref.worktree_path).decode().strip()
    print("git status:", status)

    (ref.worktree_path / "TEST_AGENT_TOUCH.md").write_text("# orchestrator smoke\n")
    msg = ref.issue_identifier + ": orchestrator smoke test"
    sha = await worktree.commit_and_push(ref, message=msg)
    print("pushed sha:", sha)

    await worktree.release(ref, merge=False)
    print("released")
    print("active worktrees:", await worktree.list_active())


if __name__ == "__main__":
    asyncio.run(main())
