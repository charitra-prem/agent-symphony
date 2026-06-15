# Pipeline orchestrator

State-machine service that drives a Linear issue from creation through dev, junior QA,
acceptance check, dev-branch merge + deploy, senior + design QA, and merge to main.

Sibling to the existing `triage_service.py` (which still handles the PM-triage step).
This service runs alongside it.

## Files in this directory

| File | What |
|---|---|
| `state_machine.md` | Full design doc — states, transitions, loop-back paths, idempotency, library choice rationale |
| `db_schema.sql` | SQLite schema — `issues`, `state_events`, `qa_runs`, `pr_links`, `webhook_inbox`, `team_workflow_states` |
| `orchestrator_skeleton.py` | FastAPI app — webhook handlers, state driver, agent invocation stubs (~400 lines) |
| `comment_templates.md` | Markdown templates for each stage's Linear comment, edited in place |
| `README.md` | (this file) |

## Run

```bash
cd /tmp/triage/orchestrator
pip install fastapi uvicorn pyyaml httpx        # same deps as triage_service.py
export ORCHESTRATOR_DB=/var/lib/triage/orchestrator.db
export LINEAR_API_KEY=lin_api_...
export LINEAR_WEBHOOK_SECRET=...
export GITHUB_WEBHOOK_SECRET=...
export GITHUB_TOKEN=ghp_...

uvicorn orchestrator_skeleton:app --host 127.0.0.1 --port 8089
```

On first start it will create `/var/lib/triage/orchestrator.db` from `db_schema.sql`.

## Webhooks

Point these at the cloudflared tunnel:

| Source | URL | Headers verified |
|---|---|---|
| Linear | `https://triage.local-pcci.org/orch/webhook/linear` | `Linear-Signature` (HMAC-SHA256) |
| GitHub | `https://triage.local-pcci.org/orch/webhook/github` | `X-Hub-Signature-256` (HMAC-SHA256 with `sha256=` prefix) |

GitHub events needed: `pull_request` (opened, closed, synchronize), `pull_request_review`,
`check_suite` (optional, for CI gating).

Agents POST results to `https://triage.local-pcci.org/orch/agent-callback` with:

```json
{
  "linear_id": "...",
  "kind": "junior_qa" | "senior_qa" | "design_qa" | "ac" | "sanity" | "dev",
  "attempt": 1,
  "verdict": "pass" | "fail" | "error",
  "feedback": "<markdown>",
  "artifacts_url": "...",
  "pr_number": 142,
  "commit_sha": "abc...",
  "cost_usd": 0.42,
  "duration_s": 117
}
```

## Environment variables

| Var | Required | Default | What |
|---|---|---|---|
| `LINEAR_API_KEY` | yes | — | Personal API key, for comments + status updates |
| `LINEAR_WEBHOOK_SECRET` | yes (prod) | — | HMAC secret. Unset = accept all (dev only) |
| `GITHUB_WEBHOOK_SECRET` | yes (prod) | — | HMAC secret |
| `GITHUB_TOKEN` | yes | — | Fine-grained PAT: PR read/write, label write, merge |
| `ORCHESTRATOR_DB` | no | `/var/lib/triage/orchestrator.db` | SQLite path |
| `MAX_PARALLEL_ISSUES` | no | `8` | Per-issue parallelism cap |

## Dependencies on other services

| Service | Where | Role |
|---|---|---|
| `triage_service.py` | sibling FastAPI on :8088 | PM-triage stage. Posts harness recommendation, sets `tier`/`harness` on the issue row, then calls back to advance state to `TRIAGED`. |
| **Dev agents** (Kimi, DeepSeek, Claude) | invoked via `pi-pcci` or similar harness | Write code + tests. Open PR. Callback when PR ready. |
| **Junior QA agent** | runs in same Hetzner box, isolated venv | Pytest/Vitest/Playwright per stack. Callback verdict. |
| **Senior QA agent** | runs locally, vision-capable Claude w/ Playwright+Stagehand | Drives the dev deploy URL. Callback with screenshots. |
| **Design QA agent** | calls Penpot API + pixel diff | Callback with drift %. |
| **Penpot** | external SaaS / self-hosted | Source of design truth. |
| **PCCI proxy** :3000 | sibling | LLM gateway for all agents. |
| **Traefik** | sibling | Routes `dev-<id>.local-pcci.org` to ephemeral containers. |
| **cloudflared** | sibling | Exposes the wildcard `*.local-pcci.org`. |

## Linear ↔ GitHub: what's already automatic vs. what we add

Linear's native GitHub integration handles:

- **Auto-linking** PRs to issues when the PR body has `Fixes ENG-142` / `Closes ENG-142` / `Resolves ENG-142`, *or* when the branch name starts with the issue identifier (e.g. `eng-142-fix-login`).
- Posting reciprocal linkback comments.
- Workflow-status automation on PR open/merge (**turn this OFF per team** — we drive status ourselves).

We add on top:

- Local `pr_links` mapping (don't round-trip Linear's API for PR ↔ issue lookups).
- Workflow status driven via GraphQL `issueUpdate(input: { stateId: $stateId })` — state-id cache in `team_workflow_states`.
- Stage-owned, edit-in-place Linear comments via `commentUpdate` mutation.

## What's NOT implemented yet (honest list)

- **`triage_service.py` integration glue** — orchestrator currently logs `invoke_pm_triage` but doesn't actually call the existing service. Wire-up needed: either internal HTTP POST to `:8088`, or move triage into this process.
- **Actual agent harness calls** — `invoke_dev_agent`, `invoke_junior_qa`, `invoke_senior_qa`, `invoke_design_qa`, `run_ac_check` are stubs. They log and return. Real implementations need to spawn subprocesses / call into `pi-pcci` / talk to remote agent daemons.
- **`docker-compose.dev.yml` spin-up** — `spin_up_dev_deploy` is a stub. Needs: per-repo compose template, Traefik label generation, port allocation, `deploys` tracking table, idle teardown cron.
- **Penpot integration** — no client yet. `invoke_design_qa` needs a Penpot API token + diffing strategy (probably `pixelmatch` + node screenshots).
- **PR merge** — `merge_pr_to_dev` / `merge_pr_to_main` stubs. Needs `gh api -X PUT /repos/.../pulls/N/merge` with squash strategy + commit message templating.
- **`agent-callback` body parsing** — endpoint is wired up but doesn't yet write `qa_runs` rows or check both senior+design verdicts before advancing to `READY_FOR_MAIN`. Driver logic for the join-state needs filling in.
- **Comment editing** — orchestrator never calls `linear.edit_comment` yet. Should be hooked into `transition()` as a side effect, gated on which stage owns the current comment id.
- **Linear status sync** — `team_workflow_states` table exists but isn't populated; need a startup task to fetch each team's workflow states once and cache.
- **Revert flow** — `MERGED_TO_MAIN` → `QA_FAIL` should auto-open a revert PR. Not implemented.
- **Multi-repo support** — schema has `repo_full_name` per PR but the dev deploy logic assumes one repo per issue. Cross-repo features need orchestration.
- **Auth on `/agent-callback`** — currently unauthenticated. Should require a shared secret header.
- **Metrics / dashboard** — `state_events` is the source. No UI yet. (User has the prem-ops-monitor pattern; could extend.)
- **Slack / paging on `BLOCKED`** — log only. Add a webhook when humans need to step in.

## systemd unit

Place at `/etc/systemd/system/orchestrator.service`:

```ini
[Unit]
Description=pipeline orchestrator (Linear → dev → QA → main)
After=network.target triage.service
Wants=triage.service

[Service]
Type=simple
User=triage
WorkingDirectory=/opt/triage/orchestrator
EnvironmentFile=/etc/triage/orchestrator.env
ExecStart=/opt/triage/.venv/bin/uvicorn orchestrator_skeleton:app --host 127.0.0.1 --port 8089
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now orchestrator.service
sudo journalctl -u orchestrator.service -f
```

cloudflared/Traefik need a route added:

```
triage.local-pcci.org/orch/* → http://127.0.0.1:8089/
```

(Strip `/orch` prefix at the proxy so the FastAPI routes match.)
