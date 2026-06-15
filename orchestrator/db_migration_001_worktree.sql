-- Add worktree-tracking columns to issues table.
-- Re-runnable: ignore "duplicate column" errors.

ALTER TABLE issues ADD COLUMN worktree_path   TEXT;
ALTER TABLE issues ADD COLUMN worktree_branch TEXT;
ALTER TABLE issues ADD COLUMN repo_full_name  TEXT;
ALTER TABLE issues ADD COLUMN base_branch     TEXT DEFAULT 'dev';

CREATE INDEX IF NOT EXISTS idx_issues_worktree_path ON issues(worktree_path);
