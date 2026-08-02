# Arena Hero 策略成熟化设计：从单 Worker 巡航到可持续经济自治

状态：设计完成，待开发授权
日期：2026-08-02
前置：`DEV-RESOURCE-EXPLORATION-ECONOMY.md`、`DEV-NEXT-PHASE-MAP-ECONOMY.md`
范围：只修改 `arena-hero-agent`。仅使用官方 Agent API 的合法私有 state；不依赖浏览器、Cookie、DOM 或隐藏地图信息。

## 1. 当前能力与真实数据

当前上线版本已经具备基础闭环：

```text
tick → state → complete Agent plan → HTTP 202 → received → next state.events
```

当前 session 的实测结果：

```text
plans: 214
UNIT_MOVE_SUCCEEDED: 208
HARVEST_SUCCEEDED: 2
DEPOSIT_SUCCEEDED: 3
resources: 5 → 8
frontier band: 已推进到 21
```

已解决的问题：

- 无资源时永久 WAIT；
- 无界 BFS 导致 OOM；
- 有货返 Core 未记住历史障碍；
- 有限 waypoint 耗尽；
- Core 满仓导致每 Tick DEPOSIT 风暴；
- Core 重生后旧 `CORE_FULL` 暂停状态遗留；
- 旧 cookie/venv 部署模板与 token 线上配置漂移。

当前策略仍是初级版本，缺口：

```text
1. 只调度字典序第一个 Worker；
2. 非 Worker（现有 Vanguard）仅隐式 WAIT，未显式策略化；
3. frontier 评分以路径搜索节点数近似路径成本，未估算新视野收益；
4. 资源/路径失败只做基础退避，未做事件账本、目标生命周期与恢复分流；
5. Core 生产、护盾修复、容量规划完全关闭；
6. 没有基于资源/小时、发现时延、交付时延的策略调参闭环；
7. 没有生命周期风险模型，Core 被攻击/摧毁时只依赖通用 snapshot 更新；
8. 没有对 Beacon 与战斗进行保守、可验证的后续策略分层。
```

## 2. 官方规则约束

以下设计必须遵守官方 Arena Hero Skill / rules / API：

- `state` 是当前可见世界的全量替换快照；缺失不是“不存在”；
- 己方所有 Core/Unit 永远出现在 state；敌方、资源、障碍受私有视野过滤；
- Core/Worker/Vanguard/Ranger 视野分别为 5/3/4/5，私有视野为并集；
- 障碍永久、资源可消耗且补充，历史资源只能作为观察，不能直接用于 HARVEST；
- 一个 Unit 每 Tick 一个动作；移动为单格四方向；
- Worker 仅在空 cargo 且站在 RESOURCE 时 HARVEST；带货同格 Core 才 DEPOSIT；
- Core 容量 `max(10, population * 5)`，满时 Worker cargo 不丢失；
- 每四个已结算 Tick 才补齐区块资源配额；补充不产生玩家事件；
- Agent plan 是该 tick 的完整替换；省略 Unit 会按 WAIT；
- `202`/`received` 只证明计划存储；下一 state.events 才证明动作结果；
- 同资源多 Worker 竞争时仅原始 UUID 最小者 HARVEST 成功，应在策略端避免竞争；
- Core 每 Tick最多生产一个 Unit，Core 格有容量限制；生产、修盾不能在迁移时做；
- Unit 生产成本 Worker/Vanguard/Ranger = 5/10/12；人口 >=20 才产生维护费；
- 不发送自定义 WebSocket 业务帧；Ping/Pong 由库处理；
- 未知动态失败只能视为 unknown，不能借此推断迷雾信息。

## 3. 产品目标

目标不是“不断发 MOVE”，而是让 Agent 在可解释、安全边界内完成持续经济增长：

```text
合法视野探索
→ 发现资源
→ 多 Worker 无冲突采集
→ 可靠绕障返 Core
→ 容量安全交付
→ 在收益与安全门槛满足后扩张 Worker
→ 记录事件账本、收益和风险
→ 发生满仓、迁移、摧毁、重生时恢复正确状态
```

阶段性收益目标，不作为未验证承诺：

```text
- 探索：连续 100 Tick 无无理由 NO_FRONTIER / EXPLORATION_EXHAUSTED；
- 经济：每个 HARVEST 能在有路径时形成可追踪 RETURN_CORE → DEPOSIT；
- 多 Worker：零同资源争抢导致的 RESOURCE_DEPLETED；
- 生产：仅在真实收益稳定、容量安全、Core 安全时扩张；
- 运营：能回答资源/小时、采集失败率、交付时延、路径失败率、Core 风险。
```

非目标：

- 不通过浏览器读取灰色迷雾资源；
- 不使用隐藏 world seed；
- 不引入无限缓存、无限路径搜索、无限重试；
- 不在没有事件证据前启用激进战斗或 Beacon 抢夺；
- 不改变/重启 Chrome 或其他 Supervisor 服务。

## 4. 目标架构

```text
             state (权威、当前可见)
                        │
                        ▼
┌─────────────────────────────────────────────┐
│ WorldMemory                                 │
│ - permanent_obstacles                       │
│ - resource_observations (TTL)               │
│ - frontier coverage / failed targets         │
│ - event_id bounded dedup                    │
└─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────┐
│ EconomyLedger                                │
│ - per worker: cargo/assignment/lifecycle    │
│ - harvest/deposit counters, timing           │
│ - Core capacity / core generation            │
│ - damage / respawn / pause state             │
└─────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────┐
│ Tactical Planner (current state only)        │
│ Cargo return > deposit > resource claim      │
│ > frontier assignment > explicit safe WAIT   │
│ Uses PathPlanner(start → target)             │
└─────────────────────────────────────────────┘
                        │
                        ▼
Complete Agent plan → 202 → received → events ledger
```

设计规律：

1. 当前 state 决定当前事实；memory 只保存永久障碍、历史观察、失败退避和策略进度。
2. 每项行动必须可被下一 state 的 event 验证或明确归类为未验证。
3. 每个 memory store 均有容量上限与淘汰规则。
4. 经济扩张永远比“多做动作”晚：先证明采集/交付吞吐，再生产。
5. 所有 Unit 必须获得显式计划意图，避免“省略 = 不小心 WAIT”。

## 5. 分阶段策略

## Phase A：多 Worker 资源编组（P0）

### 问题

当前代码只选择 `sorted(workers)[0]`。当有多个 Worker 时，其他 Worker 隐式 WAIT，资源发现、路径覆盖和产能无法扩展。

### 设计

为所有 Worker 按稳定 UUID 顺序分配任务：

```text
1. carrying Worker：RETURN_CORE / DEPOSIT / CORE_FULL_WAIT
2. visible reachable resources：一资源仅分配一个 Worker
3. remaining empty Workers：分配不同 frontier sector
4. 无合法任务：显式 WAIT + reason
```

资源分配使用最小成本二部匹配的简化确定性贪心：

```text
候选 = (Worker, visible resource, PathResult)
排序 = path status FOUND → path cost / nodes → Worker UUID → resource coordinate
逐条选取，已分配 Worker / resource 不再参与
```

不能让多个 Worker 同时 HARVEST 同一资源；即使官方 UUID 规则会决定胜者，策略端也不浪费 Tick。

占位规则：

- 路径规划的 `occupied` 包含其他己方 Unit；
- 目标 RESOURCE / Core 仍允许作为目标格；
- Core 格容量与生成/交付分别由官方 state/events 判断；
- 每 Worker 每 Tick 只一项 action。

### 验收

```text
2 Workers + 2 Resources → 稳定一对一分配
2 Workers + 1 Resource → 仅一名抢占，另一名探索
Worker A carrying → A 回家；Worker B 继续采/探
同一 state 生成所有 Worker 的明确 action
不存在 RESOURCE_DEPLETED 由己方内部竞争引起
```

## Phase B：Frontier 覆盖评分与路径成本（P0）

### 问题

当前 frontier 选择将 BFS `explored_nodes` 当作成本；它不是实际步数，且没有衡量“新视野收益”。

### 设计

升级 `PathResult`：

```text
status
first_direction
path_length
explored_nodes
start, target
```

BFS 保持可用、稳定，但记录 parent/depth 得到真实 path length。若以后地图/距离规模使 BFS 成为瓶颈，才引入 deterministic A*；本阶段不预设 A*。

Frontier 评分：

```text
score(target) =
  + expected_new_view_cells(target, WorkerVision=3)
  + sector_age_bonus
  + band_progress_bonus
  - path_length * travel_cost
  - return_distance(target, Core) * return_cost
  - revisit_penalty
  - failed_target_penalty
```

其中 `expected_new_view_cells` 只用已观测 coverage grid 计算，不假设障碍后可见性；真实新视野仍以下一 state 为准。

路线：

- 围绕 Core 的 band 由 9 起、每 6 扩展；
- 不再只沿方环边缘，而是选取视野格子覆盖最大且连续的 sector center；
- 达到最大探索半径后按最旧 sector 冷却重扫；
- 有货返程或资源抢占后，恢复未完成 assignment，而非从 band 开头重新扫。

### 验收

```text
同一 synthetic state 序列得到相同行动序列
相同 resources 下，选择较短真实路径的 frontier
已覆盖 sector 低于新 sector 优先级
资源出现立即抢占 explorer assignment
100 Tick 无资源序列不出现无限 memory 或无解释 WAIT
```

## Phase C：事件账本与恢复状态机（P0）

### 问题

当前仅根据部分 events 更新 resource/core_full；没有 event_id 去重、没有每 Worker action outcome 账本，也没有完整重生/摧毁恢复。

### 设计

新增有界 `EventLedger`：

```text
event_id LRU (max 4096)
worker_outcome[worker_id]
last_harvest_tick / last_deposit_tick
worker assignment lifecycle
core_generation / core_id
core_health_state
```

事件处理矩阵：

| event | 策略更新 |
|---|---|
| UNIT_MOVE_SUCCEEDED | 确认 assignment 进度，清除该路径临时失败 |
| UNIT_MOVE_FAILED / MOVE_BLOCKED_TERRAIN | 目标短退避；下一 Tick 从真实当前位置重规划；不得把动态失败伪造为永久障碍 |
| HARVEST_SUCCEEDED | 标记资源 observation depleted；Worker 进入 return lifecycle |
| HARVEST_FAILED / RESOURCE_DEPLETED | 释放资源 assignment；不重试同格；重新匹配 |
| HARVEST_FAILED / NOT_RESOURCE_CELL | 历史资源 observation 标为失效；回 frontier |
| DEPOSIT_SUCCEEDED | 增加 ledger amount；清除 carrying lifecycle；恢复 frontier/资源分配 |
| DEPOSIT_FAILED / CORE_RESOURCE_FULL | CORE_FULL_WAIT；cargo 保留；不重复 DEPOSIT |
| DEPOSIT_FAILED / CORE_MOVING | holding；Core 正常后重新交付 |
| CORE_DAMAGED | 记录安全状态，冻结扩张门槛 |
| CORE_DESTROYED | 清除与旧 Core/旧 Unit ID 绑定的 assignments；保留永久障碍；等待 state 的新 Core/Worker |
| CORE_RESPAWNED / 新 core_id | 以权威 resources/capacity 清除旧 core_full；重建 Core anchoring 与 frontier，不能带旧路线 |
| WORKER_CARGO_DROPPED | 记录资源堆观测，不假设立即可回收；进入当前可见 state 后再合法 HARVEST |

### 验收

- 重连重复同 event_id 不重复增加收益/失败数；
- Core ID 变化会清除旧 assignment；
- Core 重生后若 `resources < capacity`，带货 Worker 恢复 DEPOSIT；
- `CORE_FULL_WAIT` 不再产生重复失败事件；
- journal 能追溯每个 Worker 的最近 action/result。

## Phase D：保守 Core 经济与 Worker 扩张（P1）

### 原则

自动生产不以“资源够 5”作为触发。目标是提高合法视野与采集吞吐，不是无条件人口增长。

### 扩张门槛（默认）

只在下列条件全部为真时发 `core_action: SPAWN WORKER`：

```text
Core 正常、未迁移
Core 格当前可再容纳一个 Unit
resources >= 12
最近 20 Tick 至少 3 次 DEPOSIT_SUCCEEDED
最近 20 Tick 无 CORE_DAMAGED / CORE_DESTROYED
没有 carrying Worker 处于 CORE_FULL_WAIT
当前 Worker 数 < worker_target
frontier 覆盖尚有可达候选
```

初始目标：

```text
worker_target = 2
```

Worker #2 成功、稳定观察一个收益窗口后，才评审目标 3；不自动无限扩张。

资源安全缓冲：

```text
reserve = 5  # 保留一次 Worker 成本或 Core 风险恢复余量
```

生产验证只看：

```text
CORE_SPAWN_SUCCEEDED / CORE_SPAWN_FAILED
```

若 `CELL_UNIT_LIMIT`，不连续重发；记录短退避，等待 Unit 离开 Core 格。

### Shield 修复

默认关闭，直到获得 Core HP/shield 字段与攻击事件的稳定观测。届时仅在：

```text
shield < cap
resources > reserve + repair_cost
当前不在生产关键 tick
最近有 CORE_DAMAGED
```

才考虑 `REPAIR_SHIELD`。不能抢占带货 Worker 的交付能力。

### 验收

```text
不因 resources=5 生产
满足所有门槛仅生产一个 Worker
CORE_SPAWN_SUCCEEDED 后新 Worker 在下一 Tick 才分配任务
CELL_UNIT_LIMIT / INSUFFICIENT_RESOURCES 不重试风暴
```

## Phase E：Vanguard / Ranger 的合法视野利用与安全默认（P1）

当前 state 已有 Vanguard，但当前策略不为它生成显式计划。策略成熟化第一步不是让它冲锋，而是使其状态有确定语义：

```text
Vanguard/Ranger 初始：显式 WAIT
```

原因：其合法视野会自动并入 state，无需移动即可帮助资源发现；但无事件账本、敌我风险模型、战斗阈值时主动移动/攻击会牺牲经济和 Core 安全。

后续只在以下数据条件完成后开启保守 scout：

```text
Phase A-C 已稳定
Worker 经济有持续交付
最近窗口无 Core 风险
Vanguard 移动不会阻塞 Core 格 / Worker 返程
```

首个 scout 角色只做：

```text
围绕 Worker frontier 相邻站位扩大视野
不进入未知远距追击
收到 CORE_DAMAGED / 发现敌方威胁时回 Core 保护带
```

战斗、Beacon 另立文档并单独授权。

## Phase F：收益、风险与策略调参（P1）

### 指标

按 session / 50 Tick 滚动窗口记录：

```text
经济：
  harvested_amount
  deposited_amount
  resources_delta
  resource_per_resolved_tick
  harvest_to_deposit_ticks

探索：
  new_coverage_cells
  frontier_path_length
  resource_discovery_ticks
  revisit_rate
  no_path_rate

可靠性：
  202_rate
  received_rate
  event_resolution_rate
  move_failure_rate
  RSS

风险：
  CORE_DAMAGED count
  CORE_DESTROYED count
  core_full_wait_ticks
  respawn_ticks
```

不将 `received` 数量当作收益，不将资源 state 的短期变化自动归因于 Agent；收益归因优先 `HARVEST_SUCCEEDED` / `DEPOSIT_SUCCEEDED.values.amount`。

### 自适应但确定的参数

策略不能随机化。参数可按滑动窗口调整，但阈值固定：

```text
发现率低且 path 成功率高 → 增大 frontier band
重访率高 → 提升覆盖/冷却惩罚
move failure 高 → 降低 band 扩张、提高 failed target backoff
core_full_wait 高 → 冻结生产与采集扩张，优先容量/安全评审
core damage 出现 → 冻结生产与 scout，进入保守经济
```

每次调参要写 journal：旧值、新值、触发指标、持续窗口。

## 6. 代码模块边界

| 文件 | 责任 | 禁止事项 |
|---|---|---|
| `model.py` | 当前 state 严格解析、Core/Unit 可选字段补全 | 不保存历史观察 |
| `memory.py`（新增） | WorldMemory、EventLedger、容量限制、Core generation | 不发命令 |
| `path.py`（新增） | PathResult、BFS/A*、可达性、节点上限 | 不判断经济优先级 |
| `policy.py` | 多 Unit 任务分配、frontier 评分、Core action 决策 | 不直接写 journal/网络 |
| `__main__.py` | tick/state/receipt 协议、整体 state 替换、调用 policy、紧凑日志 | 不嵌入策略细节 |
| `journal.py` | 摘要事件与窗口指标 | 不写 token/full state |
| `tests/` | synthetic state/event sequences、回归、边界 | 不依赖真实 token |

这不是为了抽象而抽象：当前 `policy.py` 已同时承担 path、memory、frontier、经济状态，继续增加 Phase A-D 会降低可维护性。拆分只在实现 Phase A-C 时一起完成，避免多轮无收益重构。

## 7. 实施批次与验收门

### Batch 1（建议先做）

```text
Phase A 多 Worker 编组
+ Phase B frontier 路径长度/覆盖评分
+ Phase C event ledger 与 Core/Worker 生命周期
```

开发顺序：

```text
1. 补 model Core HP/shield/state 等已存在官方字段
2. 新增 memory/path 模块和纯单测
3. 迁移现有 policy，保持单 Worker 行为测试不退化
4. 加多 Worker / Core 重生 / 重复 event / path failure synthetic sequences
5. 本地测试、compile、CI
6. pxed dry-run
7. bounded live：20 Tick
8. 至少一条真实 harvest/deposit 再扩大观察至 50 Tick
```

### Batch 2（单独确认）

```text
Phase D 保守 Worker #2 生产
+ Phase F 指标驱动调参
```

只有 Batch 1 证明多 Worker assignment、事件账本和 Core lifecycle 稳定后才进入。

### Batch 3（单独设计/确认）

```text
Phase E Vanguard scout
→ Beacon
→ 战斗
```

## 8. 风险与回滚

| 风险 | 约束/回滚 |
|---|---|
| 多 Worker 同资源竞争 | 策略端唯一 assignment；测试 UUID 排序 |
| 多 Worker 路径互相卡位 | occupied 纳入 path；动态失败短退避，下一 Tick 重规划 |
| 新 Worker 堵住 Core 格 | 生产前检查；CELL_UNIT_LIMIT 退避 |
| 资源观察过期 | 只对当前 state resource 执行 HARVEST |
| Core 满仓 | CORE_FULL_WAIT，停止无效 DEPOSIT/新采集 |
| Core 重生状态污染 | core_id/generation 变更时重置 transient economy memory |
| 过度探索导致无经济收益 | 指标门控、band 上限、收益窗口评估 |
| 新策略 OOM | 每个 map/path/memory 都有容量上限；RSS gate |
| 协议错误或非 202 | 停 Arena，保留 session 日志；不重试风暴 |

回滚单位：一个策略 batch 一个 commit。回滚仅：

```text
恢复 arena_agent 文件
→ 仅 restart arena-hero-agent
→ 验证同一新 session 的 state → 202 → received
```

禁止：全局 Supervisor shutdown、Chrome 操作、凭据写入源码或日志。

## 9. 自审

- 没有使用浏览器或迷雾隐藏坐标；
- 没有把历史资源当作当前 RESOURCE；
- 没有将 202/received 作为收益；
- 资源、路径、Core 生命周期均以 state/events 为权威；
- 自动生产置于多 Worker/账本稳定之后；
- Vanguard 的初步用途是合法扩大已获得的私有视野，不是无证据战斗；
- 每条 memory/path 都有限；
- 当前已验证的协议与单 Worker economic loop 不被改写，Batch 1 必须有回归锁定；
- 需要实施时先做 Batch 1，完成后深度 review 真实收益再决定 Batch 2。
