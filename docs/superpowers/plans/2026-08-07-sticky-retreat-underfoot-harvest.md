# Sticky Retreat Underfoot Harvest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an empty, healthy Worker harvest a currently visible resource underfoot when its current threat has cleared, without weakening current-threat retreat, cargo return, healing, Core-full suppression, or sticky-retreat liveness.

**Architecture:** Keep the existing `ExplorationMemory.safe_retreat_workers` transaction and current-state threat calculation unchanged. Change only the per-Tick desired-intent arbitration in `economy_plan()`: a current visible resource underfoot may replace an already-selected `RETURN_SAFE` intent only when that Worker is absent from `threatened_worker_ids`. Do not clear sticky memory during the harvest attempt; authoritative cargo/resource state on the next Tick decides whether the Worker transitions to `RETURN_CORE`, retries harvest, or resumes `RETURN_SAFE`.

**Tech Stack:** Python 3.10 production runtime, Python `unittest`, direct Arena protocol client, JSONL production journal.

## Global Constraints

- Develop only in an isolated `codex/` feature branch/worktree. Never edit the protected primary `main` worktree.
- Before edits, run `git status --short --branch` and `git worktree list --porcelain`. If the primary worktree is dirty, stop and ask who owns the changes.
- Rebase the implementation branch onto the latest `main` before maintainer review or merge; resolve conflicts on the feature branch.
- Developer allow-list: `arena_agent/policy.py`, `tests/test_policy.py`, `docs/DEV-PHASE-AB-CLEAN-EVALUATION-DEFENSE.md`, and `docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md`.
- Do not modify `arena_agent/__main__.py`, `arena_agent/allocator.py`, `arena_agent/path.py`, traffic/ingress algorithms, combat behavior, production rules, Worker caps, frontier constants, deployment files, README, or external Obsidian notes.
- Do not add a new persistent state set, helper module, policy constant, feature flag, journal field, dependency, or protocol action.
- Use only the current authoritative state: current `RESOURCE.positions`, current Worker cargo/HP, and current `threatened_worker_ids`. Never use hidden or historical enemy/resource positions to choose the action.
- Preserve Python 3.10 syntax compatibility.
- The development Agent may commit its feature branch but must not merge, push `main`, deploy, restart Supervisor, or claim production acceptance. Main integration, GitHub, deployment, Obsidian status, and journal acceptance belong to the main maintainer.

## Incident Evidence And Root Cause

Production session `9ae3fcee6e8a` was scanned through Tick `67817`:

| Evidence | Result |
| --- | --- |
| Agent plans scanned | 59 |
| Empty Worker observations on a current visible resource | 12 |
| `HARVEST` decisions followed by `HARVEST_SUCCEEDED` | 10 |
| Pass-through observations | 2 |
| `HARVEST_FAILED` among the 12 | 0 |
| MANUAL action for either pass-through Worker/Tick | none |

The two confirmed pass-through records were:

| Tick | Worker | Position | Current threat | Sticky retreat | Agent action | Next authoritative result |
| --- | --- | --- | --- | --- | --- | --- |
| `67790` | `a054df2c-899a-409d-af8d-71005a62c495` | `[136,208]` | false | true | `MOVE / RETURN_SAFE` | moved to `[136,209]` |
| `67807` | `2c64c4b0-b0b6-4f7b-82d4-ab988c36508a` | `[137,202]` | false | true | `MOVE / RETURN_SAFE` | moved to `[137,203]` |

The second Worker provides the complete causal sequence:

```text
Tick 67802-67804: threatened=true, safe_retreat=true, RETURN_SAFE
Tick 67805-67806: threatened=false, safe_retreat=true, RETURN_SAFE
Tick 67807:       on_visible_resource=true, threatened=false,
                  safe_retreat=true, action=MOVE, intent=RETURN_SAFE
Tick 67808:       position changed, cargo remained 0,
                  UNIT_MOVE_SUCCEEDED and no harvest resolution
```

The whole session has unrelated MANUAL traffic and cannot support clean ROI conclusions. It is still valid action-level bug evidence because the affected Worker IDs had no MANUAL action on the two affected Ticks, the Agent plan itself selected `MOVE`, and the next authoritative event confirmed that move.

Root cause in `arena_agent/policy.py`:

1. Sticky retreat is assigned to `desired[worker.id]` before underfoot-resource arbitration.
2. The underfoot-resource branch currently requires `worker.id not in desired`.
3. Therefore an unthreatened sticky retreat outside the Core guard remains `RETURN_SAFE` and cannot select `RESOURCE`, even when `worker.position in state.resource_cells`.
4. This is an intentional interaction between earlier safety and dynamic-resource changes, not a transport, allocator, pathfinding, visibility, MANUAL precedence, or event-settlement failure.

## Required Priority Contract

The implementation must preserve this exact order:

```text
1. Core-full recovery/hold transaction remains unchanged.
2. cargo > 0 -> RETURN_CORE / DEPOSIT.
3. hp <= 1 -> RETURN_HEAL / HEAL.
4. empty + currently threatened -> RETURN_SAFE, even on a resource cell.
5. empty + healthy + not currently threatened + current resource underfoot
   + Core not full -> HARVEST, including an unthreatened sticky retreat.
6. unthreatened sticky retreat without a resource underfoot -> RETURN_SAFE.
7. ordinary current-visible resource allocation, then frontier exploration.
```

`safe_retreat_workers` must remain populated during the opportunistic harvest Tick. This is a one-Tick desired-intent override, not transaction cancellation:

- On `HARVEST_SUCCEEDED` with authoritative `cargo > 0`, existing `observe()` logic clears sticky retreat and cargo priority selects `RETURN_CORE`.
- If the resource remains currently visible and cargo remains zero, existing current-state retry semantics may select `HARVEST` again.
- If the resource disappears or harvest fails and the Worker remains empty outside Core radius 3, the preserved sticky state resumes `RETURN_SAFE`.
- If a current threat reappears, current threat always blocks the harvest override and selects `RETURN_SAFE`.
- Since an unthreatened sticky Worker is outside Core guard radius 3, removing it from ingress candidates for the harvest Tick cannot steal a local ingress token. Do not change ingress code to solve this bug.

## Rejected Alternatives

- **Clear `safe_retreat_workers` when a resource appears:** rejected because a failed/disappearing resource would cancel the safety transaction and can restore `RETURN_SAFE <-> EXPLORE` visibility-flap oscillation.
- **Harvest whenever a resource is underfoot, including current threat:** rejected because it weakens the explicit current-threat safety rule already covered by tests.
- **Run the global allocator before retreat arbitration:** rejected because this changes multi-Worker assignment, traffic, and current-threat behavior far beyond the incident.
- **Add a new `RETURN_SAFE_HARVEST` intent or journal schema:** rejected because official action remains `HARVEST`, existing `RESOURCE` intent and `worker_trace` fields already provide complete evidence.
- **Refactor all priorities into a new abstraction:** rejected as unnecessary for a four-condition arbitration correction.

---

### Task 1: Reproduce And Fix Underfoot Harvest During Cleared Sticky Retreat

**Files:**
- Modify: `tests/test_policy.py` near the existing underfoot-resource tests and defense tests
- Modify: `arena_agent/policy.py` in the underfoot-resource arbitration inside `economy_plan()`

**Interfaces:**
- Consumes: `economy_plan(state, memory)`, `ExplorationMemory.safe_retreat_workers`, current `threatened_worker_ids`, `Snapshot.resource_cells`, and the existing `desired: dict[str, tuple[Unit, Position, str]]`.
- Produces: existing `Plan.unit_actions[worker_id] == {"type": "HARVEST"}` and `Plan.worker_intents[worker_id] == (worker.position, "RESOURCE")`; no new API or state type.

- [ ] **Step 1: Confirm branch and baseline before editing**

Run:

```bash
git status --short --branch
git worktree list --porcelain
git diff --check
```

Expected: the implementation worktree is on a non-`main` `codex/` branch, with no unrelated changes and no diff errors.

- [ ] **Step 2: Add the failing success-transition regression test**

Add this method to `AgentTests` near `test_worker_at_current_visible_resource_preempts_frontier_and_harvests`:

```python
def test_unthreatened_sticky_safe_retreat_harvests_current_resource_underfoot(self):
    core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
    worker = {"kind": "UNIT", "id": "worker", "controlled": True,
              "position": [6, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
    enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
             "position": [8, 0], "unit_type": "WORKER", "hp": 2}
    memory = ExplorationMemory()

    threatened = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0,
        "population": 1, "objects": [core, worker, enemy], "events": []})
    first = economy_plan(threatened, memory)
    self.assertEqual(first.worker_intents["worker"], ((0, 0), "RETURN_SAFE"))

    resource_worker = {**worker, "position": [5, 0]}
    resource = {"kind": "RESOURCE", "positions": [[5, 0]]}
    cleared = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0,
        "population": 1, "objects": [core, resource_worker, resource], "events": []})
    harvest = economy_plan(cleared, memory)
    self.assertIn("worker", memory.safe_retreat_workers)
    self.assertEqual(harvest.worker_intents["worker"], ((5, 0), "RESOURCE"))
    self.assertEqual(harvest.unit_actions["worker"], {"type": "HARVEST"})

    carrying = snapshot_from_state(3, {"status": "ACTIVE", "resources": 0,
        "population": 1, "objects": [core, {**resource_worker, "cargo": 1}],
        "events": [{"event_id": "harvest", "event_type": "HARVEST_SUCCEEDED",
                    "actor_id": "worker", "position": [5, 0]}]})
    returned = economy_plan(carrying, memory)
    self.assertNotIn("worker", memory.safe_retreat_workers)
    self.assertEqual(returned.worker_intents["worker"], ((0, 0), "RETURN_CORE"))
```

- [ ] **Step 3: Add the failing failure-resume regression test**

Add this second method next to the first:

```python
def test_failed_underfoot_harvest_resumes_unthreatened_sticky_retreat(self):
    core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
    worker = {"kind": "UNIT", "id": "worker", "controlled": True,
              "position": [6, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
    enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
             "position": [8, 0], "unit_type": "WORKER", "hp": 2}
    memory = ExplorationMemory()

    threatened = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0,
        "population": 1, "objects": [core, worker, enemy], "events": []})
    economy_plan(threatened, memory)

    resource_worker = {**worker, "position": [5, 0]}
    resource = {"kind": "RESOURCE", "positions": [[5, 0]]}
    cleared = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0,
        "population": 1, "objects": [core, resource_worker, resource], "events": []})
    harvest = economy_plan(cleared, memory)
    self.assertEqual(harvest.unit_actions["worker"], {"type": "HARVEST"})

    failed = snapshot_from_state(3, {"status": "ACTIVE", "resources": 0,
        "population": 1, "objects": [core, resource_worker],
        "events": [{"event_id": "failed", "event_type": "HARVEST_FAILED",
                    "actor_id": "worker", "position": [5, 0]}]})
    resumed = economy_plan(failed, memory)
    self.assertIn("worker", memory.safe_retreat_workers)
    self.assertEqual(resumed.worker_intents["worker"], ((0, 0), "RETURN_SAFE"))
    self.assertEqual(resumed.unit_actions["worker"]["type"], "MOVE")
```

- [ ] **Step 4: Run the new tests red against the old implementation**

Run:

```bash
python3.12 -m unittest \
  tests.test_policy.AgentTests.test_unthreatened_sticky_safe_retreat_harvests_current_resource_underfoot \
  tests.test_policy.AgentTests.test_failed_underfoot_harvest_resumes_unthreatened_sticky_retreat -v
```

Expected before implementation: both tests fail at the Tick-2 `HARVEST` assertion because the existing plan selects `MOVE` with `RETURN_SAFE`. Save the failure output in the Agent handoff summary; do not weaken the assertions to make the old behavior pass.

- [ ] **Step 5: Implement the narrow desired-intent override**

Replace only the current underfoot-resource loop in `economy_plan()` with:

```python
# Current-state resource underfoot preempts stale RESOURCE/EXPLORE and an
# unthreatened sticky retreat, but never cargo return, healing, current
# threat retreat, or Core-full harvest suppression.
for worker in workers:
    existing = desired.get(worker.id)
    unthreatened_sticky_retreat = (
        existing is not None
        and existing[2] == "RETURN_SAFE"
        and worker.id not in threatened_worker_ids
    )
    if ((existing is None or unthreatened_sticky_retreat)
            and worker.cargo == 0
            and (worker.hp is None or worker.hp > 1)
            and not memory.core_full
            and worker.position in state.resource_cells):
        desired[worker.id] = (worker, worker.position, "RESOURCE")
```

Do not mutate `memory.safe_retreat_workers` here. Do not move the global allocator, ingress reconciliation, or threat calculation.

- [ ] **Step 6: Run the focused green and safety tests**

Run:

```bash
python3.12 -m unittest \
  tests.test_policy.AgentTests.test_unthreatened_sticky_safe_retreat_harvests_current_resource_underfoot \
  tests.test_policy.AgentTests.test_failed_underfoot_harvest_resumes_unthreatened_sticky_retreat \
  tests.test_policy.AgentTests.test_threatened_empty_worker_returns_safe_but_carrying_priority_and_ranger_protection_hold \
  tests.test_policy.AgentTests.test_current_resource_is_reconsidered_after_harvest_failure \
  tests.test_policy.AgentTests.test_empty_safe_retreat_completes_inside_core_guard_without_ingress \
  tests.test_policy.AgentTests.test_carrying_worker_inside_core_guard_still_returns_to_exact_core -v
```

Expected: 6 tests pass. The current-threat/resource test must still select `RETURN_SAFE`, while the cleared sticky-retreat tests select `HARVEST`.

- [ ] **Step 7: Run the complete local verification gate**

Run:

```bash
PATH="/opt/homebrew/opt/python@3.12/libexec/bin:$PATH" \
  python3 -m unittest discover -s tests -p 'test_*.py'
python3.12 -m compileall -q arena_agent
bash -n deploy/pxed-deploy.sh
git diff --check
```

Expected after adding two tests: 166 tests pass, `compileall` exits 0, shell syntax exits 0, and `git diff --check` prints nothing.

- [ ] **Step 8: Review the exact code scope and commit**

Run:

```bash
git diff -- arena_agent/policy.py tests/test_policy.py
git status --short
git add arena_agent/policy.py tests/test_policy.py
git commit -m "fix: harvest underfoot during cleared retreat"
```

Expected: only the policy loop and the two regression tests are included in this commit.

---

### Task 2: Update The Strategy Contracts Without Claiming Production Acceptance

**Files:**
- Modify: `docs/DEV-PHASE-AB-CLEAN-EVALUATION-DEFENSE.md` in the sticky retreat section
- Modify: `docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md` in the Phase B Worker-retreat summary

**Interfaces:**
- Consumes: the priority and state-transition contract implemented by Task 1.
- Produces: repository-authoritative documentation for future policy changes; no runtime interface.

- [ ] **Step 1: Update the detailed Phase A/B retreat contract**

After the sticky retreat paragraph in `docs/DEV-PHASE-AB-CLEAN-EVALUATION-DEFENSE.md`, add this exact contract:

```markdown
脚下当前可见资源的特例不取消 sticky retreat transaction。空载健康 Worker
仅在本 Tick 已无 current threat、Core 未满且自身正站在当前 `RESOURCE.positions`
时，以 `RESOURCE/HARVEST` 临时覆盖 `RETURN_SAFE`；`safe_retreat_workers`
保留到下一权威 state。采集成功且 cargo>0 后转 `RETURN_CORE`，资源消失或
采集失败后恢复 `RETURN_SAFE`；current threat 仍存在时绝不以采集覆盖撤退。
```

Also update the acceptance list so the resource case explicitly requires both halves:

```markdown
- current threat + resource underfoot: remain `RETURN_SAFE`, do not harvest;
- cleared current threat + sticky retreat + resource underfoot: `HARVEST`, then
  authoritative cargo decides `RETURN_CORE` or resumed `RETURN_SAFE`.
```

- [ ] **Step 2: Update the concise official strategy contract**

In `docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md`, extend the Worker retreat sentence with:

```markdown
远端 sticky retreat 不覆盖已清除 current threat 后的脚下当前可见资源：该 Tick
允许 `HARVEST`，但不提前清除撤退事务；current threat、carrying、injured 和
Core-full 仍保持更高优先级。
```

Do not write “已上线”, “线上通过”, or new session/PID/rollback evidence in either document. Those claims require maintainer deployment evidence.

- [ ] **Step 3: Verify documentation consistency and placeholder-free scope**

Run:

```bash
rg -n "脚下|underfoot|RETURN_SAFE|current threat|HARVEST" \
  docs/DEV-PHASE-AB-CLEAN-EVALUATION-DEFENSE.md \
  docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md
if git diff -U0 -- \
    docs/DEV-PHASE-AB-CLEAN-EVALUATION-DEFENSE.md \
    docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md \
    | rg '^\+.*(已上线|线上通过)'; then
  exit 1
fi
git diff --check
```

Expected: the new contract appears in both files, no added line makes an unsupported production claim, and the diff check is clean. Existing historical “已上线” text elsewhere in the documents is ignored.

- [ ] **Step 4: Commit the contract update**

Run:

```bash
git diff -- docs/DEV-PHASE-AB-CLEAN-EVALUATION-DEFENSE.md \
  docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md
git add docs/DEV-PHASE-AB-CLEAN-EVALUATION-DEFENSE.md \
  docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md
git commit -m "docs: define cleared retreat harvest priority"
```

Expected: the documentation commit contains only the two repository contract files.

## Developer Completion Gate

Before handing the feature branch to the main maintainer:

```bash
git status --short --branch
git log --oneline --decorate -3
git diff main...HEAD --stat
PATH="/opt/homebrew/opt/python@3.12/libexec/bin:$PATH" \
  python3 -m unittest discover -s tests -p 'test_*.py'
python3.12 -m compileall -q arena_agent
bash -n deploy/pxed-deploy.sh
git diff --check main...HEAD
```

Required handoff evidence:

- old implementation red result for both new tests;
- focused 6-test green result;
- fresh full-suite count and zero failures;
- compile, shell syntax, and diff-check results;
- exact commit IDs;
- branch/worktree path;
- confirmation that no push, merge, deployment, Supervisor change, or Obsidian edit occurred.

## Main Maintainer Integration Gate

The main maintainer, not the development Agent, performs these steps:

1. Fetch latest GitHub `main`.
2. Rebase the feature branch onto latest `main` and resolve conflicts on the feature branch.
3. Re-run all Developer Completion Gate commands after rebase.
4. Review `main...feature` for current-threat, cargo, injured, Core-full, ingress, and allocator noninterference.
5. Merge to protected `main`, push GitHub, and deploy only `arena-hero-agent` using the existing managed-source backup/rollback flow.
6. Keep the previous verified source backup until journal acceptance completes.

## Production Journal Acceptance

Deployment health alone is not bug acceptance. First prove the normal protocol chain:

```text
state Tick N
-> Agent plan Tick N contains HARVEST for the affected Worker
-> HTTP 202
-> received source=AGENT for Tick N
-> no MANUAL action for that Worker on Tick N
-> state Tick N+1 contains HARVEST_SUCCEEDED or HARVEST_FAILED
```

The natural production trigger must contain all of:

```text
earlier Tick: threatened_workers contains Worker ID
earlier/current memory: safe_retreat_workers contains Worker ID
harvest Tick: threatened_workers does not contain Worker ID
harvest Tick: on_visible_resource=true, cargo=0, healthy, Core not full
harvest Tick: intent_kind=RESOURCE, action=HARVEST
```

Then require one authoritative branch:

- Success: next state has `HARVEST_SUCCEEDED`, cargo becomes positive, and intent becomes `RETURN_CORE`.
- Failure/disappearance: next state has zero cargo and no current resource, sticky memory remains, and intent becomes `RETURN_SAFE` unless the Worker entered Core radius 3.

Non-regression window after deployment:

- zero current-threat Workers harvesting instead of retreating;
- zero carrying Workers losing `RETURN_CORE/DEPOSIT` priority;
- zero injured Workers losing `RETURN_HEAL/HEAL` priority;
- zero empty unthreatened Workers moving off a current visible underfoot resource because of sticky `RETURN_SAFE`;
- zero unexpected `HARVEST_FAILED` storm, cargo loss, stale Tick streak, non-202 response, or unexplained Core transaction;
- `unassigned_workers=[]`.

If no natural sticky-retreat-underfoot-resource event occurs during the observation window, report only deployment/protocol health and keep production bug acceptance pending. Do not infer acceptance from ordinary underfoot harvests.

The main maintainer may append the final commit, session, Tick chain, PID, and rollback path to the external Obsidian development record only after this evidence exists.

## Rollback Conditions

Immediately restore the previous verified Arena source backup and restart only `arena-hero-agent` if any of these are observed:

- a currently threatened empty Worker selects `HARVEST` instead of `RETURN_SAFE`;
- a carrying or injured Worker loses its existing higher-priority return action;
- repeated underfoot `HARVEST_FAILED` prevents both harvest and retreat progress;
- authentication/protocol failure, persistent non-202, stale Tick circuit breaker, cargo loss, or unexplained Core economy event begins after deployment.

After rollback, verify process status, a fresh `state -> plan -> 202 -> received -> next state.events` chain, and the restored policy file hash. Never stop global Supervisor or unrelated services.
