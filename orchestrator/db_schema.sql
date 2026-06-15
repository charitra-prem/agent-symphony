-- Pipeline orchestrator schema (SQLite, WAL mode).
-- Single file: /var/lib/triage/orchestrator.db
-- Apply: sqlite3 orchestrator.db < db_schema.sql

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;   -- WAL + NORMAL = safe + fast on a single box

-- ---------------------------------------------------------------------------
-- 1. issues — one row per Linear issue. Source of truth for orchestrator state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS issues (
    -- identity
    linear_id           TEXT PRIMARY KEY,        -- Linear UUID
    identifier          TEXT NOT NULL UNIQUE,    -- e.g. ENG-142
    title               TEXT NOT NULL,
    team_key            TEXT NOT NULL,           -- e.g. ENG

    -- state machine
    state               TEXT NOT NULL,           -- IssueState enum value
    prev_state          TEXT,                    -- for "back to previous" on unblock
    loop_count          INTEGER NOT NULL DEFAULT 0,   -- # of times we re-entered DEV_IN_PROGRESS
    blocked_reason      TEXT,

    -- routing decision (from PM triage)
    tier                TEXT,                    -- 'junior' | 'senior'
    harness             TEXT,                    -- e.g. 'claude-sonnet-max', 'kimi', 'deepseek'
    task_type           TEXT,
    complexity          TEXT,

    -- agent ownership
    current_owner_agent TEXT,                    -- 'pm' | 'dev:<harness>' | 'qa:junior' | ...
    current_attempt     INTEGER NOT NULL DEFAULT 0,  -- bumped each loop, used as idempotency key

    -- per-stage comment ids (so we can edit in place)
    triage_comment_id   TEXT,
    dev_comment_id      TEXT,
    junior_qa_comment_id TEXT,
    senior_qa_comment_id TEXT,
    design_qa_comment_id TEXT,
    merge_comment_id    TEXT,

    -- artifacts
    branch_name         TEXT,                    -- e.g. eng-142-fix-login
    repo_full_name      TEXT,                    -- e.g. premai-io/fluso-frontend
    deploy_url          TEXT,                    -- https://dev-eng-142.local-pcci.org

    -- timestamps
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    triaged_at          TEXT,
    merged_to_dev_at    TEXT,
    merged_to_main_at   TEXT,
    done_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state);
CREATE INDEX IF NOT EXISTS idx_issues_team ON issues(team_key);
CREATE INDEX IF NOT EXISTS idx_issues_updated ON issues(updated_at);


-- ---------------------------------------------------------------------------
-- 2. state_events — append-only audit log. One row per transition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS state_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    linear_id       TEXT NOT NULL,
    from_state      TEXT,                        -- NULL for first event
    to_state        TEXT NOT NULL,
    actor           TEXT NOT NULL,               -- 'webhook:linear' | 'webhook:github' | 'agent:dev' | 'sweep' | 'human'
    reason          TEXT,                        -- short human-readable
    payload         TEXT,                        -- JSON: webhook delivery_id, agent verdict, etc.
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    FOREIGN KEY (linear_id) REFERENCES issues(linear_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_issue ON state_events(linear_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_to_state ON state_events(to_state);


-- ---------------------------------------------------------------------------
-- 3. qa_runs — one row per QA invocation (junior + senior + design). Verdicts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qa_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    linear_id           TEXT NOT NULL,
    attempt             INTEGER NOT NULL,        -- matches issues.current_attempt at start
    kind                TEXT NOT NULL,           -- 'junior' | 'senior' | 'design' | 'sanity' | 'ac'
    pr_number           INTEGER,                 -- github PR being tested
    commit_sha          TEXT,                    -- exact SHA tested

    -- verdict
    verdict             TEXT,                    -- 'pass' | 'fail' | 'error' | 'running'
    feedback            TEXT,                    -- markdown, fed back into dev agent prompt on fail
    artifacts_url       TEXT,                    -- e.g. screenshots bundle, Playwright report

    -- agent meta
    agent_name          TEXT,                    -- e.g. 'qa-junior-react'
    cost_usd            REAL,
    duration_s          INTEGER,

    -- idempotency
    idempotency_key     TEXT NOT NULL UNIQUE,    -- "{linear_id}:{state}:{attempt}:{kind}"

    -- timing
    started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at         TEXT,

    FOREIGN KEY (linear_id) REFERENCES issues(linear_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_qa_issue ON qa_runs(linear_id, attempt, kind);
CREATE INDEX IF NOT EXISTS idx_qa_verdict ON qa_runs(verdict);


-- ---------------------------------------------------------------------------
-- 4. pr_links — issue ↔ github PR mapping. 1:N since loops produce multiple PRs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pr_links (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    linear_id           TEXT NOT NULL,
    repo_full_name      TEXT NOT NULL,           -- e.g. premai-io/fluso-frontend
    pr_number           INTEGER NOT NULL,
    base_branch         TEXT NOT NULL,           -- 'dev' or 'main'
    head_branch         TEXT NOT NULL,           -- e.g. eng-142-fix-login
    role                TEXT NOT NULL DEFAULT 'feature',  -- 'feature' | 'revert' | 'hotfix'
    state               TEXT NOT NULL,           -- 'open' | 'merged' | 'closed'
    merged_sha          TEXT,
    opened_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    merged_at           TEXT,

    UNIQUE (repo_full_name, pr_number),
    FOREIGN KEY (linear_id) REFERENCES issues(linear_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_prs_issue ON pr_links(linear_id);
CREATE INDEX IF NOT EXISTS idx_prs_state ON pr_links(state);


-- ---------------------------------------------------------------------------
-- 5. webhook_inbox — dedupe re-deliveries from Linear / GitHub.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_inbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,               -- 'linear' | 'github'
    delivery_id     TEXT NOT NULL,               -- Linear-Delivery or X-GitHub-Delivery header
    event_type      TEXT NOT NULL,               -- e.g. 'Issue.create', 'pull_request.opened'
    payload_hash    TEXT NOT NULL,               -- sha256 of raw body
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    processed_at    TEXT,
    processing_result TEXT,                      -- 'ok' | 'noop' | 'error:<msg>'

    UNIQUE (source, delivery_id)
);

CREATE INDEX IF NOT EXISTS idx_inbox_source_type ON webhook_inbox(source, event_type);


-- ---------------------------------------------------------------------------
-- 6. team_workflow_states — cache of Linear team workflow state ids.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS team_workflow_states (
    team_id         TEXT NOT NULL,
    state_name      TEXT NOT NULL,               -- e.g. 'In Progress', 'In Review', 'Done'
    state_id        TEXT NOT NULL,               -- Linear UUID
    state_type      TEXT,                        -- 'started' | 'completed' | 'cancelled' | etc.
    refreshed_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (team_id, state_name)
);


-- ---------------------------------------------------------------------------
-- Helpful views
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_active_issues AS
SELECT linear_id, identifier, title, state, tier, harness, loop_count,
       current_owner_agent, updated_at
FROM issues
WHERE state NOT IN ('done', 'cancelled')
ORDER BY updated_at DESC;

CREATE VIEW IF NOT EXISTS v_latest_qa AS
SELECT q.*
FROM qa_runs q
INNER JOIN (
    SELECT linear_id, kind, MAX(attempt) AS max_attempt
    FROM qa_runs
    GROUP BY linear_id, kind
) latest
  ON q.linear_id = latest.linear_id
 AND q.kind = latest.kind
 AND q.attempt = latest.max_attempt;
