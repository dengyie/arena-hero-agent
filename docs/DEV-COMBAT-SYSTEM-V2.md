# DEV: Arena Combat System V2

- Status: IMPLEMENTED; Section 2.1 is the current status authority; staged live rollout
  and clean combat acceptance remain evidence-gated
- Date: 2026-08-05
- Repository: `/Users/mango/project/arena-hero-agent`
- Reference implementation reviewed: `VelvetEvening/Arena-Crazy-Attack@88083db`
- Official rules baseline: gameplay v0.13, HTTP/WebSocket API v0.1
- Scope: Arena Agent only; official private state and command API only

## 1. Problem statement

The original design-time baseline had a verified economy, bounded exploration,
traffic control, Core ingress, and no complete combat product. The implemented
Agent now also has guarded Vanguard/Ranger lifecycle, Core-local defender
positioning, current-state combat proposals, v0.13 cell payloads, combat event
accounting, and a friendly-loss fuse. Live combat-event evidence remains
outstanding.

Current implemented combat boundary:

- existing Vanguard: SWEEP only when an enemy is currently visible in an
  adjacent cell and economic risk is present near the Core;
- existing Ranger: precision SHOOT only at a currently visible legal target,
  only while the Ranger is already within the Core guard radius;
- Vanguard/Ranger production and role assignment are implemented and deployed;
- defender positioning is Core-local and uses shared Worker reservations;
- visible threats can trigger Worker safe-retreat and a bounded friendly patrol:
  only when a current-state enemy threatens it, defenders may follow a current-state,
  owned, healthy, empty Worker anchor for at most `ESCORT_LEASE_TICKS=120`; without
  that current threat they remain Core-local. They never store enemy facts, escort
  carriers, pursue through fog, or continue after the lease/return conditions;
- target-free v0.13 cell-fire payloads are implemented but no live cell-fire
  event has yet been observed;
- no clean live combat episode has been observed in the latest sessions.

The target is a defensive combat system that can actually create and operate a
small guard force, protect the economy, and produce event-level combat evidence
without adopting hidden-information tracking, aggressive pursuit, Beacon
expeditions, or uncontrolled population growth.

## 2. Evidence baseline

### 2.1 Current repository and live state

Current authoritative cutoff (2026-08-06):

- repository tip and deployed commit: `7ff47cc`;
- local and pxed staging suite: 149 `unittest` tests pass; compile, deploy-shell
  syntax, and diff checks pass;
- production Supervisor mode: configured `live-precision`, PID `124297` at deploy;
- session `7ca42f757d04`: 56 resolved Tick, 55
  `combat_metric_eligible=true`, 0 `economy_metric_eligible`, 4 real
  `DEPOSIT_SUCCEEDED`, 0 move failure, 0 friendly death/cargo loss;
- the 50 combat-eligible Tick gate is satisfied; C12 remains blocked only by the
  three real clean combat episodes and SWEEP/precision/cell-intercept coverage;
- economy ROI remains ineligible because the baseline has 17 Workers and MANUAL
  Worker actions continue. Combat eligibility does not override that conclusion.

Historical design-time baseline (superseded; retained for provenance):

- repository tip: `a4ed71f`;
- local suite: 89 `unittest` tests pass;
- inspected design-time sessions had only Workers, population 15-17;
- latest inspected session had same-session `state -> HTTP 202 -> received`,
  stable RSS near 20 MiB, real harvest/deposit events, and no combat events;
- the design-time baseline was externally over the ordinary Worker cap, therefore
  `economy_metric_eligible=false`; it can prove protocol and action outcomes but
  cannot prove clean economy ROI. Combat eligibility is evaluated separately.

### 2.2 Official rules that constrain the design

- Worker HP 2, Vanguard HP 4, Ranger HP 2.
- Vanguard SWEEP deals 1 simultaneous damage to every enemy entity in one
  adjacent cardinal cell.
- Ranger SHOOT uses `expected_cell` at cardinal or exact 45-degree range 1-3.
  Only intermediate obstacles block fire; Units and Cores do not.
- Since rules v0.13, `target_id` is optional. A target-free cell shot resolves
  after movement against the lowest-HP hostile in that cell. Precision mode
  remains compatible but misses when the named target moves.
- Movement resolves before combat through the global dependency graph. One Unit
  action cannot both move and attack in the same Tick.
- Combat is simultaneous. A Unit killed during combat still resolves its
  already-locked legal attack.
- Unit healing and the sole Core action resolve after combat. Production cannot
  protect the birth Tick.
- Cell capacity is two occupying entities; hostile entities cannot finish in the
  same cell.
- Population 0-19 has zero upkeep; 20 starts tier 1.
- Current private state is a complete replacement. Enemy Units outside vision
  are unknown; remembered positions are not current targeting facts.

### 2.3 Authoritative operations contract

HTTP `202` is not final per-actor authority when multiple source plans exist for
one Tick. The Agent collects all same-Tick `received` rows through the next Tick
boundary, merges them with MANUAL per-actor/Core precedence, and only then
confirms movement edges, combat submissions, or combat spawn acceptance. A
MANUAL action is never Agent evidence even when its payload is byte-identical.

Core ingress has a stable global fairness queue but a local movement token: the
first queued member currently within `CORE_INGRESS_RADIUS` advances. A remote
queue head may keep approaching, but cannot hold all local carriers indefinitely.
Workers already on Core execute DEPOSIT/HEAL before ingress gating.

Within the local radius the token is priority-aware (`RETURN_CORE` before
`RETURN_SAFE`) while preserving FIFO inside a priority class. A holder that does
not reduce Core distance for four consecutive Ticks yields temporarily so one
Manual-oscillated Unit cannot recreate the deadlock.

Conflicting same-Tick AGENT receipts are non-authoritative: they contaminate the
window and cannot confirm movement edges, combat submissions, spawn acceptance,
population attribution, or resource attribution. Duplicate identical receipts
remain idempotent.

Evaluation eligibility is domain-specific. `economy_metric_eligible` remains false
for an over-cap baseline or any MANUAL receipt and is the only basis for economy
ROI/fallback throughput. `combat_metric_eligible` is evaluated per Tick and is
false for conflicting AGENT receipts, stale submission, MANUAL Core action,
MANUAL action on the bound Vanguard/Ranger, upkeep deficit, friendly death, or
cargo loss. A MANUAL Worker-only action does not contaminate an otherwise
authoritative combat episode, but it never becomes Agent movement or economy
evidence. C12 may use combat eligibility for its 50-Tick/episode gates while
reporting economy ROI as ineligible separately.

The JSONL journal rotates before 32 MiB with bounded backups. Evaluators scan
rotated segments oldest-to-newest and deduplicate server events by `event_id`.
Oversized pre-rotation legacy files are compressed once and remain evaluator-readable.
Normal WebSocket closure reconnects in-process; permanent authentication rejection
remains a stop. Source-directory deployment restores the pre-sync managed tree
and Supervisor configuration if post-sync verification or restart fails.

## 3. Reference-project deep review

### 3.1 Useful patterns to adapt

`Arena-Crazy-Attack` contains several sound structural ideas:

1. Friendly role identity is persistent: home Vanguard, home Ranger, squad
   membership, leader, reinforcement, and pending production confirmation.
2. Ranger geometry correctly supports cardinal/exact-diagonal range 1-3 and
   ignores Unit/Core blockers.
3. Ranger positioning is expressed as legal firing-cell search rather than
   blindly walking onto the enemy.
4. Combat movement uses bounded A* and projected friendly occupancy.
5. Pursuit has explicit duration and cooldown instead of an unbounded target
   lock.
6. Spawn intent is reconciled against later state before assigning the new Unit
   to a squad.
7. A squad has assembly, active, reinforcement, carrier, and recovery states
   rather than a flat priority list.

### 3.2 Patterns explicitly rejected

The following are incompatible with this Agent's current safety and evidence
contract:

- persisted enemy Core positions as navigation or attack targets;
- persisted combat threats used as if still current;
- out-of-vision resource coordinates treated as active Worker tasks;
- proactive enemy-Worker interception;
- enemy-Core hunting and remembered-Core reacquisition;
- dual Beacon expedition, carrier mission, and special-username exclusion logic;
- automatic expansion to 19 without an economic reserve/controller audit;
- target pursuit through visibility loss;
- one 4527-line policy module with only four opening tests.

The reference repository is useful as a pattern library, not as a source drop.
Its tests cover only four production/opening cases and do not prove combat
geometry, movement contention, loss accounting, visibility boundaries, Beacon
lifecycle, or long-running recovery.

## 4. Options considered

### A. Copy the aggressive expedition policy

High activity, but it mixes production, hidden-history tracking, Beacon,
formation, pursuit, and economy in one release. It would destroy attribution and
would be unsafe at the current externally over-cap baseline. Rejected.

### B. Keep static Core-local attacks and only add production

Low implementation risk, but newly produced defenders may remain misplaced and
never create legal attack geometry. It does not form a complete combat system.
Rejected.

### C. Build a local defensive squad state machine

Create at most one home Vanguard and one home Ranger, position them in a bounded
Core defense zone, use current-state target/cell fire, coordinate movement with
economy reservations, and account for every result. This creates real combat
capability while preserving the proven economy and private-view boundaries.
Selected.

## 5. Chosen architecture

```text
authoritative Snapshot
  -> CombatObservation (current enemies only)
  -> FriendlyRoleMemory (friendly IDs only)
  -> CombatProductionController
  -> DefensiveSquadPlanner
       -> threat envelope
       -> target/cell scoring
       -> firing/adjacent-cell search
       -> bounded response movement
  -> shared Tick reservations with Worker traffic
  -> complete plan
  -> next-state events
  -> CombatEpisode + ProductionTransaction + role reconciliation
```

### 5.1 Friendly role state

Add a bounded `CombatMemory` owned by `ExplorationMemory`:

```text
home_vanguard_id
home_ranger_id
pending_spawn: {unit_type, requested_tick, prior_ids}
response_until_tick
response_anchor
last_role_transition
```

Only friendly IDs and current defensive anchors persist. Enemy IDs, enemy
positions, owner names, and target locks do not persist across states.

Role reconciliation runs on every complete state:

- if assigned defender disappeared, clear the role;
- a pending spawn is confirmed only when a new owned Unit of the requested type
  appears and the prior accepted Agent Core action was the matching SPAWN;
- timeout/failure events clear the pending transaction and start a bounded
  retry cooldown;
- choose stable lowest-UUID unassigned defenders when adopting already-existing
  combat Units.

### 5.2 Production controller

Default product target is exactly:

```text
1 home Vanguard + 1 home Ranger
```

No expedition or roaming force is part of V2.

Production priority:

```text
Core-full transaction
> current Core HEAL
> current Core REPAIR_SHIELD
> defensive Vanguard/Ranger spawn
> ordinary Worker spawn
```

A defensive spawn requires all of:

- Core exists and state is `NORMAL`;
- no Core-full transaction or blocked carrying Worker at a full Core;
- Core cell has a legal slot after planned movement;
- no pending spawn transaction or recent spawn failure cooldown;
- population remains `<= 20` after spawn; ordinary production is capped at 19,
  while the sole guarded exception is the missing-Ranger transaction from 19 to 20;
- `upkeep_next_tick == 0` before the transaction; projected upkeep uses the official
  triangular tier formula and reserves 20 future Tick payments in addition to
  `COMBAT_RESERVE = 20`;
- no unexplained current-session population transition;
- spawn order: Vanguard first, then Ranger.

If population 19 is also Core-full and exactly one carrying Worker occupies the Core,
the Ranger transaction is atomic in official resolution order: move that deterministic
leader to a legal adjacent cell and submit Ranger SPAWN in the same Tick. Unit movement
frees the Core slot before Core production; Ranger cost frees storage before the carrier
returns to deposit. Failure to find a legal evacuation cell remains an explicit hold.
Static inventory reserve is not sufficient evidence for entering tier-1 upkeep: the
preceding 20-Tick event ledger must contain at least 20 real `DEPOSIT_SUCCEEDED` events.
Plans, HTTP 202, receipts, and starting inventory do not count as sustainable supply.

`no unexplained current-session population transition` 的实现 hook 固定为：
`__main__.py` 在 `record_population_transition()` 后，将
`source_audit.unattributed_population_increases == 0` 作为本 Tick 的
`combat_production_guard` 传给 policy；policy 单测同时覆盖 guard true/false。
baseline over-cap 本身只污染评价，不阻止合法的已授权生产；未归因人口增长
则阻止战斗生产并写明 `operator_attention` 原因。

The controller may operate on an externally over-cap Worker baseline because
contamination is an evaluation property, not hidden game state. Its production
outcomes remain excluded from clean ROI until a clean session exists.

At population 17, this policy can create exactly the two-unit home guard and stop
at 19. At population 18, it creates only the Vanguard. If an externally over-cap
baseline reaches population 19 with the Vanguard confirmed but no Ranger, it may
perform exactly one Ranger transaction to population 20, provided Ranger cost plus
20 resources of base reserve plus 20 Tick of tier-1 upkeep remain. Population 20
is a hard Combat V2 ceiling and emits operator attention; Worker recovery remains
separately capped by `MAX_EXTERNAL_RECOVERY_POPULATION = 19`.
The completed population-20 squad may still use Ranger fire under the existing current-
visibility, Core-full, cargo-threat, injury, cooldown, geometry, and stage-mode fuses;
population 21+ suppresses Ranger fire and never opens further production.
At population 20, resources `<= COMBAT_RESERVE` activates `COMBAT_UPKEEP_FUSE` before
normal actions: only the bound cargo-free home Ranger submits `SELF_DESTRUCT`, all other
Units WAIT, and no Core action is sent. Unit self-destruction resolves before upkeep,
returning to tier 0 before an unpaid deficit can damage a Worker. `current` and `shadow`
never submit this fuse action.

### 5.3 Defensive zone and states

Use Manhattan geometry around the current stationary Core:

```text
GUARD_RING = 2..4
ENGAGE_RADIUS = 6
RESPONSE_TTL = 6 ticks
```

Squad states:

```text
UNAVAILABLE  no defender of required type
ASSEMBLE     move friendly defender toward an unreserved guard slot
HOLD         guard slot reached, no current threat
RESPOND      current enemy threatens Core or is inside the Core-local engage radius
ATTACK       legal SWEEP/SHOOT selected this Tick
RECOVER      injured defender returns to Core for HEAL
BLOCKED      bounded planner/reservation cannot produce a legal step
```

`RESPONSE_TTL` stores only the fact that friendly defenders are responding and
the Core-relative response anchor. It never stores an enemy ID/coordinate after
visibility disappears. On visibility loss defenders fall back toward the guard
ring; they do not continue pursuit.

### 5.4 Threat classification

Current-state-only priorities:

1. enemy adjacent to owned Core;
2. enemy within `ENGAGE_RADIUS` of Core;
3. currently visible enemy Core inside the Core-local defensive zone;
4. an enemy within 3 of a carrying/injured Worker triggers Worker `RETURN_SAFE`
   and Ranger safety fuses; an empty healthy Worker may become a bounded patrol
   anchor, but a carrier or injured Worker is never an escort anchor;
5. no Core-local defensive threat.

Enemy Unit ownership remains unknown. Target scoring uses only current object
kind, current unit type, current HP if legally visible, distance, attack
geometry, and economic proximity.

### 5.5 Vanguard behavior

```text
if hp < 4 and at Core: HEAL
elif hp < 4: move toward Core through shared reservations
elif current enemy in adjacent cardinal cell: SWEEP best cell
elif current Core-local defensive threat exists: move toward a legal adjacent attack cell
elif outside guard ring: move toward assigned guard slot
else: WAIT
```

Best SWEEP cell maximizes current visible hostile count, then Core/carrying
Worker protection, then deterministic direction order. Empty predictive SWEEP
is deferred from V2 because no clean movement model exists yet.

### 5.6 Ranger behavior and v0.13 cell fire

Ranger decisions use two modes:

1. Precision fire at a currently visible stationary-risk target when its current
   cell is the highest-confidence choice.
2. Target-free cell fire when a visible hostile has a legal one-step movement
   candidate into a high-value protected cell or Vanguard pressure cell.

Candidate cells are only:

- the hostile's current cell;
- its four cardinal next cells that are not current obstacles;
- cells adjacent to the Core or carrying Worker;
- a cell currently pressured by an adjacent friendly Vanguard.

No candidate comes from stale enemy memory. Score:

```text
core threat
+ carrying-worker threat
+ current hostile occupancy
+ Vanguard pressure
+ number of currently visible hostiles that could occupy the cell
- friendly economic congestion
- speculative movement penalty
```

Actions:

```text
if hp < 2 and at Core: HEAL
elif hp < 2: move toward Core
elif high-confidence legal current-cell shot: SHOOT precision
elif legal high-value intercept cell: SHOOT expected_cell without target_id
elif Core-local defensive threat exists: move to the best legal firing cell
elif outside assigned guard slot: move toward guard slot
else: WAIT
```

Target-free fire must be separately journaled as `CELL_INTERCEPT`; precision fire
as `PRECISION_CURRENT`. `SHOT_HIT/SHOT_MISSED` is the only outcome truth.

### 5.7 Movement and reservations

Combat movement must not introduce a second traffic system. Generalize the
existing per-Tick reservation surface so Workers and defenders share:

- reserved destinations;
- reserved reverse edges;
- current occupancy and cell capacity;
- dynamic move-failure TTL;
- bounded path/node budgets.

The authoritative v0.13 stacking rule is capacity two occupying entities per
cell. A friendly Unit may enter a normal cell containing one friendly Unit; a
Core itself consumes one slot, so Core + one Unit is full. Two friendly entities,
any currently visible enemy occupant, and obstacles remain blockers. Shared
reservations track current load minus planned departures plus planned arrivals:
an empty cell may receive two friendly movers, a singly occupied friendly cell
may receive one, and a successfully vacated slot may be used by a dependency
follower. Path search therefore blocks only effectively full/enemy cells, not
every occupied cell. A carrying Worker already sharing the receptive Core cell
submits `DEPOSIT` before Core-ingress queue holds are considered; the queue
serializes movement into the Core, not actions by an already-arrived Unit.

Priority remains:

```text
Core-full actor
> carrying Worker
> injured Worker
> sticky safe retreat
> injured defender recovery
> defender protecting immediate Core/cargo threat
> visible resource Worker
> defender assembly/guard movement
> frontier Worker
```

Combat Units never reserve the Core ingress staging cells ahead of carrying or
retreating Workers. When no legal step exists they explicitly WAIT with a reason;
they do not random-walk.

### 5.8 Combat and production journal

Each plan adds compact fields:

```text
combat_roles
combat_state_by_unit
combat_target_mode
combat_candidate_cell
combat_decision_reason
combat_path_status/path_length/nodes
combat_reservation_holds
production_transaction
```

Episode close adds:

```text
precision_shots
cell_intercept_shots
shots_hit/missed
sweeps/targets_hit
outgoing/incoming_damage
friendly_defender_deaths
friendly_worker_deaths
friendly_cargo_lost
harvests/deposits during episode
excluded_from_evaluation + exclusion_reason
```

No full state, token, enemy history, or owner identity is persisted.

## 6. Failure matrix

| Failure | Required behavior |
|---|---|
| `CORE_SPAWN_FAILED` | clear pending spawn, cooldown, preserve economy |
| new Unit not visible after accepted spawn | wait for event/state confirmation; never assign a guessed ID |
| defender move failure | dynamic edge/cell TTL; preserve role and bounded target |
| target disappears | no pursuit; return to guard state |
| Ranger precision miss | count only `SHOT_MISSED`; do not infer why |
| Ranger cell miss | count as intercept miss; do not infer hidden movement |
| defender damaged | enter RECOVER; Ranger fire fuse remains active |
| defender killed | confirm only after next complete owned state omission |
| Worker cargo dropped | long combat fuse and episode loss |
| population reaches 19 | ordinary production stops; only the guarded, supply-proven missing-Ranger 19-to-20 transaction is allowed |
| population reaches 20 | stop all further production; upkeep fuse may remove only the bound cargo-free Ranger |
| Core full / carrier backlog | combat production and assembly yield to economy transaction |
| MANUAL Worker action | exclude Agent Worker attribution and economy ROI; Combat remains eligible if no bound defender/Core override |
| MANUAL bound defender or Core action | behavior continues from authoritative state; this Tick is Combat-ineligible |
| conflicting same-Tick AGENT receipts | mark the Tick non-authoritative; confirm no movement/combat/spawn/population/resource attribution |
| duplicate identical AGENT receipts | idempotent; retain one authoritative plan |
| path node cap | explicit bounded hold/backoff; no unbounded search |

## 7. File-level implementation plan

| File | Change |
|---|---|
| `arena_agent/model.py` | retain legal enemy HP/Core fields needed for current-state scoring; no owner history |
| `arena_agent/combat.py` | threat scoring, SWEEP-cell scoring, v0.13 cell fire, firing-cell search |
| `arena_agent/policy.py` | `CombatMemory`, role/spawn FSM, defender intents, shared reservations |
| `arena_agent/__main__.py` | production attribution and compact combat decision journal |
| `tests/test_policy.py` | sequence and integration regressions |
| `README.md` | current capability and live-mode description |
| `docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md` | make current V2 status authoritative |
| Obsidian project record | rollout evidence, contamination, rollback, unverified gates |

No new daemon, browser dependency, SDK migration, Beacon logic, Core migration,
or unrelated service change.

## 8. Test matrix

The repository suite is larger than this contract matrix. These are the
production-critical behaviors that must remain explicitly covered.

### Production and upkeep

1. population 17 can produce Vanguard then Ranger and stop at 19;
2. population 18 produces only the missing defender;
3. population 19 cannot run ordinary production;
4. population 19 may run exactly one missing-Ranger transaction only after the
   real 20-deposit/20-Tick supply gate and reserve checks;
5. population 20 requests no further production; population 21 is rejected;
6. population-20 reserve pressure self-destructs only the bound cargo-free Ranger
   before upkeep, never a Worker/Vanguard;
7. Core-full/carrier backlog/Core heal/shield repair beats combat spawn;
8. accepted SPAWN without event and next-state Unit never fabricates a role;
9. spawn success/failure/event/state sequence reconciles exactly once.

### Combat and visibility

10. adjacent multi-hostile cell selects deterministic Vanguard SWEEP;
11. only Core-local current threats produce bounded defender response movement;
12. Only a current enemy threat may create an empty healthy Worker patrol anchor;
    the anchor is bounded by lease, distance, and return-to-Core conditions;
13. visibility loss returns defenders to guard ring with no enemy history;
14. injured defender returns and HEALs; fatal candidate never heals;
15. current legal Ranger target produces precision payload;
16. optional-target v0.13 intercept omits `target_id`;
17. intermediate obstacle blocks fire; Units/Cores do not;
18. local and distant Ranger gates are independent;
19. hit/miss events update precision/intercept counters exactly once.

### Traffic, source authority, and observability

20. carrying Worker reservation beats defender movement;
21. v0.13 capacity ledger allows two friendly entities but blocks Core+Unit/full
    cells and current enemy occupants;
22. two same-Tick movers can fill an empty two-slot cell;
23. remote ingress head cannot block the first local carrier;
24. local carrier outranks safe-retreat; same-class FIFO remains stable;
25. local token with no Core-distance progress yields after four Ticks;
26. dynamic move failure preserves target/role and uses bounded TTL;
27. bounded search returns explicit hold at node cap;
28. same-Tick AGENT/MANUAL receipts merge at the Tick boundary independent of
    arrival order; byte-identical MANUAL action is never Agent evidence;
29. conflicting AGENT receipts are non-authoritative; identical duplicates are
    idempotent;
30. Manual Worker contaminates economy only; Manual bound defender/Core
    contaminates Combat;
31. journal rotation, legacy gzip, cross-segment evaluator, and `event_id` dedupe
    preserve counts;
32. WebSocket normal close backs off/reconnects, resource changes close numerically,
    enemy disappearance leaves no target memory, and every owned Unit has an
    explicit action.

## 9. Rollout and acceptance

### Stage 0: local only

- tests first;
- `python3 -m unittest discover -s tests -p 'test_*.py'`;
- `python3 -m compileall -q arena_agent`;
- `bash -n deploy/pxed-deploy.sh`;
- `git diff --check`;
- self-review against official v0.13 action schemas.

### Stage 1: shadow decision journal

Compute roles, production choice, movement, and fire choice but keep current live
combat actions. Require at least 30 resolved Ticks with bounded path work,
complete actions, and no economy reservation regression.

### Stage 2: guarded production

Enable only the two-defender production FSM. Acceptance requires:

```text
submitted SPAWN
-> HTTP 202
-> received AGENT plan
-> CORE_SPAWN_SUCCEEDED
-> next complete state contains the new owned type
-> role assignment journal
```

### Stage 3: positioning

Enable defender assembly/guard movement. Require event-level movement success,
no repeated ingress contention, stable RSS, and no deposit regression caused by
defender reservations.

### Stage 4: live attacks

Enable attacks serially: Vanguard adjacent SWEEP first, then Ranger precision
current-cell fire, then target-free cell intercept only after a clean precision
episode closes. Each stage requires a bounded combat episode with submitted action,
next-state hit/miss/sweep result, damage, loss accounting, and eight-Tick close.

### Maturity labels

- `implemented`: local tests only;
- `shadow-verified`: live decisions, no changed actions;
- `production-verified`: spawn event and owned state confirmed;
- `positioning-verified`: movement events and economy noninterference confirmed;
- `combat-event-verified`: real attack resolution observed;
- `strategy-quality-verified`: three Combat-clean episodes, all required attack
  coverage, at least 50 Combat-eligible resolved Tick, and acceptable loss/upkeep
  outcomes. Economy ROI is reported separately.

A Combat-contaminated episode can reach `combat-event-verified`, never
`strategy-quality-verified`. An economy-contaminated but Combat-clean episode may
participate in Combat strategy evaluation; it never makes economy ROI eligible.

## 10. Rollback

Rollback scope is only the V2 Arena files and `arena-hero-agent` process.

Trigger rollback or stop Arena on:

- repeated non-handoff command errors;
- spawn retry storm, population crossing 20, or any production request at population 20+;
- repeated defender/Worker reservation deadlock;
- unbounded path work or rising RSS;
- cargo/deposit regression attributable to defender movement;
- target action schema rejection;
- friendly losses without correct cooldown/accounting.

Restore the previous verified commit, restart only `arena-hero-agent`, then verify
process, same-session `202/received`, and subsequent economy events. Never stop
global Supervisor, Chrome, or unrelated services.

## 11. 完备开发任务清单

本节是执行顺序和完成定义的唯一任务清单。任务必须按依赖顺序推进；同一
任务包内可以并行写纯函数和测试，但不得跨包提前改变线上行为。

### 11.1 任务状态和依赖

```text
C0 文档/基线冻结
  ├─ C1 官方状态/事件字段补齐
  ├─ C2 战斗纯函数与 v0.13 cell-fire
  └─ C3 CombatMemory 与角色生命周期
       ├─ C4 生产事务控制器
       └─ C5 防区 slot / 战斗 planner
            └─ C6 Worker/Combat 共享 reservation
                 └─ C7 journal 与 episode attribution
                      └─ C8 shadow mode
                           └─ C9 guarded production
                                └─ C10 positioning
                                     └─ C11 live combat events
                                          └─ C12 clean strategy evaluation
```

C0-C7 是代码和本地验证任务；C8-C11 是分阶段线上任务；C12使用
`combat_metric_eligible=true`作为Combat Tick门，并独立报告
`economy_metric_eligible`和经济ROI。C9-C11不得合并为一次线上发布。

### 11.2 C0：基线冻结与实现合同

前置：无。

工作：

- 固定实现基线 commit、历史89项测试结果和Arena历史session证据；当前实现
  以Section 2.1的`7ff47cc`/149项测试为准；
- 明确 `CombatMemory`、`Plan`、journal 字段和 task state 的向后兼容规则；
- 为每个行为常量建立单一来源，禁止在 `policy.py`、`combat.py`、测试中
  分散复制；
- 创建任务批次的变更 allow-list，冻结 allocator/path/认证/WS loop。

完成定义：

- 文档中每个后续任务都有唯一输入、输出、测试和线上门；
- 任何未列入 allow-list 的文件不进入 V2 行为批次；
- 基线 suite、compile、shell syntax、diff check 全部通过。

产物：本文件更新；不改运行代码。

### 11.3 C1：官方字段和当前视野模型

前置：C0。

允许文件：`arena_agent/model.py`、`tests/test_policy.py`。

工作：

- `VisibleEnemy` 保留当前 state 合法可用的 `hp`（若该版本 state 提供）、
  `unit_type`、`kind`；不增加 owner、历史位置或 last-seen 字段；
- 明确 enemy Core 的 `owner_username` 仅用于当前展示/分类，不参与持久目标；
- 补充 state replacement、缺失字段、enemy HP 可见性和下一 state enemy
  消失测试；
- 保持 owned Unit HP、Core HP/shield/state/upkeep 的现有语义。

禁止：任何敌方记忆、浏览器/Beacon 数据接入、state 合并。

完成定义：

- C1 测试证明 state replacement 不保留上一帧 enemy ID/position；C3 再证明
  `CombatMemory` 也不产生 enemy ID/position 持久字段；
- 旧 89 项测试不回归；
- `VisibleEnemy` 的新增字段不影响旧 fixture。

### 11.4 C2：战斗纯函数和 v0.13 射击

前置：C0；C1 的字段定义可先用 fixture 完成。

允许文件：`arena_agent/combat.py`、`tests/test_policy.py`。

工作：

- 保留并自检 cardinal/exact-45/range 1-3/intermediate obstacle 几何；
- 新增 target-free `{"type":"SHOOT","expected_cell":[x,y]}` 构造；
- 保留 precision `target_id` 构造，二者必须显式区分；
- 新增候选 cell 生成、确定性评分和合法 firing-cell 搜索；
- 纯函数只接收当前 Snapshot、当前 obstacles、当前友军 occupancy，不能
  读取或写入敌方历史。

完成定义：

- 覆盖 `(3,3)` 合法、`(2,1)` 非法、旁边障碍不挡、线上中间障碍挡；
- 覆盖 Unit/Core 不挡射击；
- 覆盖 precision 与 cell-shot payload 无多余字段；
- 覆盖空 cell、目标移动、目标消失只由后续 `SHOT_MISSED` 归因；
- 纯函数测试确定性运行 1000 次无状态增长。

### 11.5 C3：CombatMemory、角色和死亡生命周期

前置：C1、C2。

允许文件：`arena_agent/policy.py`、`tests/test_policy.py`。

工作：

- 在 `ExplorationMemory` 下新增有界 `CombatMemory`；
- 实现 home Vanguard/home Ranger 角色认领、稳定 UUID 选择和角色清理；
- 实现 `pending_spawn`：requested tick、unit type、prior owned IDs、
  bounded timeout、failure cooldown；
- 只保存 Core-relative response anchor/response TTL，不保存 enemy target；
- 接入已有 EventLedger 的友军死亡确认、cargo drop 和 combat fuse。

完成定义：

- 已有 combat Unit 可被确定性认领；
- owned Unit 缺失只在下一完整 state 确认死亡；
- pending spawn 未出现新 Unit 时不能伪造角色；
- event_id 重复不会重复计数或重复改变角色；
- 1000 synthetic ticks 后 memory/event/role 容器均有界。

### 11.6 C4：防守生产事务控制器

前置：C3。

允许文件：`arena_agent/policy.py`、`arena_agent/__main__.py`、
`tests/test_policy.py`。

工作：

- 实现缺口顺序：Vanguard → Ranger；
- 统一检查 Core NORMAL、Core slot、Core-full、current Core defense action、
  pending spawn、cooldown、resources-after-cost >= 20 和 upkeep；
- 普通生产要求 after-spawn population <= 19；唯一例外是已绑定 Vanguard、
  缺失 Ranger、最近20 Tick有20次真实deposit且成本/基础reserve/20 Tick tier-1
  upkeep均满足时的population 19-to-20 Ranger事务；
- 生产只提交 Core action，不在同 Tick 猜测出生 Unit；
- 接入 `CORE_SPAWN_SUCCEEDED`、`CORE_SPAWN_FAILED` 和 next-state owned Unit
  reconciliation；
- population 20写`operator_attention`并禁止任何后续生产；资源触及基础reserve时
  仅绑定且无cargo的home Ranger可在upkeep前触发`COMBAT_UPKEEP_FUSE`。

完成定义：

- population 17可按两Tick完成V/R并停在19；
- population 18只生产缺口单位；population 19只允许上述唯一Ranger事务；
- population 20不再请求生产，population 21视为硬异常；
- Core-full、Core heal、shield repair、carrier backlog优先于战斗生产；
- failed spawn不产生retry storm；
- 生产journal能区分requested/accepted/resolved/confirmed/failed。

### 11.7 C5：防区 slot 和战斗 planner

前置：C2、C3；C4 可用测试 fake role。

允许文件：`arena_agent/combat.py`、`arena_agent/policy.py`、
`tests/test_policy.py`。

工作：

- 实现 Core-relative guard slots，范围 `MANHATTAN 2..4`；
- 实现 `UNAVAILABLE/ASSEMBLE/HOLD/RESPOND/ATTACK/RECOVER/BLOCKED` 状态；
- Vanguard：受伤回 Core，邻接目标 SWEEP，远端只走向合法攻击邻格；
- Ranger：受伤回 Core，优先合法 current-cell precision，再选 target-free
  cell intercept，再移动到 firing cell；
- visibility loss 后回防区，不追击、不保留敌方坐标。

完成定义：

- 两个 defender 不占同一 slot；
- local 与 distant Ranger gate 独立；
- carrying/injured Worker threat 优先级压过 speculative fire；
- 无敌人时固定 state 的 Worker action 字节级不变；
- 当前敌人消失后最多在规定 TTL 内回到 guard state。

### 11.8 C6：Worker/Combat 共享 reservation

前置：C4、C5。

允许文件：`arena_agent/policy.py`、`arena_agent/path.py`（仅共享接口所需最小改动）、
`tests/test_policy.py`。

工作：

- 将 defender movement 接入现有 per-Tick destination/reverse-edge/cell
  capacity reservation；
- 固定优先级：Core-full、carrying、injured Worker、safe retreat、injured
  defender、即时 Core/cargo threat defender、resource、assembly、frontier；
- Combat Unit 不抢 Core ingress staging；
- move failure 继续只进入 dynamic TTL；node cap 输出显式 BLOCKED/WAIT。

完成定义：

- carrying Worker reservation 永远胜过 defender assembly；
- defender 不造成 Core ingress leader 的 destination conflict；
- 两个 defender 无同目的格 reservation；
- 动态失败后保留 role、切换候选或显式 hold；
- 固定经济 fixture 的 plan 与旧版字节级等价；
- 1000 synthetic ticks memory/RSS/path work 有界。

### 11.9 C7：journal 和 episode attribution

前置：C3-C6。

允许文件：`arena_agent/__main__.py`、`arena_agent/journal.py`、
`arena_agent/policy.py`、`tests/test_policy.py`。

工作：

- 写入 compact combat role/state/target mode/candidate/reason/path/reservation；
- production transaction 记录 requested tick、HTTP result、received、event
  result、confirmed Unit；
- episode 记录 precision/cell-intercept/shots/sweeps/damage/deaths/cargo 和
  episode 内 harvest/deposit；
- 复用 `economy_metric_eligible`、`combat_metric_eligible` 和 episode attribution；
  经济污染不自动污染Combat，Combat污染/stale/safety loss关闭或排除Combat episode；
- console/journal 不写 full objects、token、owner identity、enemy history。

完成定义：

- 每个 owned Unit 仍有 explicit action；
- event_id 去重；
- `202/received` 不增加 hit、damage、spawn-confirmed 或收益；
- open episode 在 session end 标 incomplete；
- clean/contaminated episode 分母互不混用。

### 11.10 C8：Shadow decision journal

前置：C7、全部 Stage 0 本地门通过。

行为：

- 计算 V2 role/production/position/fire decision；
- live plan 仍使用当前已验证动作；
- 只新增 compact shadow 字段，不提交 V2 spawn/move/attack。

线上门：

- Arena-only staged deploy；
- 同 session `tick/state → 202 → received`；
- 至少 30 个 resolved Tick；
- no reservation conflict regression、no repeated same-edge failure、RSS
  stable；
- `shadow_decisions` 与实际 action 分开记录。

失败：只回滚 Arena 当前批次，不重启全局 Supervisor/Chrome。

### 11.11 C9：Guarded production

前置：C8 通过；明确授权改变 Core spawn 行为。

只开放 C4 生产，不开放 defender movement 或 attacks。

线上门：

```text
requested SPAWN
→ 202
→ received source=AGENT
→ CORE_SPAWN_SUCCEEDED/FAILED
→ next state owned Unit confirmation
→ role journal
```

硬停止条件：population > 20、population 20 后再次请求生产、重复 spawn、
Core-full/deposit regression、非预期 Unit type、pending transaction 无界增长。

### 11.12 C10：Positioning

前置：C9 至少一次成功 production confirmation，无 spawn retry storm。

只开放 C5/C6 的 defender assembly/guard movement；攻击仍关闭。

线上门：

- 30 resolved Tick 内 defender movement event 可解释；
- Core ingress/deposit throughput 无归因下降；
- defender slot 不振荡；
- dynamic failure 有 TTL、有替代动作或显式 hold；
- combat Unit 不进入 Worker ingress staging。
- friendly patrol anchor 只保存 owned Worker ID，租约最多120 Tick；carrier、
  injured、Core arrival、defender离开Core guard或租约到期均释放，并要求守卫
  回到Core guard后才能绑定下一anchor。

### 11.13 C11：Live attacks

前置：C10通过；存在自然当前可见合法目标（Core-local或当前patrol anchor
防区）；明确授权改变战斗 action。

按顺序开放：

1. Vanguard adjacent SWEEP；
2. Ranger precision current-cell SHOOT；
3. Ranger target-free `CELL_INTERCEPT`。

The production Supervisor may remain configured as `live-precision`: after a real
precision submission resolves and its episode closes normally, the same process changes
its effective mode to `live-cell` on the next Tick. The journal records both
`configured_mode` and effective `mode`. Passive-damage episodes, open episodes, and
`INCOMPLETE` session-end episodes never advance the gate; restart conservatively returns
to configured precision until a new closed precision episode exists in memory.

每一项至少完成一个 bounded episode：

```text
submitted action
→ next-state SWEEP_RESOLVED/SHOT_HIT/SHOT_MISSED
→ damage/death/cargo accounting
→ safety fuse
→ 8 quiet ticks
→ episode close
```

任何 miss 只记录 miss，不推断服务端失败原因。没有自然目标时保持 WAIT，
不能为了验收制造攻击或读取迷雾。

### 11.14 C12：Clean strategy evaluation

前置：C11至少一个事件级Combat episode；`combat_metric_eligible=true`用于Tick
门，且不存在Combat污染的基线/窗口。经济ROI可以仍为ineligible，不阻断Combat
strategy evaluation。

门槛：

- 至少 3 个 clean complete combat episodes；
- 无 confirmed friendly death，或有可解释且可接受的损失对照；
- cargo loss=0，或有明确经济补偿证据；
- episode内真实harvest/deposit作为伴随事实；只有
  `economy_metric_eligible=true`时才用于经济非干扰/ROI判断，经济ineligible
  不得通过或否决Combat质量；
- cooldown 真实触发并解除；
- population/upkeep/Core-full 无风险；
- 至少50个`combat_metric_eligible=true`的resolved Tick后才允许评审参数调整。

C12 之前禁止调整 combat radius、production reserve、frontier、Worker cap、
attack score 或追击策略。

## 12. 提交和发布边界

建议按以下提交拆分，避免行为、日志和部署混在一个不可回滚批次：

```text
D0 docs: combat v2 execution contract
D1 test: model and v0.13 cell-fire contract
D2 feat: combat pure functions and cell-fire payloads
D3 test/feat: combat role lifecycle and spawn transaction
D4 test/feat: defensive planner and guard slots
D5 test/feat: shared worker-combat reservations
D6 feat: combat journal and episode attribution
D7 feat: shadow decision mode
D8 release: guarded production
D9 release: defender positioning
D10 release: live attack actions
```

D1-D6 每个批次都必须本地绿色；D7 之后才允许线上。D8-D10 必须分别可回滚。
不要把 `requirements.txt`、Chrome、Beacon、Core migration、全局 Supervisor
配置或无关仓库带入这些提交。

## 13. 任务交付证据总表

| 任务 | 本地必须提供 | 线上必须提供 | 可否改变线上动作 |
|---|---|---|---|
| C0-C2 / D1-D2 | tests、compile、schema/payload probe、diff check | 无 | 否 |
| C3-C7 / D3-D6 | sequence tests、bounded-memory probe、journal redaction probe | 无 | 否 |
| C8 / D7 | shadow-vs-actual regression、journal schema probe | 30 resolved Tick、同 session 202/received、RSS/traffic | 否 |
| C9 / D8 | production FSM full matrix | SPAWN→event→owned Unit confirmation | 仅 Core SPAWN |
| C10 / D9 | slot/reservation/noninterference matrix | 30 resolved Tick movement/economy evidence | 仅 defender MOVE |
| C11 / D10 | attack payload/event attribution matrix | SWEEP、precision、cell-intercept 各自事件链 | 仅明确授权的攻击动作 |
| C12 | Combat/economy domain aggregation/replay | 3 Combat-clean episodes + 50 Combat-eligible resolved Tick；经济ROI单列 | 不做参数调整，先评估 |

任何一行的“必须提供”缺失，都只能标记为未完成，不能用更高层的证据替代。

## 14. 完成定义和最终交付

Combat V2 只有同时满足以下条件才能标记为完成：

- C0-C7 全部本地任务通过；
- C8 shadow 30 Tick 通过；
- C9 production-confirmed；
- C10 positioning-verified；
- C11 combat-event-verified，至少有真实 SWEEP/precision/cell-fire 事件链；
- C12 strategy-quality-verified，满足3个Combat-clean episode、三类攻击覆盖和
  50个Combat-eligible Tick；经济ROI结论独立；
- Obsidian 项目记录写明 commit、session、cutoff、实际事件、污染状态、
  rollback 和未验收项；
- 进程、协议、游戏结果、策略质量四类结论分开；
- 没有把代码测试、202/received、单次 live smoke 冒充最终战斗成功。

## 15. Implementation approval boundary

### 15.1 Archived historical implementation status (superseded)

The entries below are retained as rollout provenance only. They are not the
current repository, deployment, or acceptance status; use Section 2.1 and the
latest Obsidian record as the current authority.

- C0-C7 / D1-D6 已完成本地实现与回归：当前视野模型、v0.13 precision/cell payload、
  CombatMemory、accepted→resolved→confirmed spawn transaction、Core-relative guard/firing
  cells、Worker/Combat 共享 reservation、compact journal/episode attribution；
- 本地证据：113 项 `unittest`、`compileall`、deploy shell syntax、`git diff --check`、
  1000 次纯函数确定性和 1000 synthetic Tick bounded-memory probe 通过；
- 当前默认部署 mode 为 `current`；独立 D7 已于 cutoff `2026-08-05T07:51:47+08:00`
  运行 `--combat-mode shadow`，Arena Supervisor `RUNNING`，PID `48599`，RSS 末次约
  `21.0 MB`；
- C8 `shadow-verified`：同一 session `c9187bb3ace1` 至少 44 个 resolved Tick，
  每 Tick `state → HTTP 202 → received`，plan action 与 received action 全部相等；
  mode 全为 `shadow`，combat submission=0，最大重复边失败=0，dynamic edge/cell=0，
  最大 reservation=17；
- C8 线上历史样本的 baseline 为 population=19、17 Worker，所有 Tick
  `economy_metric_eligible=false`；因此不能推出经济ROI；该历史样本没有己方
  Vanguard/Ranger，combat proposal coverage=0，属于未观测，不是战斗成功；
- C9 `production-verified`：session `a2d4d29bf27f` 于 Tick `54712` 仅提交一次
  Vanguard SPAWN，完成 `202 → received → CORE_SPAWN_SUCCEEDED → population=19 →
  owned Vanguard → home_vanguard_id → transaction CONFIRMED`；后续 10 Tick 无重复 SPAWN；
- C10 `positioning-verified`：session `3952d46b91d9` 的 Tick `54731-54760` 共
  30 resolved Tick；Vanguard 仅移动 2 次后稳定 HOLD，样本内 477 次
  `UNIT_MOVE_SUCCEEDED`、5 次 `DEPOSIT_SUCCEEDED`、2 次 `HARVEST_SUCCEEDED`，
  attack=0、spawn=0、repeated failure=0、最大 reservation=17；
- C11 已开放到 `live-precision`：session `454372837399` 的旧样本没有自然相邻
  SWEEP；随后 session `a4fa3ca1eb3a` 完成唯一 Ranger 原子生产事务，但 tier-1
  upkeep 供给不足，资源从 83 降至 0，Tick `55891-55892` 连续出现
  `UPKEEP_PAID deficit=1`，远端 Worker `8cd0622d-...` HP `1→0`，population
  自然回到 19。Ranger仍存活且role已绑定；该窗口确认一名friendly death，严禁作为
  clean combat/ROI证据。production已新增20-Tick真实deposit供给门和upkeep fuse；
  commit `03fbf93` 以 configured `live-precision` 运行，session `47ded8a978dc`
  已至少80 resolved Tick、资源恢复至37、无新死亡/cargo loss/deficit。当前仍未遇到
  Core防区内自然合法precision射线；正常closed precision episode后effective mode会
  自动晋级`live-cell`，但截至该cutoff尚无SHOOT/SWEEP事件链；
- C12 已增加可复现离线 evaluator `scripts/evaluate-combat-v2.py`。当前 session 实测
  resolved Tick 已超过 250，但 `eligible_resolved_ticks=0`、clean episodes=0，且缺
  sweep/precision/cell event coverage，因此 `strategy_quality_ready=false`；没有自然合法
  目标或 clean baseline 时不能制造攻击、死亡或样本；

用户已于 2026-08-05 明确授权继续完成剩余任务；该授权不取消 C9-C11 的串行阶段门、
硬停止条件和独立回滚要求。任何阶段缺少自然合法目标、clean baseline 或真实事件时，
只能保留在最后一个已验证阶段，不能用 synthetic/人为行为替代线上证据。
