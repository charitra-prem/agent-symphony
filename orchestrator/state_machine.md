# Pipeline Orchestrator — State Machine

> Drives a Linear issue from creation → dev → junior QA → dev branch → senior + design QA → main.
> Built on top of the existing `triage_service.py` (which handles the PM-triage step).

---

## 1. State enum

```python
class IssueState(str, Enum):
    NEW              = "new"                 # Linear webhook just arrived
    TRIAGING         = "triaging"            # PM agent is classifying
    TRIAGED          = "triaged"             # PM done → harness picked
    DEV_ASSIGNED     = "dev_assigned"        # dev agent claimed it (working branch created)
    DEV_IN_PROGRESS  = "dev_in_progress"     # code being written
    DEV_DONE         = "dev_done"            # PR open against `dev` branch, tests green locally
    JUNIOR_QA        = "junior_qa"           # junior-QA agent running against PR
    JUNIOR_QA_FAIL   = "junior_qa_fail"     # loops back to DEV_IN_PROGRESS
    ACCEPTANCE_CHECK = "acceptance_check"    # AC verification (strict)
    MERGED_TO_DEV    = "merged_to_dev"       # squash-merged into `dev`
    DEV_DEPLOY       = "dev_deploy"          # docker-compose up on Hetzner ephemeral port
    SENIOR_QA        = "senior_qa"           # vision QA via Playwright/Stagehand
    DESIGN_QA        = "design_qa"           # Penpot parity check (parallel w/ senior_qa)
    QA_FAIL          = "qa_fail"             # loops back — could be dev or design
    READY_FOR_MAIN   = "ready_for_main"      # both green
    MERGED_TO_MAIN   = "merged_to_main"      # squash-merged into `main`
    SANITY_CHECK     = "sanity_check"        # post-merge smoke
    DONE             = "done"
    BLOCKED          = "blocked"             # human intervention
    CANCELLED        = "cancelled"
```

### Allowed transitions

```
NEW              → TRIAGING
TRIAGING         → TRIAGED | BLOCKED
TRIAGED          → DEV_ASSIGNED
DEV_ASSIGNED     → DEV_IN_PROGRESS
DEV_IN_PROGRESS  → DEV_DONE | BLOCKED
DEV_DONE         → JUNIOR_QA
JUNIOR_QA        → JUNIOR_QA_FAIL | ACCEPTANCE_CHECK
JUNIOR_QA_FAIL   → DEV_IN_PROGRESS                # loop
ACCEPTANCE_CHECK → MERGED_TO_DEV | JUNIOR_QA_FAIL
MERGED_TO_DEV    → DEV_DEPLOY
DEV_DEPLOY       → SENIOR_QA                       # SENIOR_QA fans out to DESIGN_QA in parallel
SENIOR_QA        → READY_FOR_MAIN | QA_FAIL
DESIGN_QA        → READY_FOR_MAIN | QA_FAIL
QA_FAIL          → DEV_IN_PROGRESS                 # loop
READY_FOR_MAIN   → MERGED_TO_MAIN
MERGED_TO_MAIN   → SANITY_CHECK
SANITY_CHECK     → DONE | QA_FAIL

(any)            → BLOCKED | CANCELLED             # human/admin override
BLOCKED          → (any prior non-terminal)
```

`READY_FOR_MAIN` is reached only when **both** `SENIOR_QA` and `DESIGN_QA` have posted a `pass`
verdict on the latest commit SHA. The driver (`next_action`) waits on both `qa_runs` rows.

---

## 2. Per-state side effects

| State | Linear comment thread | Linear workflow status | GitHub label | Other |
|---|---|---|---|---|
| `NEW` | post 👀 "seen" stub | `Triage` | — | insert row in `issues` |
| `TRIAGING` | edit → 🤖 "PM analyzing" | `Triage` | — | invoke triage agent |
| `TRIAGED` | edit → 🧠 routing decision | `Todo` | — | record harness + tier |
| `DEV_ASSIGNED` | new comment "🤖 Dev (<harness>) picked up" | `In Progress` | `agent:dev` | spawn dev worker |
| `DEV_IN_PROGRESS` | (silent) | `In Progress` | — | dev pushes commits |
| `DEV_DONE` | edit → "PR opened: #N" | `In Review` | `agent:qa-needed` | create draft PR |
| `JUNIOR_QA` | new "🧪 Junior QA running…" | `In Review` | `agent:qa-running` | invoke junior QA |
| `JUNIOR_QA_FAIL` | edit → "❌ Junior QA: <reasons>" | `In Progress` | `agent:dev` | feedback → dev |
| `ACCEPTANCE_CHECK` | edit → "✅ Junior QA passed, AC check…" | `In Review` | — | strict AC verify |
| `MERGED_TO_DEV` | edit → "🎯 Merged into `dev`" | `In Review` | `branch:dev` | gh PR merge |
| `DEV_DEPLOY` | new "🚀 Deployed at https://dev-<id>.local-pcci.org" | `In Review` | `env:dev` | docker-compose up |
| `SENIOR_QA` | new "🔍 Senior QA (vision) running…" | `In Review` | `agent:senior-qa` | Playwright/Stagehand |
| `DESIGN_QA` | new "🎨 Design QA (Penpot diff) running…" | `In Review` | `agent:design-qa` | Penpot parity |
| `QA_FAIL` | edit → "❌ <which>: <reasons>" | `In Progress` | `agent:dev` | loop |
| `READY_FOR_MAIN` | edit → "✅ All QA green" | `Ready` | `ready-for-main` | open `dev→main` PR if not exists |
| `MERGED_TO_MAIN` | edit → "🎯 Merged into `main`" | `Done` | `branch:main` | gh PR merge |
| `SANITY_CHECK` | edit → "👀 Post-merge sanity…" | `Done` | — | re-run senior QA on prod-like |
| `DONE` | edit → "✅ Done" + 🎉 reaction | `Done` | — | close issue |
| `BLOCKED` | edit → "🛑 Blocked — <reason>" | `Blocked` | `blocked` | page humans |

**Comment thread strategy:** each *stage* (triage / dev / junior QA / senior QA / design QA / merge)
owns **one** Linear comment that is *edited in place* across its sub-stages. This avoids a 30-row
spammed thread. Templates in `comment_templates.md`.

---

## 3. Failure & loop-back paths

| Failure | Goes to | Carries forward |
|---|---|---|
| Triage agent crash / malformed JSON | `BLOCKED` | raw response in audit log |
| Dev agent timeout (> 60 min idle) | `BLOCKED` | last commit SHA |
| Junior QA fail | `DEV_IN_PROGRESS` | structured `feedback` JSON appended to dev agent's next prompt |
| AC check fail | `JUNIOR_QA_FAIL` (treated identically) | AC delta |
| Senior QA fail (functional bug) | `DEV_IN_PROGRESS` (revert merge to dev? — see below) | bug repro steps + screenshots |
| Design QA fail (Penpot drift) | `DEV_IN_PROGRESS` | side-by-side diff URLs |
| Sanity check fail | `QA_FAIL` → emergency revert of `main` PR | full revert log |

**Revert policy:** loops after `MERGED_TO_DEV` do *not* auto-revert the dev branch. The next dev
attempt produces a follow-up commit/PR. If the dev branch is too polluted, the orchestrator can
escalate to `BLOCKED`. Loops after `MERGED_TO_MAIN` *do* auto-open a revert PR — main must stay
clean.

A `loop_count` field on `issues` is incremented each time we re-enter `DEV_IN_PROGRESS` from a
failure. If `loop_count >= 3`, state forces to `BLOCKED` with comment "🛑 3 loops without
convergence — escalating to humans."

---

## 4. Persistence

**SQLite, single file** at `/var/lib/triage/orchestrator.db`, WAL mode.

Justification:
- Single-box deployment (Hetzner). No replication needed.
- Issue throughput is small (probably < 50 active at once).
- WAL handles webhook-bursty concurrent writes cleanly.
- Migration to Postgres later is one `pg_dump`-equivalent away — schema is portable (no SQLite-isms used).

Three core tables (full DDL in `db_schema.sql`):

- `issues` — one row per Linear issue, current state, harness, owner agent, PR ref.
- `state_events` — append-only audit log; one row per transition.
- `qa_runs` — one row per QA invocation (junior / senior / design), with verdict + artifacts URL.
- `pr_links` — issue ↔ PR mapping (1:N — issue can have multiple PR attempts on loop).

Audit log doubles as **jsonl-streamable** — `state_events.payload` is JSON, exportable for
post-mortems / training data.

---

## 5. Concurrent issues / worker pool

- FastAPI receives webhooks → enqueues a transition into `next_action`.
- A **single asyncio task per issue** (keyed by `linear_id`) drives that issue's state machine.
  Achieved with an `asyncio.Lock` per-issue, held in an in-memory dict guarded by a global lock.
- Cross-issue parallelism: the orchestrator can hold many per-issue tasks (default cap **8
  parallel issues**, configurable via `MAX_PARALLEL_ISSUES`).
- Agent invocations (dev / QA / design) are *external processes* — they don't block the asyncio
  loop. We poll their status (or accept their callback webhooks) and only advance state when they
  report completion.
- **Queue:** simple FIFO `asyncio.Queue` for "want to start work on issue X". When pool slots free
  up, next issue drains.

For docker-compose dev deploys, separately cap to **3 concurrent ephemeral environments** to
respect Hetzner RAM. Tracked in a `deploy_slots` table (not strictly necessary — we can
`docker ps | wc -l`).

---

## 6. Idempotency

**Webhook re-delivery** (Linear / GitHub both retry on non-2xx):
- Every webhook is logged to `webhook_inbox` with `(source, delivery_id, payload_hash)` as a
  UNIQUE key. Re-delivery is a no-op insert and returns 200 OK without re-running the handler.
- Linear sets `Linear-Delivery` header; GitHub sets `X-GitHub-Delivery`. Use those as
  `delivery_id`.

**State transitions:**
- Every transition asserts the *expected current state* (`UPDATE issues SET state=:new WHERE id=:id
  AND state=:expected`). If 0 rows updated → another worker already transitioned, log "lost race"
  and abort cleanly.
- This makes the state machine safe to drive from multiple webhook handlers concurrently.

**Agent retries:**
- Dev/QA agent invocations carry an idempotency key = `{issue_id}:{state}:{attempt}`.
- The agent host (Claude SDK / harness) is expected to dedupe by that key. If we *re-issue* the
  same call (e.g. on orchestrator restart), the agent returns the cached result instead of
  re-running.
- Result writes use `INSERT OR IGNORE` on `(qa_runs.idempotency_key)`.

**Linear comment edits:**
- The "stage-owning" comment id is stored on `issues.<stage>_comment_id`. Restarting mid-flight,
  the orchestrator looks up that id and continues editing instead of posting a fresh comment.

---

## 7. State-machine library choice

**Picked: hand-rolled.**

- `transitions` (pytransitions) is the most popular, but it mutates `self.state` magically and
  encourages stuffing logic into model classes — awkward to combine with SQLite-as-source-of-truth
  + per-issue lock model. Also weighs ~40kb of magic to do what a `dict[state, set[state]]` table
  does explicitly.
- `python-statemachine` (1.x, last release May 2026) has nicer async support and a more Pythonic
  API, but again expects the state to live on a Python object you keep around — we want the DB to
  be the source of truth.
- Our state graph is **~17 states, ~20 transitions**, all linearly inspectable. A flat
  `TRANSITIONS: dict[IssueState, set[IssueState]]` plus a `transition(issue_id, new_state)`
  function that does the conditional UPDATE is ~30 lines of code and matches the persistence
  model exactly.

If complexity grows past ~30 states / parallel regions, revisit `python-statemachine`.

---

## 8. Driver loop

```python
async def next_action(issue):
    """Decide what to do next given an issue's current state.

    Called on:
      - webhook arrival (Linear or GitHub)
      - agent callback (dev/QA agent reports completion)
      - periodic sweep (every 60s) to catch dropped events
    """
    match issue.state:
        case NEW:                  await transition_to_triaging(issue)
        case TRIAGING:             pass    # in-flight, will self-advance
        case TRIAGED:              await assign_dev_agent(issue)
        case DEV_ASSIGNED:         pass    # dev agent owns it now
        case DEV_DONE:             await start_junior_qa(issue)
        case JUNIOR_QA_FAIL:       await loop_back_to_dev(issue, feedback=...)
        case ACCEPTANCE_CHECK:     await run_ac_check(issue)
        case MERGED_TO_DEV:        await deploy_dev_branch(issue)
        case DEV_DEPLOY:           await fan_out_qa(issue)   # senior + design parallel
        case READY_FOR_MAIN:       await merge_to_main(issue)
        case MERGED_TO_MAIN:       await sanity_check(issue)
        case QA_FAIL:              await loop_back_to_dev(issue, ...)
        case DONE | BLOCKED | CANCELLED:  return
        case _:                    log.warning("no-op for state %s", issue.state)
```

---

## 9. Linear ↔ GitHub integration — what NOT to duplicate

Linear's native GitHub integration already handles:
- **Auto-linking** PRs ↔ issues if the PR body contains magic words (`Fixes ENG-142`, `Closes ENG-142`,
  `Resolves ENG-142`) *or* the branch name contains the issue identifier (e.g. `eng-142-foo`).
- Auto-transitioning Linear status on PR open/merge (configurable per team).
- Posting linkback comments in both directions.

**What we add on top:**
- We still write `pr_links` rows ourselves so the orchestrator has a local mapping without
  round-tripping Linear's API.
- Dev agents are told to **name branches `<issue-identifier>-<slug>`** so Linear's native linking
  works for free. Backup: include `Refs <ID>` in PR body.
- We override Linear's auto-status transitions (turn them OFF in team settings) because *we*
  drive the workflow status via GraphQL `issueUpdate`. Otherwise both systems fight.
- GitHub webhooks (PR opened, review submitted, merged, check_suite completed) are what
  *actually* advance the orchestrator state — Linear's GH integration is just a UX layer.

### Linear GraphQL — status update

```graphql
mutation UpdateIssueState($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
    issue { id state { id name } }
  }
}
```

`id` accepts either the UUID or the human identifier (`ENG-142`). `stateId` is the UUID of the
target workflow state — fetched once per team via:

```graphql
query TeamStates($teamId: String!) {
  team(id: $teamId) { states { nodes { id name } } }
}
```

We cache `state name → state id` per team in `team_workflow_states` table (or just a YAML on disk
since teams rarely add states).

---

## 10. Local "branch deploys" on Hetzner

No managed branch-deploy product. Pattern:

1. Each repo defines a `docker-compose.dev.yml` with `${HOSTNAME}` / `${PORT}` / `${BRANCH}`
   variables.
2. On `MERGED_TO_DEV`, the orchestrator:
   - `git fetch && git checkout dev && git pull` in `/srv/deploys/<repo>/<issue-id>/`
   - generates `.env` with `HOSTNAME=dev-<issue-id>.local-pcci.org`, ephemeral `PORT=$(shuf -i
     20000-29999 -n 1)`, `BRANCH=dev`
   - `docker compose -p dev-<issue-id> -f docker-compose.dev.yml up -d`
3. **Traefik** (already part of the Hetzner box — runs in front of cloudflared) picks up the
   container via labels:
   ```yaml
   labels:
     - "traefik.enable=true"
     - "traefik.http.routers.dev-${ISSUE_ID}.rule=Host(`dev-${ISSUE_ID}.local-pcci.org`)"
     - "traefik.http.services.dev-${ISSUE_ID}.loadbalancer.server.port=3000"
   ```
   Traefik routes by Host header — no port allocation collision risk.
4. Senior QA gets the URL `https://dev-<issue-id>.local-pcci.org`.
5. On `DONE` or `CANCELLED`, `docker compose -p dev-<issue-id> down -v` + `rm -rf` the worktree.

**Lifecycle cap:** ephemeral envs auto-`down` after 4h idle (cron sweep), and *always* `down` on
state ∈ {DONE, CANCELLED, BLOCKED for > 24h}. Tracked in `deploys` table (not in the v1 schema —
out of scope for orchestrator skeleton).

Why Traefik over nginx-proxy / Caddy-with-API: it's already running, label-driven (zero config
reloads), and integrates with the existing cloudflared tunnel via the wildcard `*.local-pcci.org`
DNS record.

---

## 11. Open questions deferred to v2

- Multi-repo issues (an issue that spans frontend + backend repos) — current schema assumes one PR
  per loop attempt; extension is a `pr_links.role` column we already include.
- Re-running design QA only (skip senior QA) when failure was design-only — current loop
  re-invokes both. Optimization for later.
- Agent cost accounting — the routing matrix tracks tier; we should record actual token spend per
  invocation in `qa_runs.cost_usd` (column is in schema, populated by agent harnesses).
