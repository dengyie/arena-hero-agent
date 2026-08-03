# Arena Hero 官方规则驱动的 Agent 策略契约

状态：当前权威开发契约
日期：2026-08-03
官方来源：`https://doc.arenahero.io/`，本次逐页核对 `World and Ticks`、`Commands and Priority`、`Movement and Stacking`、`Map and Vision`、`Core and Economy`、`Units`、`Combat`、`Champion Beacon`、`Destruction and Respawn`、`Game API`、`Reference`。
运行实况来源：Obsidian `Note/Infra/pxed 挂机脚本运维手册.md` 的 Arena 章节。
范围：仅 `arena-hero-agent`；仅官方 Agent API 私有 state 与 commands；不读取浏览器、Cookie 前端状态、迷雾数据或任何其他服务。

## 1. 三层事实模型

任何设计结论必须标注所属层级，禁止把一层推断当成另一层事实。

| 层级 | 来源 | 可用于 | 不可用于 |
|---|---|---|---|
| 官方规则 | `doc.arenahero.io` | 行动合法性、结算顺序、字段语义、错误处理 | 推断当前世界隐藏状态 |
| 权威当前 state/events | Agent WebSocket 当前完整 private snapshot | 本 Tick 任务、实体、资源、风险、结果修正 | 保留缺失对象为当前事实 |
| Agent memory | 已验证永久障碍、TTL 资源观察、短 TTL traffic 失败 | 有界路径和退避 | HARVEST、攻击或敌方位置事实 |

## 2. 官方协议契约

### Tick 与计划

```text
global 15-second command window
→ tick
→ state（唯一行动信号，完整私有快照）
→ POST one Agent plan
→ received（当前存储的 plan）
→ next state.events（唯一结算事实）
```

- 同 source 的成功 POST 替换旧 plan，服务端不 patch/merge；Agent 每次必须提交完整 intent。
- 仅在拿到该 Tick 的 `state` 后发送；同 Tick 的不确定上传只用相同 key 与相同 body 恢复。
- `202`/`received` 只说明储存；不能计为移动、采集、交付、生产、治疗、攻击成功。
- `TICK_MISMATCH`/`COMMAND_WINDOW_CLOSED` 等交接或窗口错误，等待/recompute 于下一 state；`IDEMPOTENCY_CONFLICT` 不可盲重试。
- 静态非法 action 会原子拒绝整个 plan，旧有效 plan 保持不变；动态条件只在 resolution 产生 events。

### 官方 resolution order

```text
final Agent/Manual plan lock
→ SELF_DESTRUCT / cargo drops
→ upkeep / farthest excess Unit deaths
→ Unit movement + finished Core migration
→ validate START_MOVE
→ Beacon pickup/drop
→ Worker HARVEST/DEPOSIT
→ immutable combat snapshot + simultaneous damage/destruction/capture
→ surviving Unit HEAL (UUID order)
→ surviving Core HEAL / REPAIR_SHIELD / SPAWN (one action)
→ Core respawn attempt
→ periodic resource replenishment (each 4 resolved Ticks)
```

设计后果：

- 不能以本 Tick 移动是否计划成功推断 Unit/Cell 占位；下一 state event 才是结果。
- Worker DEPOSIT 在 Core action 前结算，实际接收的资源可支付同 Tick Core action；upkeep 已更早结算，不能倒灌。
- Unit HEAL 与 Core HEAL 是不同动作，不能合并处理。

## 3. 世界、视野与记忆

- `state` 是当前 private visible world 的完整替换；缺失实体/资源/Beacon 字段必须丢弃旧事实。
- 当前 `RESOURCE` 仅表示当前可见且可用；历史资源仅供分析，绝不可产生 HARVEST/目标事实。
- 障碍是地图地形，可进入永久障碍记忆；Unit、Core、移动失败和动态占位只能进入 TTL traffic memory。
- 资源按固定 chunk quota 每四个 resolved Tick 仅补已消耗的位置；不未使用累积。
- Core vision、Unit vision和 Beacon 可改变当前私有视野，但 Agent 不能探测迷雾以外目标。

## 4. 单位与经济规则

### Unit actions

| 类型 | 官方可用能力 | 当前 Agent 策略 |
|---|---|---|
| Worker | MOVE, HARVEST, DEPOSIT, HEAL, Beacon, SELF_DESTRUCT, WAIT | MOVE/HARVEST/DEPOSIT/HEAL/WAIT；不 Beacon/self-destruct |
| Vanguard | MOVE, SWEEP, HEAL, Beacon, SELF_DESTRUCT, WAIT | 当前可见且正交相邻敌方时 SWEEP，否则 WAIT |
| Ranger | MOVE, SHOOT, HEAL, Beacon, SELF_DESTRUCT, WAIT | 禁用 |

- Worker HARVEST 需要空 cargo 且站在当前 resource cell；自然资源得 1，Beacon owner 得 2。
- 同一 resource 多 Worker 竞争由最低 UUID 获胜，内部策略必须先唯一认领，不能把官方 tie-break 当调度方案。
- DEPOSIT 需要 Worker 与己方 Core 同格；Core 满则 `DEPOSIT_FAILED/CORE_RESOURCE_FULL`，cargo 保留。
- 任意 Unit 的 `HEAL` 需要在己方、未迁移 Core 格；combat 后按 UUID，每少 1 HP 花 1 Core resource。hp=1 Worker 空载返 Core/HEAL、带货先交付是官方顺序下的正确策略。

### Core 经济

```text
capacity = max(10, population * 5)
Worker cost = 5
Vanguard cost = 10
Ranger cost = 12
one Core action / Tick
one Unit spawn / Tick
Core cell capacity = Core + one Unit
```

- `SPAWN` 后 Unit 出生 Tick 不行动，且 combat 后才出生。
- Core `HEAL` 花 1 resource / missing HP；`REPAIR_SHIELD` 花 1 resource / shield。它们与 SPAWN 互斥。
- 维护费不足不伤 Core，而是移除最远 excess Units；population 降低后若资源超过新 capacity，溢出资源会立即销毁并发 `CORE_RESOURCE_OVERFLOW_DESTROYED`。
- 资源突变必须先归因于 `CORE_RESOURCE_OVERFLOW_DESTROYED`、Core capture、spawn/repair/heal、upkeep 死亡、Core destruction/respawn 或当前权威 state；禁止按裸 `resources` 调策略。
- 当前 cap 8 是 Agent 风险护栏，不是官方人口上限。提升前必须有完整 50 resolved Tick 证据：upkeep、overflow、交付时延、Core 风险、RSS、traffic failure。

## 5. 移动与交通契约

官方移动以全局 dependency graph 同步结算：

- 跨 source 争同一 destination，双方失败；没有提交顺序优先。
- 己方多个对象争有限 slot，最低 UUID 可能获胜，但策略不能依赖。
- 可以进入当前占用格，只要占用者成功离开且最终 capacity 合法；链和 swap 由 resolution 决定。
- terrain、swap、dependency、occupancy、capacity 都是动态结算，必须读 `UNIT_MOVE_FAILED` reason。

当前已上线交通层：

```text
permanent obstacles
+ current obstacles
+ TTL dynamic blocked edges/cells
+ per-Tick destination/edge reservation
+ Core ingress queue (only <=3 Manhattan staging holds)
+ next-state event correction
```

冻结约束：

- dynamic failure 永远不写为永久障碍；
- 同 worker/from/direction 的失败边须退避并换候选或显式 hold；
- Core full EVICT/RECOVERY 优先于普通 ingress；
- 远端 carrier 继续接近，只有 Core staging 区非队首才 `CORE_INGRESS_HOLD`；
- 无事件级 P0 不重排 traffic reservation/queue 语义。

## 6. 当前任务优先级

```text
1. Core full transaction (EVICT / RECOVERY / cooldown)
2. carrying Worker RETURN_CORE / DEPOSIT
3. empty hp=1 Worker RETURN_CORE / HEAL
4. current visible resource unique claimant
5. bounded role frontier (Chebyshev 21 / 33 / 51 / 75)
6. explicit reasoned WAIT
7. Vanguard legal adjacent SWEEP, otherwise WAIT
```

优先级不等于绕过 traffic 约束。高优先级任务先获得 reservation，但仍要满足官方 destination/cell capacity。

## 7. Combat、Beacon 与迁移边界

### Combat

- Vanguard SWEEP 只作用于相邻格所有敌方 entity，空格 SWEEP 也会成功但 `targets_hit=0`。
- Ranger SHOOT 仅对当前 state 可见 target 才具有效益；resolution 要求 target 仍在 expected cell、敌对、同直线/45-degree、距离 1-3、无中间 obstacle。动态失败统一 `SHOT_MISSED`。
- combat 使用不可变快照并同步伤害；不以先后手、最后一击或历史目标推断。

当前仅启用可见相邻 Vanguard SWEEP；没有真实 `SWEEP_RESOLVED`/伤害闭环前，不启用 Vanguard pursuit、Ranger、攻击性生产或 Beacon。

### Beacon、Core move

Beacon carrier/ground 可见字段在下一 state 缺失时必须清除旧值。Core migration 为四 Tick 事务，会影响 Unit HEAL、Core action、Beacon、路径和交付；未独立设计前保持禁用。

## 8. 当前实现与验证状态

| 能力 | 状态 | 事件级证据 |
|---|---|---|
| 协议/完整 plan | 已上线 | 连续 202/received；state.events 校正 |
| bounded BFS/frontier | 已上线 | 无 OOM、path metrics |
| 资源采集/交付 | 已上线 | HARVEST_SUCCEEDED/DEPOSIT_SUCCEEDED |
| Core full capacity recovery | 已上线 | CORE_SPAWN_SUCCEEDED + deposit |
| hp=1 Worker return/Unit HEAL | 已上线 | UNIT_HEAL_SUCCEEDED |
| dynamic traffic/reservation | 已上线 | 50 Tick 无 MOVE failure 突发 |
| ingress staging | 已上线 | 远端 carrier MOVE，近区单列 |
| Vanguard guard | 代码受限启用 | 未有 SWEEP_RESOLVED 真实闭环 |
| Ranger/Beacon/Core move | 禁用 | 未设计/未验证 |

## 9. 指标与决策门

每次策略调整先以至少 50 resolved Tick、当前 session 的 events 和 state 为证据：

```text
protocol: status/received/stale tick/reconnect
movement: success/failure reason, repeated edge, reservation/hold duration
traffic: ingress queue length, near-stage hold, remote staging distance
resource: harvest, deposit amount, harvest-to-deposit latency, resource observations
core: resources/capacity/population/upkeep_next_tick/hp/shield/state/overflow
risk: Unit HP/damage, Core damage/destroyed/respawned
runtime: RSS, journal record size, path nodes/length
```

允许的下一优化只包括：

1. 已归因资源事件后的 frontier/role radius/return cost参数调整；
2. ingress staging 阈值的单变量评审；
3. Core `REPAIR_SHIELD` 的独立防守设计，前提是明确风险和经济门；
4. 合法可见 Vanguard scout 的独立设计。

禁止：不明资源变化时扩 Worker、读取迷雾、动态障碍永久化、随机逃逸、与战斗/Beacon/Core move 混批。

## 8.1 官方经济归因与 Core 防守批次（已开发，待线上事件验证）

本批将官方 Core/state 事实接入模型和策略：

```text
Snapshot: core hp/shield/state + upkeep_next_tick
Event ledger: UPKEEP_PAID / CORE_RESOURCE_OVERFLOW_DESTROYED /
              CORE_HEAL_* / CORE_REPAIR_*
Core action: NORMAL Core、resources >= 10、无 Core full/recent Core damage 时
             hp<5 → HEAL；否则 shield<5 → REPAIR_SHIELD
```

Core 防守绝不覆盖 Core full recovery，也不改变 Worker/traffic/ingress/cap=8。当前线上 Core `NORMAL/hp=5/shield=5/upkeep=0`，因此预期无 Core action；只有出现权威缺口时才会发官方合法 action，并以 `CORE_HEAL_SUCCEEDED` / `CORE_REPAIR_SUCCEEDED` 验收。

## 9. 发布与回滚

```text
local unit tests + compileall + shell syntax + diff check
→ GitHub CI
→ pxed staged tests + compile
→ Arena-only restart
→ new session 202/received
→ next-state events
→ 30 Tick smoke + 50 Tick economy/traffic window
```

回滚仅针对本批 `arena_agent` 文件和 `arena-hero-agent`。认证错误、持续非 202、RSS 异常、连续同边失败或无解释 Core 经济事件时停止 Arena；绝不停止总 Supervisor、Chrome 或无关服务。

## 11. 文档关系

- 本文是当前策略法律与官方事实的唯一主契约。
- `DEV-MULTI-WORKER-TRAFFIC-CONTROL.md` 是已上线 traffic 设计/验收记录。
- `DEV-STRATEGY-REFACTOR.md`、`DEV-RESOURCE-EXPLORATION-ECONOMY.md`、`DEV-NEXT-PHASE-MAP-ECONOMY.md`、`DEV-STRATEGY-MATURITY.md` 为历史演进材料，不可单独决定新开发。
- Obsidian 运维手册是私有运行状态、凭据材料和上线证据的权威记录；代码设计不得将敏感材料写入 Git。
