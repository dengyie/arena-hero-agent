# Arena Hero 策略重构主契约

状态：已定稿，待策略开发授权
日期：2026-08-03
权威运行基线：`Note/Infra/pxed 挂机脚本运维手册.md` 的 `5. Arena Hero Agent`
范围：仅 `arena-hero-agent`；仅使用官方 Agent API 私有 state；不操作浏览器、Chrome、囤囤鼠、农场或其他 Supervisor 服务。

## 1. 文档审视结论

现有文档保留了正确的历史决策，但其阶段状态已过时：

| 历史文档 | 仍有效内容 | 已过期/已实现内容 |
|---|---|---|
| `DEV-RESOURCE-EXPLORATION-ECONOMY.md` | 私有视野、资源 TTL、有界路径、事件验收 | 单 Worker、不开生产、有限 waypoint 是历史阶段限制 |
| `DEV-NEXT-PHASE-MAP-ECONOMY.md` | 有界 memory、路径结果、运营分层、发布门禁 | Phase 0-3 已实装；`EXPLORATION_EXHAUSTED` 已不是当前策略状态 |
| `DEV-STRATEGY-MATURITY.md` | 经济优先级、风险矩阵、收益指标、模块责任 | "只调度第一个 Worker"、"不生产"、隐式非 Worker 已不再符合现状 |

已完成能力不得在后续重构中回退：

```text
完整 Agent plan
→ 202 / received 协议闭环
→ event_id 去重的结果验收
→ 多 Worker 唯一资源分配
→ 有界 path planner
→ 持久 frontier / stale candidate 刷新
→ Core 满仓的 Core 级容量事务
→ 当前生产上限为 3 Worker（已证实不足，待容量控制器批次替换）
```

## 2. 当前产品定义

Arena 的当前产品不是“发命令客户端”，而是一个有限状态、可解释、以资源交付为目标的经济 Agent：

```text
合法私有视野
→ 探索新的 frontier
→ 当前可见资源的唯一认领
→ HARVEST
→ 返 Core
→ 容量安全 DEPOSIT
→ 基于真实交付和风险决定是否扩张
```

成功标准分层，不能混用：

| 层级 | 合格证据 | 不足证据 |
|---|---|---|
| 进程 | Supervisor RUNNING、RSS 稳定 | 仅 RUNNING |
| 协议 | tick/state/202/AGENT received 对齐 | 仅 HTTP 202 |
| 动作 | `UNIT_MOVE_SUCCEEDED`、`HARVEST_SUCCEEDED`、`DEPOSIT_SUCCEEDED` | plan 已提交 |
| 经济 | 交付 amount、资源增量、交付时延、失败率 | 单次资源 state 变化 |
| 策略 | 有界覆盖增长、资源/已结算 Tick、稳定失败率 | 不断 MOVE |

## 3. 不可破坏的策略法律

1. `state` 是当前可见世界的权威完整替换；缺失资源、敌人或障碍不代表世界不存在。
2. 历史资源只可帮助分析，不可作为 `HARVEST` 依据；仅当前 `state.resource_cells` 可被认领。
3. `OBSTACLE` 可进入永久记忆；动态 Unit/Core 占位不得伪装成永久障碍。
4. 所有路径只允许 `current position -> current target` 的有界 cardinal planner；禁止直线猜测、全图扫描和无界 BFS。
5. 每 Tick 必须提交完整 Agent intent；省略 Unit 等价于 WAIT，不能依赖旧计划残留。
6. `202` 和 `received` 只证明计划存储；下一份 `state.events` 才能更新经济、动作和风险账本。
7. 对同一资源最多安排一名 Worker；官方 UUID 胜负规则是兜底，不能成为内部竞争策略。
8. Core 满仓是 Core 级事务：必须保留 cargo、避免 Core 格竞争、避免重复 DEPOSIT/SPAWN 风暴。
9. 所有历史结构有限：event ID、frontier、完成/失败目标、资源观察和路径节点均有上限与淘汰规则。
10. 未经独立设计和事件级验收，不启用 Beacon、战斗、Core 迁移、Vanguard scout 或 Ranger 射击。

## 4. 当前冻结面

以下已由单测、CI、pxed 暂存和真实 events 验证；没有 P0/P1 证据不得重排或重构其语义：

```text
cargo > visible resource > frontier 的经济优先级
完整 tick/state/plan/202/received 链路
有界 BFS 与 node cap
资源唯一认领
stale frontier 刷新
Core full HOLD / EVICT / RECOVERY / cooldown
当前 3 Worker 上限仅为临时护栏，不能冻结为长期策略
Vanguard/Ranger 显式 WAIT
token-only 部署与 Arena-only restart
```

## 5. 现行经济状态机

```text
ACTIVE
  ├─ carrying Worker 且 Core 非满：RETURN_CORE -> DEPOSIT
  ├─ Core 满且 carrier 在 Core 格：CORE_FULL_EVICT / CORE_FULL_RECOVERY
  ├─ Core 满且 carrier 在 Core 外：CORE_FULL_HOLD
  ├─ 空载 Worker 且可见资源：TO_RESOURCE -> HARVEST
  ├─ 空载 Worker 无资源：EXPLORE
  └─ 无有效路径：显式 WAIT + path/frontier reason
```

### Core 满仓容量恢复事务

```text
两个 carrier 位于 Core 格
→ 仅一个确定性 evictor MOVE，另一个 WAIT，禁止 SPAWN

唯一 carrier 位于 Core 格且资源满
→ carrier MOVE 到安全邻格 + Core SPAWN WORKER

carrier 位于 Core 外、恢复尚未完成
→ WAIT，不尝试进入 Core 格

CORE_SPAWN_FAILED
→ 冷却，重新读取下一 state，不重放旧事务

CORE_SPAWN_SUCCEEDED
→ Core capacity 提升，Workers 根据最新 state 再次 RETURN_CORE / DEPOSIT
```

当前运行代码的 Worker 上限仍为 3，人口 3 时容量为 15；这只是当前实现的临时护栏，已被线上满仓死锁证据否定。容量控制器批次会用受控上限、冷却与事件门替换它。没有开启 Core 迁移、护盾修复、Beacon、Vanguard scout 或战斗。

## 6. P0：容量控制器替代固定 Worker 上限

### 6.1 已证实的死锁

当前线上 session 已出现以下权威 state：

```text
population = 3
resource_capacity = max(10, 3 * 5) = 15
resources = 15
3 个 Worker 均 cargo > 0
其中 1 个位于 Core，另外 2 个在 Core 外
policy_state = CORE_FULL_HOLD，连续 133 Tick
```

当前固定 `MAX_ECONOMY_WORKERS = 3` 使 Core 满仓事务无法继续生产，从而无法提高容量；Core 外 carrying Worker 又按正确的锁规则不能争入 Core 格。结果是安全但无收益的确定性死锁。

官方规则确认：人口低于 20 时维护费为 0。因此单纯的 3 Worker 上限不是经济安全条件；容量 `max(10, population * 5)` 与真实 carrying backlog 才是控制输入。

### 6.2 不可采用的方案

| 方案 | 问题 | 结论 |
|---|---|---|
| 丢弃 cargo / SELF_DESTRUCT | 已采资源直接损失，且会产生资源堆与额外状态 | 禁止 |
| 允许全部 carrier 同时进 Core | 会复发 `CELL_UNIT_LIMIT` 和移动依赖失败 | 禁止 |
| 无限 Worker 扩张 | 人口 >=20 后维护费增长，视野/路径/经济失控 | 禁止 |
| 固定 cap=3 后永久 HOLD | 已在线上证明会停住整个经济 | 淘汰 |
| 受控容量控制器 | 仅在 Core 满、存在 backlog、无风险时逐级扩容，并有硬上限/冷却/收益门 | 选用 |

### 6.3 目标容量控制器

容量控制器的目标是把每次实际满仓视作待处理的经济 backlog，而不是永久停机信号：

```text
Core 满 + carrier backlog
→ 进行一次 Core 级恢复事务
→ Worker +1，capacity +5
→ 等待 spawn / deposit 结果
→ 下一 state 重新计算 backlog
→ 未满时恢复依次交付
→ 仍满且满足安全门时，才允许下一次恢复事务
```

初始常量，不作为永久承诺：

```text
MAX_ECONOMY_WORKERS = 8       # 硬内存/复杂度护栏，远低于维护费 tier=20
CAPACITY_RECOVERY_COOLDOWN = 4 resolved ticks
MAX_PENDING_CARRIERS = 8      # 指标/异常保护，而非正常期望
```

理由：Worker 价格 5，人口 8 时容量 40、维护费仍为 0；每次 Worker +1 仅提升容量 5。任何超过 8 的扩张必须在指标窗口证明资源吞吐、Core 风险、路径失败率和 RSS 后单独批准。

### 6.4 Core 事务状态机

状态机只基于当前 state、事件账本和确定性 Worker UUID；不使用旧位置或预期成功推断。

```text
NORMAL
  ├─ resources < capacity: 正常 RETURN_CORE / DEPOSIT / economy
  └─ resources == capacity + carrying backlog: CORE_FULL_LOCK

CORE_FULL_LOCK
  ├─ Core 格有 2 carrying Workers: CORE_FULL_EVICT
  │    → 仅 UUID 最大 evictor MOVE；其他 carrier WAIT；无 SPAWN
  ├─ Core 格有 1 carrying Worker，其他 carrier 在 Core 外:
  │    → CORE_FULL_RECOVERY
  │    → leader MOVE 离开 + Core SPAWN WORKER
  ├─ 无 carrier 在 Core 格:
  │    → CORE_FULL_STAGING
  │    → 仅 UUID 最小 carrier 可尝试进入；其余 carrier HOLD
  ├─ CORE_SPAWN_FAILED / CELL_UNIT_LIMIT:
  │    → COOLDOWN，重新读下一 state；禁止重放
  └─ CORE_SPAWN_SUCCEEDED:
       → 等待下一完整 state，重新从 NORMAL/LOCK 决策
```

关键规则：

- 一次计划最多一个 Core action；一次恢复事务只允许一个 leader 和一个 Core `SPAWN`。
- Core 格单位容量约束优先于返程路径吞吐。恢复进行期间，非 leader carrying Worker 必须 `WAIT`，不能向 Core 路径规划。
- `CORE_FULL_HOLD` 只能是有 tick、有原因、受 duration 指标约束的暂态；超过阈值必须生成明确 `CAPACITY_STALLED` 观测，而不是无限静默 HOLD。
- 对已经达到 `MAX_ECONOMY_WORKERS` 的真满仓，只允许 HOLD 并记录 `CAPACITY_HARD_LIMIT`；不得私自丢货或扩到未知人口。
- `CORE_DAMAGED`、`CORE_DESTROYED`、`CORE_MOVING`、非 202、认证错误时冻结新扩容，仅保持官方允许的安全行为。

### 6.5 生产门的拆分

普通生产与满仓容量恢复不能共用一条简单 `can_spawn_worker()`：

| 类型 | 触发 | 资源门 | 风险门 | Core 格门 |
|---|---|---|---|---|
| 常规扩张 | 稳定经济提升视野/吞吐 | `resources >= 12`、近期交付充分 | 最近窗口无 Core 风险 | 当前 Core 格无 Unit |
| 容量恢复 | `resources == capacity` 且 carrying backlog | 本 Tick 资源可支付 Worker 5；交付后/生产前结算按官方顺序 | 无 Core 风险、未冷却 | leader 依状态机撤位，其他 carrier 锁住 |

常规扩张可以在 metrics 成熟后调节；容量恢复是 P0 liveness 保障，必须先实现并以事件验收。

### 6.6 验收矩阵

离线序列必须覆盖：

1. population 3、capacity 15、resources 15、一个 Core carrier + 两个外部 carrier：只 leader `MOVE + SPAWN`，外部 carrier 均 WAIT。
2. 两 carrier 同格 Core：只一个 evictor MOVE，Core action 为 None。
3. leader 撤位成功、`CORE_SPAWN_SUCCEEDED` 后，capacity 变为 20；carrier 依 UUID 一次一个返回/交付。
4. `CORE_SPAWN_FAILED / CELL_UNIT_LIMIT`：至少 4 个 resolved ticks 不重发 SPAWN。
5. population 8、resources=capacity：不再 SPAWN，输出 `CAPACITY_HARD_LIMIT`。
6. Core damage/moving/respawn：冻结恢复；新 Core ID 后清理旧 Core transaction。
7. event 重放不重复启动事务，长期 memory 有上限。

线上分级验收：

```text
A. 新 session 连续 202 + received
B. CORE_FULL_* 计划与下一 state 的 MOVE/SPAWN 事件一一对应
C. CORE_SPAWN_SUCCEEDED 后 capacity 实际增加 5
D. 至少一个被阻塞 carrier 形成 DEPOSIT_SUCCEEDED
E. 50 resolved ticks 内 core_full_hold_ticks 有上界，不能长期占主导
F. RSS、path node cap、非 202、move failure rate 不回归
```

## 7. 重构后的模块边界

当前 `policy.py` 同时包含状态记忆、路径、frontier、账本和动作决定。继续堆积策略会降低审计性，但一次性大拆分会扰动已验证闭环。采用两阶段重构。

### Phase R1: 容量控制器稳定后，无行为变化的内部拆分

目标：不改变当前动作序列与协议行为，仅建立可单测的边界。

| 模块 | 责任 | 不负责 |
|---|---|---|
| `arena_agent/path.py` | `PathResult`、有界 BFS、path 状态 | 经济优先级、日志、网络 |
| `arena_agent/memory.py` | `ExplorationMemory`、资源观察、frontier、EventLedger、Core 事务记忆 | 命令组装 |
| `arena_agent/policy.py` | 读取 Snapshot，调用 path/memory，形成 Plan | HTTP、WebSocket、journal 写入 |
| `arena_agent/metrics.py` | session/rolling-window 指标归集和摘要 | 影响动作选择 |
| `arena_agent/__main__.py` | WebSocket、HTTP、state replacement、journal | 策略细节 |

约束：

```text
- 先做 characterization tests，锁住既有 synthetic sequence 的 Plan 输出
- 不改变 policy priority、常量、Worker cap、Core 事务或 journal schema
- 仅一批重构一个 commit；本地/CI/pxed 暂存通过后才 live
- R1 上线验收只看协议、动作事件与 RSS 无回归，不追求短期收益提高。
- R1/R2 不得排在 P0 容量控制器之前；当前已发生的 `CORE_FULL_HOLD` 长窗口应先消除。
```

### Phase R2: 指标先行，不先改决策

目标：让策略优化有真实分母，避免再次只靠 MOVE/202 判断效果。

每 session 和滚动 50 个已处理 Tick 的只读摘要：

```text
protocol:
  plan_202, agent_received, non_202, reconnects

movement:
  move_planned, move_succeeded, move_failed_by_reason
  path_found, path_no_path, path_node_cap

frontier:
  band_radius, frontier_target_completed, stale_batch_refresh
  unique_targets, revisit_count, no_frontier_ticks

economy:
  harvest_succeeded/failed_by_reason
  deposit_succeeded_amount, deposit_failed_by_reason
  resources_delta, harvest_to_deposit_ticks
  carrying_ticks, core_full_hold_ticks

capacity/risk:
  worker_count, capacity, spawn_succeeded/failed_by_reason
  core_damaged, core_destroyed, respawned

runtime:
  RSS sample, journal record size
```

规则：

- 统计必须从 `events` 和权威 state 归因；不能从 202、received 或预期 plan 推测成功。
- 运行 journal 只写摘要，不写完整 `state.objects`、凭据或无限历史；私有 Obsidian 的“私有运行材料（敏感）”段是经用户授权的独立存储，不属于运行 journal，也不得同步到 Git。
- 指标模块观察期先不影响动作；至少一个可比较的 50 Tick 窗口后才允许调整策略常量。

## 7. 后续策略调整门

R2 指标稳定后，以明确触发条件进行小批次策略优化：

| 证据 | 允许调整 | 禁止调整 |
|---|---|---|
| 新 frontier 覆盖低、MOVE 成功率高 | 调整 band/sector 评分和重访惩罚 | 使用随机地图探索 |
| `UNIT_MOVE_FAILED` 偏高 | 提升动态失败退避、降低同区并发 | 把动态占位加入永久障碍 |
| `HARVEST` 稀少、探索稳定 | 审核覆盖收益评分 | 读取浏览器迷雾数据 |
| `CORE_FULL_HOLD` 高 | 调整 Core 事务节奏/目标 Worker 上限评审 | 丢 cargo 或 SELF_DESTRUCT |
| `CORE_DAMAGED` 出现 | 冻结扩张和 scout，先记录风险 | 自动进入战斗/追击 |
| 交付稳定且无风险 | 单独评审保守 `REPAIR_SHIELD` | 与 Beacon/战斗混改 |

## 8. 未启用能力的独立路线

这些不是当前经济重构的一部分：

```text
M1: Core HP/shield 风险模型与保守 REPAIR_SHIELD
M2: Vanguard scout，只扩大合法私有视野，不进行进攻
M3: Beacon 的拾取、持有收益、丢失和风险模型
M4: Vanguard/Ranger 战斗，目标可见性、射线、伤害和退出策略
M5: Core 迁移，四 Tick 事务、交付暂停与路径影响
M6: Worker cap > 8，必须在容量控制器稳定后证明维护费、容量和资源吞吐收益
```

每个 M 需要独立设计文档、事件矩阵、synthetic tests、CI、pxed 暂存和 bounded live 验收；不得与 R1/R2 或彼此混合。

## 9. 测试与发布矩阵

R1/R2 至少补充：

1. 当前单/双/三 Worker 经济动作序列的 characterization tests。
2. Core full 的 HOLD、EVICT、RECOVERY、cooldown、spawn success/failure sequence。
3. frontier stale batch refresh、max-band rescan、资源抢占与目标唯一分配。
4. event 重放、Core ID 更换、Core 重生、容量恢复。
5. metrics 对 event 的幂等聚合、窗口裁剪和日志瘦身。
6. memory/path 上界与长期 sequence 不增长。

发布门：

```text
local unittest + compileall + shell syntax
→ GitHub CI
→ pxed staged unittest + compileall
→ Arena-only restart
→ 新 session 202/received
→ 后续 events 的 movement/economy/core transaction 验收
→ 50 Tick 指标窗口复核
```

回滚仅回退本批 `arena_agent` 文件并重启 `arena-hero-agent`。认证失败、非 202、RSS 异常或连续无解释 Core 事务失败时停止 Arena；禁止全局 Supervisor shutdown，禁止操作 Chrome 或其他服务。

## 10. 设计自审

- 本文没有把已验证的经济行为重新列为“待实现”。
- 本文将 R1 的结构整理和 R2 的观察指标与未来行为优化分开，避免一次发布混入架构与策略风险。
- 文本没有承诺浏览器数据、隐藏信息、随机探索、无限扩张或未验证战斗。
- 现行 Core 满仓事务被当作冻结面；任何未来修改需以 session 事件而非理论推演证明。
- 策略收益定义为事件/资源变化，不定义为 RUNNING、202、received 或 MOVE 数量。
