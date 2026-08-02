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
→ 3 Worker 容量上限
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
人口上限为 3 Worker
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

Worker 上限是 3，不是“无限扩张”。人口 3 时容量为 15；后续是否提高上限必须由维护费、风险、交付吞吐和长窗口证据单独决定。

## 6. 重构后的模块边界

当前 `policy.py` 同时包含状态记忆、路径、frontier、账本和动作决定。继续堆积策略会降低审计性，但一次性大拆分会扰动已验证闭环。采用两阶段重构。

### Phase R1: 无行为变化的内部拆分

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
- R1 上线验收只看协议、动作事件与 RSS 无回归，不追求短期收益提高
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
- journal 只写摘要，不写完整 `state.objects`、凭据或无限历史。
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
M6: Worker cap > 3，必须证明维护费、容量和资源吞吐收益
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
