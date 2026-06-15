# Linear comment templates

Each pipeline **stage** owns a single comment that is edited in place across its sub-stages. The
comment id is stored on `issues.<stage>_comment_id`. This keeps the thread to ~6 comments instead
of ~30.

Emojis are reused from the existing triage flow that the user confirmed works: 👀 🤖 🧠 ✅ ❌.

---

## Stage 1 — Triage

(Already implemented in `triage_service.py`. Kept here for reference.)

```md
👀 **Triage agent**: seen, picking up shortly…
```
↓
```md
🤖 **Triage agent**: PM (Claude Sonnet, Max plan) analyzing the issue…
```
↓
```md
🧠 **Triage agent**: classification done, forming recommendation…
```
↓
```md
**Triage recommendation**

→ Run: `<invoke>` — **<harness>** (<conf> confidence)
→ Fallback: `<invoke>` — **<harness>**

- **Task type**: <type>
- **Complexity**: <c>
- **Estimated time**: ~<min> min

**Reasoning**: <…>

<sub>posted by linear-triage. Edit `routing_matrix.yaml` on the box to change recommendations.</sub>
```

---

## Stage 2 — Dev

```md
🤖 **Dev agent** (<harness>): picking up — branch `<identifier>-<slug>`
```
↓ (mid-flight progress, optional)
```md
🤖 **Dev agent** (<harness>): working — <N> files changed, <M> tests added
<sub>last commit: <short-sha> · <commit-msg></sub>
```
↓ (success)
```md
✅ **Dev agent** (<harness>): PR opened — #<PR>
> <PR title>

- Branch: `<identifier>-<slug>` → `dev`
- Files changed: <N>
- Tests added: <M>
- Estimated review time: <…>
```
↓ (loop-back, after QA fail)
```md
🔁 **Dev agent** (<harness>) — loop <N>/3: addressing QA feedback
<details><summary>What's being fixed</summary>

<bullet list from feedback>

</details>
```

---

## Stage 3 — Junior QA

```md
🧪 **Junior QA**: running targeted tests against #<PR>…
```
↓ (pass)
```md
✅ **Junior QA**: pass on <commit-sha>

- Feature test suite: <N>/<N> passing
- Regression sweep: <M>/<M> passing
- Coverage of acceptance criteria: <…>

Promoting to AC check.
```
↓ (fail)
```md
❌ **Junior QA**: fail on <commit-sha>

**What's broken:**
- <bullet 1>
- <bullet 2>

**Repro:**
```bash
<commands>
```

Looping back to dev agent (attempt <N>/3).

<details><summary>Full Playwright/test report</summary>

[artifacts](<artifacts_url>)

</details>
```

---

## Stage 4 — Acceptance criteria check

```md
🔬 **AC check**: verifying every line of acceptance criteria…
```
↓ (pass)
```md
✅ **AC check**: every AC item satisfied. Merging to `dev`.

| AC | Verified |
|---|---|
| <ac 1> | ✅ <how> |
| <ac 2> | ✅ <how> |
```
↓ (fail — folds back into junior QA fail)
```md
❌ **AC check**: <N> ACs not satisfied.

| AC | Status |
|---|---|
| <ac 1> | ✅ |
| <ac 2> | ❌ <why> |

Looping back to dev agent.
```

---

## Stage 5 — Merge to dev + dev deploy

```md
🎯 **Merged to `dev`** — #<PR> squash-merged at <sha>.
```
↓
```md
🚀 **Dev deploy**: spinning up at https://dev-<identifier>.local-pcci.org …
```
↓ (ready)
```md
🚀 **Dev deploy** live: https://dev-<identifier>.local-pcci.org

- Branch: `dev` @ <sha>
- Container: `dev-<identifier>`
- Auto-teardown: <timestamp> (4h idle)
```

---

## Stage 6 — Senior QA (vision)

```md
🔍 **Senior QA** (vision, Playwright/Stagehand): running against dev deploy…
```
↓ (pass)
```md
✅ **Senior QA**: pass

- Acceptance criteria walkthrough: ✅
- Vision sanity (no broken layouts, no console errors): ✅
- Cross-flow regression: ✅

[full report](<artifacts_url>) · [screenshots](<screens_url>)
```
↓ (fail)
```md
❌ **Senior QA**: fail

**Bugs found:**
1. <bug with screenshot>
2. <bug>

[screenshots](<screens_url>) · [report](<artifacts_url>)

Looping back to dev agent (attempt <N>/3).
```

---

## Stage 7 — Design QA (Penpot parity)

```md
🎨 **Design QA**: diffing against Penpot file `<file_id>` …
```
↓ (pass)
```md
✅ **Design QA**: parity confirmed (<drift>% drift, under threshold).

| Surface | Drift | Status |
|---|---|---|
| <screen 1> | 0.4% | ✅ |
| <screen 2> | 1.2% | ✅ |

[side-by-side](<diff_url>)
```
↓ (fail)
```md
❌ **Design QA**: drift exceeds threshold.

| Surface | Drift | Status |
|---|---|---|
| <screen 1> | 0.4% | ✅ |
| <screen 2> | 8.7% | ❌ |

**Specific deltas:**
- Button padding off by 4px on <screen>
- Heading font weight is 500 in code, 600 in Penpot

[side-by-side](<diff_url>)

Looping back to dev agent.
```

---

## Stage 8 — Merge to main + sanity

```md
🎯 **Merged to `main`** — #<PR> squash-merged at <sha>.
```
↓
```md
👀 **Sanity check**: re-running senior QA against `main` deploy…
```
↓ (pass)
```md
✅ **Done** — sanity passed.

- Linear status → Done
- 🎉 reaction added
- Dev deploy torn down
```
↓ (fail)
```md
🚨 **Sanity check**: failed on `main`. Auto-reverting PR #<PR> and looping back.

<details><summary>What broke</summary>

<bug details>

</details>
```

---

## Stage X — Blocked

```md
🛑 **Blocked**: <short reason>

**Last known state**: <prev_state>
**Loop count**: <N>/3
**Last actor**: <agent>

<details><summary>Recent events</summary>

<state_events tail>

</details>

A human needs to unblock this. Reply with `/unblock <state>` to resume.
```
