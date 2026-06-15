-- Phase-tracking columns + append-only event log.
-- Re-runnable: ALTERs are wrapped per-statement so a duplicate-column
-- error on one column does not skip the others. CREATE TABLE/INDEX
-- already use IF NOT EXISTS. Preferred runner: apply_migration_002.py
-- which does conditional ALTER via PRAGMA table_info.

BEGIN;
ALTER TABLE issues ADD COLUMN current_phase TEXT;
COMMIT;

BEGIN;
ALTER TABLE issues ADD COLUMN current_phase_at TEXT;
COMMIT;

CREATE TABLE IF NOT EXISTS phase_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  linear_id TEXT NOT NULL,
  attempt INTEGER,
  phase TEXT NOT NULL,
  note TEXT,
  at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_phase_events_linear_id ON phase_events(linear_id);
