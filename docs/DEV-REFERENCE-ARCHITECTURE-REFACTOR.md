# Arena Hero 参考成熟 Agent 的架构重构设计

状态：已上线并完成代码 review；资源全局匹配已通过 50 Tick 线上验收。
日期：2026-08-03
参考实现：`https://github.com/VelvetEvening/Arena-Crazy-Attack`，审查提交 `88083db7fb9ba6b21b5918498921bcfeb4bd7719`。
当前规则主契约：`docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md`。

## 1. 目标与范围

目标不是移植第三方的激进玩法，而是吸收其成熟的工程结构和全局分配算法，使当前 Agent 在不破坏已验证经济/交通闭环的前提下获得：

```text
可单测的领域模块
+ 可解释的每 Tick 规划上下文
+ 当前可见资源的全局最小成本唯一分配
+ 事件确认驱动的内存更新
+ 有界且可观察的长期运行状态
```

本批只覆盖 Worker 经济、Core 事务、交通和可观测性。明确不做：

```text
Vanguard pursuit / 自动生产 Vanguard 或 Ranger
Ranger SHOOT
Beacon 拾取、远征、护送
Core migration
敌方 Core/Unit 的历史目标追逐
自动 population > 8
浏览器、DOM、隐藏地图或第三方 state 来源
```

## 2. 第三方方案审视

### 可吸收的设计

| 第三方能力 | 可吸收价值 | 本项目改造 |
|---|---|---|
| `PlanningContext` | 每 Tick 共享事实与 reservation 集中在一个对象，减少函数间隐式耦合 | 新建不可变 `PlanningContext`，输入为当前 `Snapshot` 与当前 Tick memory 视图 |
| Hungarian 最小成本匹配 | 多 Worker 与多资源时避免按 Worker 贪心造成全局长路径 | 仅匹配当前 `state.resource_cells`、空载且非受伤 Worker；成本使用 bounded path length |
| `FriendlyOccupancy` | 显式建模本 Tick 预计占位 | 复用当前 destination/edge reservation；不复制其“cell count 可直接预判 resolution”假设 |
| 角色状态对象 | 将远征/追击等长期状态从巨大决策函数拆开 | 只建立 `CoreTransactionState`、`TrafficMemory`、`ResourceAllocator`；不引入 expedition 状态 |
| 原子持久化 | 防止进程重启后状态半写入 | 当前 live memory 保持 session 内；仅当需要跨重启的低风险配置/统计时单独设计持久化，不能持久化当前可见资源或敌方事实 |
| A*/射线 helper 与测试 | 算法边界有独立函数与单测 | 保留当前 bounded BFS 作为唯一移动规划器；Ranger 几何仅记录为未来独立战斗模块设计素材 |

### 不可直接采用的设计

| 第三方行为 | 与本项目/官方契约冲突或风险 | 处理 |
|---|---|---|
| 长期 `known_resources` 作为 Worker 任务 | 当前 `state` 缺失资源不能保留为可采集事实 | 禁止；资源 observation 只能 TTL 分析，分配只读当前可见资源 |
| 追逐历史 enemy core / target | 可见性替换后会越界地把历史对象当当前目标 | 禁止 |
| 自动人口到 19 | 当前 Agent cap=8 的 traffic/经济收益尚未完成评审 | 禁止 |
| 用户名专属禁区 | 非通用策略、无当前产品依据 | 禁止 |
| 两支远征、Beacon 强攻、Ranger 编队 | 无当前 Agent 的 event-level 闭环，和经济重构风险混杂 | 拆为未来独立 milestone |
| 单文件 `arena_core_agent.py` | 约 4k 行状态、交通、战斗、部署混在一处，难以审计当前规则变化 | 不复制；采用小模块边界 |
| SDK/Turn 运行循环 | 当前 Agent 的 WS/HTTP/idempotency/journal 已实测稳定 | 不替换 transport |

## 3. 现状问题

当前 `arena_agent/policy.py` 同时承担：

```text
EventLedger
+ resource/frontier memory
+ traffic memory
+ bounded path search
+ resource greedy assignment
+ Core full transaction
+ Worker/Core/Vanguard action selection
```

这导致三类风险：

1. 贪心资源认领按 Worker 顺序决策；多资源/多 Worker 时不能保证最小总路径。
2. 交通、经济、Core 事务状态边界只靠函数顺序维持，新增策略易覆盖已有 action。
3. 算法、记忆和 policy 输出不能独立 characterization，重构时容易破坏已验证 Core/traffic 语义。

## 4. 方案对比

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 直接移植第三方单文件 Agent | 快速得到匹配/战斗/持久化能力 | 违反 visibility、改变 transport、混入激进策略、无法小范围回滚 | 拒绝 |
| 保持当前单文件，仅加入 Hungarian helper | 改动最小 | policy 继续膨胀，Core/traffic/资源耦合更深 | 拒绝 |
| 分阶段模块化 + 有界最小成本分配 | 保持现有协议/交通/事务，算法独立可测，可逐步上线 | 需要先建立 characterization 测试与迁移层 | 采用 |

## 5. 目标架构

```text
WS state + events
        │
        ▼
model.py ──────► Snapshot（当前 private state，仅事实）
        │
        ▼
memory.py ─────► EventLedger / ExplorationMemory / CoreTransactionMemory
        │
        ├──────────────► path.py（bounded BFS，纯函数）
        ├──────────────► traffic.py（TTL blocker, reservations, ingress staging）
        └──────────────► allocator.py（当前可见资源全局最小成本匹配）
                              │
                              ▼
                       policy.py（优先级与 Plan 组装）
                              │
                              ▼
                  __main__.py（WS/HTTP/journal；不含策略）
```

### 文件责任

| 文件 | 负责 | 禁止负责 |
|---|---|---|
| `model.py` | parse immutable current `Snapshot`、Core/Unit/current visible object 字段 | memory、目标推断、网络 |
| `path.py` | `PathResult`、bounded BFS、单步替代路径 | action、资源分配、日志 |
| `memory.py` | event id 去重、TTL 资源观察、Core transaction、经济/风险账本 | HTTP、动作输出 |
| `traffic.py` | TTL failed edge/cell、destination/edge reservation、ingress staging | permanent map memory、生产判断 |
| `allocator.py` | current visible resources 与 eligible Worker 的匹配矩阵、最小成本分配 | 使用历史资源、发送动作 |
| `policy.py` | priority state machine、调用各模块、唯一 `Plan` 输出 | socket/curl/journal 写入 |
| `metrics.py` | 从 state/events 汇总窗口指标和归因标签 | 改变 policy |
| `__main__.py` | protocol、idempotency、summary journal | 规划细节 |

## 6. Worker 资源全局匹配

### 输入边界

```text
eligible workers:
  current Worker
  cargo == 0
  hp > 1 or hp unknown
  no Core full transaction
  not assigned to Core return / Unit HEAL

resources:
  current state.resource_cells only
  visible in this Snapshot only
  not obstacle / not reserved by higher-priority task
```

### 成本

对每个 `(worker, resource)` 运行现有 bounded BFS：

```text
if PathResult.status in {FOUND, START_AT_GOAL}:
  cost = path_length * 100
       + worker_recent_load * 15
       + role_radius_penalty
       + deterministic_tiebreak(worker_uuid, resource)
else:
  edge absent
```

- 不以曼哈顿距离取代真实路径长度。
- path node cap/no path 的 pair 不进入矩阵。
- role radius仅是 penalty/eligibility，不突破现有近中远角色边界。
- 矩阵最大为 `8 Worker × current visible resources`；资源多时先保留每 Worker 最多 16 个最低 path cost 候选，总上限 128 条边。
- 使用 deterministic Hungarian/min-cost assignment；输出一对一分配。

### 行为保证

```text
cargo / Core transaction / return HEAL 优先于 allocator
同 resource 不会被两名 Worker 计划 HARVEST
无可达 pair → 回到 bounded frontier，不等待历史资源
allocator 不保留上 Tick assignment 为事实
```

## 7. Tick 规划顺序

```text
1. parse current Snapshot
2. memory.observe + apply de-duplicated previous resolution events
3. build immutable PlanningContext
4. Core full transaction (exclusive)
5. carrying return / deposit and injured return / Unit HEAL
6. resource allocator for remaining eligible Workers
7. bounded frontier for remaining Workers
8. traffic reservation/ingress coordination on desired moves
9. legal current-visible Vanguard guard action
10. choose at most one Core action:
    Core full recovery > Core HEAL/REPAIR > conservative Worker spawn
11. emit complete Plan
```

No layer may mutate current `Snapshot`; action collisions must resolve through the traffic layer, never later dictionary overwrite.

## 8. Characterization and Test Plan

### Phase A: freeze behavior before extraction

Add plan-output tests covering:

```text
Core full: EVICT / RECOVERY / cooldown / hard cap
Core ingress: far staging / near hold / queue advance
Dynamic move failure: failed edge is not immediately repeated
Worker hp=1: carrying deposit first; empty return and Unit HEAL
Core hp/shield: HEAL/REPAIR only under legal gated state
Vanguard: only current visible adjacent SWEEP
```

No behavior changes in this phase. Every test must execute current policy output before code relocation.

### Phase B: extract pure modules

Move path, traffic, event/memory classes without output changes. Run both current suite and a new corpus of fixed synthetic state sequences. Acceptance: identical serialized `Plan.as_dict()` for corpus.

### Phase C: allocator behavior change

Add tests that fail under greedy order:

```text
W1→R1 = 1, W1→R2 = 2
W2→R1 = 2, W2→R2 = 100
expected global match: W1→R2, W2→R1 (total 4)
not greedy W1→R1, W2→R2 (total 101)
```

Also test resource visibility replacement, unreachable edges, 8-worker candidate bound, reservation handoff, and deterministic repeat output.

### Phase D: metrics extraction

Move summary-only accounting into `metrics.py`; keep journal schema backward-compatible, add `allocator_pairs`, `allocator_total_cost`, `allocation_unmatched`, and reasoned skips. No policy constants change.

## 9. Deployment Gates

Each phase is one commit and one deployment at most:

```text
local characterization + unit tests + compileall + shell syntax
→ GitHub CI
→ pxed staged tests + compile
→ Arena-only restart
→ fresh session 202/received
→ next-state event validation
→ 30 Tick smoke
→ 50 resolved Tick window
```

Phase A/B live acceptance: exact protocol/action/economy non-regression.

Phase C live acceptance:

```text
no increase in UNIT_MOVE_FAILED rate
no duplicate same-resource HARVEST contention
harvest-to-deposit median does not regress
path length / nodes remain bounded
no Core transaction or ingress regression
```

Rollback: only files of the active phase; restart only `arena-hero-agent`. Never stop global Supervisor/Chrome/unrelated services.

## 9.1 上线与代码 review 结果

上线链路：`92a9c14`（模块抽取与 allocator）→ `373ca5e`（显式 128-edge 上界）→ `880e8a7`（review 修复）。

```text
local: 55 tests + compileall + deploy shell PASS
allocator: 1-4 Worker / 1-4 resource 随机矩阵与穷举最小成本一致
CI: PASS
pxed staged: 54 / 55 tests + compileall PASS
```

review 发现并修复一项 P1 observability 缺陷：Core full/PAUSE 等跳过 allocator 的分支会保留上一 Tick 的 allocator summary；`880e8a7` 现已在每次 plan 开始重置 `allocation_count/total_cost`，并以跨 Tick regression 锁定。

线上新 session 的完整 50 Tick：

```text
50/50 HTTP 202
299 UNIT_MOVE_SUCCEEDED
2 DEPOSIT_SUCCEEDED
1 HARVEST_SUCCEEDED
1 MOVE_DESTINATION_OCCUPIED
1 DEPOSIT_FAILED / CORE_RESOURCE_FULL
```

资源 `38 → 40`、population=8/capacity=40，13 个 `CORE_FULL` 是当前 cap=8 的有原因安全状态。失败移动为同一 carrier 在不同位置/方向的 5 次短期动态占位；后续 event 显示替代路径连续成功，未形成同 edge 重试风暴。allocator 在资源可见时真实产生 1–3 个匹配和 `HARVEST_SUCCEEDED`，无资源时 `matched=0/cost=0`。

## 9.2 多 source 干预审计（已上线，行为零变更）

最近 allocator 上线后的 50 Tick 出现 `population=8 → 10`。经同 Tick `received` 审计，新增实体来自 `MANUAL` source 的 `SPAWN VANGUARD` 和 `SPAWN WORKER`，本 Agent 的 `core_action` 全为 `NONE`。因此该窗口不能用于判定 Agent cap、容量控制器或经济策略收益。

下一批只增加 journal/metrics 的来源归因：

```text
received source/tick/action 摘要
manual_agent_intervention_count
external_spawn / external_core_action event correlation
metric_window_contaminated = true
```

它不改变 `Plan`、Core action、Worker action、traffic、allocator 或生产上限。`014eb30` 已上线并验证新 session 13/13 HTTP 202：`source_audit` 当前为 `MANUAL=0 / window_contaminated=false / last_received=AGENT`，且保留 `DEPOSIT_SUCCEEDED`。未来收到 Manual plan 时窗口会自动标记；只有无外部 source 干预的完整 50 resolved Tick 才能作为策略调参样本。

## 9.3 资源供给与匹配指标（已上线，行为零变更）

无污染 50 Tick 显示 `matched=0` 的主因是当前私有视野资源稀少，而不是 allocator 失败。`85446df` 现将 allocator 摘要拆为：

```text
eligible / visible_resources / matched / unmatched_eligible
resource_starved / total_cost
```

新 session 实证 `visible_resources 4→0→1` 时 `matched 4→0→1`，仅资源为 0 时 `resource_starved=true`。这一批不改 Plan 或调度；后续只在 `visible_resources>0 && unmatched_eligible` 持续偏高时审查 path/role 参数。出现的 `MOVE_CONTESTED` 与 `MOVE_DESTINATION_OCCUPIED` 均在下一 Tick 改道成功，尚无 traffic P0。

## 9.4 Frontier 与动态 traffic 失败分离（已上线）

无污染窗口曾显示 `frontier.failed` 在 50 Tick 内 `7→67`，但 path 均为 `FOUND`；根因是任意 `UNIT_MOVE_FAILED` 把 active frontier target 误记为不可达。`dabe719` 现规定：

```text
UNIT_MOVE_FAILED → TTL traffic backoff / alternate first step
path NO_PATH or NODE_CAP → frontier failed target
```

上线新 session 的 30 Tick 已出现两次 `MOVE_CONTESTED`，但 `frontier.failed` 保持 `0→0`；265 次成功移动、2 次交付、1 次采集继续发生。资源为零的 Tick 占 26/30，四个可见资源 Tick 均有匹配；当前瓶颈是合法视野供给，不是 frontier/path failure。

## 9.5 Frontier 完成度与失败归因（已开发，待线上验收）

深度 review 发现 `complete_frontier_if_reached()` 已实现但未被 policy 调用，导致 journal 的 `frontier.completed` 永远为零；同时失败只有总量，无法区分 BFS 无路与动态 traffic。现已修复：

```text
到达 active frontier target → completed_targets +1，并释放该 Worker target
BFS NO_PATH/NODE_CAP → failed target + failure_reasons[status]
UNIT_MOVE_FAILED → 仅 traffic TTL，不增加 frontier failure reason
journal → completed / failed / failure_reasons / active_targets
```

这批不调整 frontier 半径、角色分工、traffic 或经济动作。已上线首段 9/9 HTTP 202、95 次移动成功、1 次采集、1 次交付；`failure_reasons={}` 与 `active_targets=9` 已写入 journal。当前尚未出现 Worker 抵达 waypoint，因此 `completed=0` 是诚实状态，不宣称已完成 waypoint 闭环。

## 10. Explicit Non-goals and Review Gates

- No new long-running daemon, wrapper script or SDK migration.
- No cross-session persistent current-resource/enemy task state.
- No production cap increase, combat production, Beacon, Core migration, random path fallback, username exclusion, or map scraping.
- Before any future combat extraction, require official-state field mapping and event-level damage/shot data from this Agent's own session.
- The third-party repository remains a read-only architectural reference; no source is copied verbatim into this project.

## 11. Self-review

- The adopted global matching algorithm is bounded by current Worker/resource counts and uses the existing bounded BFS, so it cannot recreate unbounded search.
- Third-party resource persistence and historical enemy pursuit are rejected because they violate state replacement/private visibility.
- Existing Core transaction and traffic semantics are frozen first, then consumed by the allocator rather than rewritten.
- Core defense, Worker HEAL and current Vanguard guard remain behaviorally isolated from allocation.
- Each behavior change has a synthetic counterexample and event-based live acceptance criterion.
