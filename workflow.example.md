---
# Symphony-style orchestrator config. Hot-reloads when this file changes.
# Hand-editable. Invalid YAML keeps the last-known-good in memory.

tracker:
  kind: linear
  endpoint: https://api.linear.app/graphql
  api_key: $LINEAR_API_KEY
  required_labels: []           # gate: must have ALL of these to be dispatched
  active_states:                # source-of-truth states we'll act on
    - Backlog
    - Todo
    - In Progress
  terminal_states:              # we release the worktree + drop the issue
    - Done
    - Cancelled
    - Canceled
    - Duplicate
    - Closed
  block_on_open_blockers: true  # if Todo and any open blocker, defer

polling:
  interval_ms: 60000            # sweep cadence (webhook is primary; this is fallback)

workspace:
  root: /root/work/repos        # parent of <repo>/main and <repo>/main-worktrees

agent:
  max_concurrent_agents: 8      # global cap
  max_concurrent_agents_by_state:
    dev_assigned: 4
    dev_in_progress: 4
    junior_qa: 6
    senior_qa: 3
    design_qa: 2
    dev_deploy: 4
  max_retry_backoff_ms: 300000  # 5 min cap
  max_turns: 20
  stall_timeout_ms: 600000      # 10 min of no tmux activity -> kill

# Operator overrides — drop into a Linear issue description as a fenced
# yaml block ```workflow ... ``` to override for that issue only.
overrides_enabled: true
---

# Symphony Workflow

The orchestrator follows the Symphony spec (https://github.com/openai/symphony):
reconcile -> filter eligible -> sort -> dispatch -> reconcile again.

Edit this file directly; the orchestrator reloads it within ~5 seconds.
