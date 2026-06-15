# agent-symphony

Symphony-style autonomous engineering pipeline running on top of Linear.

A Linear issue lands → PM agent triages and recommends a harness → orchestrator
spins up a git worktree → coding agent (Claude / Codex / Pi / Kimi)
implements → junior QA runs tests + AC check → senior QA spins a dev deploy
and visual-checks against acceptance criteria → design QA diffs against Penpot
tokens → PR merges if everything passes.

Inspired by:
- [openai/symphony](https://github.com/openai/symphony) — for the
  workflow.md, reconciliation, per-state caps, phase tracking, and HTTP
  control surface.
- [multica-ai/multica](https://github.com/multica-ai/multica) — for the
  agents-as-assignees UX (planned in Phase 2).
- [coderabbitai/git-worktree-runner](https://github.com/coderabbitai/git-worktree-runner)
  — for per-issue worktree isolation.

## Layout

```
agent-symphony/
├── orchestrator/                # FastAPI pipeline driver (port 8089)
│   ├── orchestrator.py          # 17-state machine + sweep + webhooks
│   ├── workflow.py              # hot-reloadable workflow.md loader
│   ├── worktree.py              # gtr-based per-issue worktrees
│   ├── db_schema.sql            # sqlite schema
│   └── agents/
│       ├── dispatcher.py        # reconcile + eligibility + per-state caps
│       ├── phase.py             # run-attempt phase observability
│       ├── stall_detector.py    # tmux stall sweeper
│       ├── control_api.py       # /api/v1 control surface + dashboard
│       ├── dev_agent_runner.py  # spawns Claude/Codex/Pi tmux sessions
│       ├── junior_qa.py         # framework detect + test runner
│       ├── senior_qa.py         # visual-qa against dev deploy
│       ├── design_qa.py         # Penpot DTCG token drift
│       ├── github_pr.py         # PR open/merge via gh CLI
│       ├── dev_deploy.py        # ephemeral Traefik-routed deploys
│       ├── penpot_client.py     # Penpot API client
│       ├── repo_resolver.py     # extract repo URL from issue
│       ├── linear_status.py     # Linear GraphQL state writes
│       └── comment_handler.py   # /triage /retry /pass /cancel commands
├── triage/                      # FastAPI triage service (port 8088)
│   ├── triage_service.py        # webhook ingestion + PM dispatch
│   ├── claude_session.py        # persistent tmux Claude session
│   ├── linear_client.py         # Linear GraphQL client
│   ├── pm_prompt.md             # PM agent system prompt
│   └── routing_matrix.yaml      # harness/persona selection by task type
└── workflow.example.md          # operator config, drop at /etc/triage/workflow.md
```

## Quick start

```bash
# orchestrator + triage both need:
#   /etc/triage/env with LINEAR_API_KEY, LINEAR_WEBHOOK_SECRET, GITHUB_TOKEN
#   /etc/triage/workflow.md (use workflow.example.md as template)

cd orchestrator && python -m venv .venv && .venv/bin/pip install fastapi uvicorn pyyaml watchdog
cd triage      && python -m venv .venv && .venv/bin/pip install -r requirements.txt

systemctl enable --now triage orchestrator
```

## Dashboards & control

- Dashboard: `https://<host>/`
- State JSON: `GET /api/v1/state`
- Force a sweep: `POST /api/v1/refresh`
- Per-issue debug: `GET /api/v1/issue/{identifier}`
- Retry a blocked issue: `POST /api/v1/issue/{identifier}/retry`
- Cancel an issue: `POST /api/v1/issue/{identifier}/cancel`
