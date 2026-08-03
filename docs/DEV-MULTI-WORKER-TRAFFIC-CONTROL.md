# Arena Hero 多 Worker 交通、资源与风险控制设计

状态：已上线交通子设计/验收记录；当前官方规则基线以 `DEV-OFFICIAL-STRATEGY-CONTRACT.md` 为准。
日期：2026-08-03
前置：`DEV-STRATEGY-REFACTOR.md`；当前运行态以 Obsidian `Note/Infra/pxed 挂机脚本运维手册.md` 的 Arena 章节为准。
范围：仅 `arena-hero-agent`。只使用当前官方 Agent state；不使用浏览器、迷雾坐标、历史敌方位置或新守护进程。

## 1. 真实问题与结论

当前版本已稳定完成协议、采集、交付、容量 3→4 恢复、HP 恢复和 8 Worker 上限控制，但最新 50 Tick 出现新的 P0：多 Worker 交通死锁。

```text
50/50 HTTP 202
339 UNIT_MOVE_SUCCEEDED
50 UNIT_MOVE_FAILED / MOVE_DESTINATION_OCCUPIED
2 UNIT_MOVE_FAILED / CELL_UNIT_LIMIT
5 HARVEST_SUCCEEDED
4 DEPOSIT_SUCCEEDED
path_length: avg 20.61, max 57
harvest→deposit: 5, 11, 11, 12, 24, 32 ticks
```

主要复现：

```text
Worker ca38... 位置 [-397, 89]
→ 每 Tick 计划 MOVE RIGHT
→ 下一 state: MOVE_DESTINATION_OCCUPIED
→ 状态不变
→ 再次规划同一 RIGHT
→ 连续 50 Tick 无进展
```

这不是障碍静态地图问题，也不是 HTTP/认证问题。当前 path 输入只包含永久障碍和己方当前位置；它没有使用当前可见敌方实体、近期动态 move failure、同 Tick 己方目的格 reservation，因此无法避开动态阻塞，并且会对同一失败边无期限重试。

## 2. 产品目标

将当前“每 Worker 独立 shortest first-step”的规划，升级为有界、确定性、全局协调的交通层：

```text
current state
→ cargo / risk / resource / frontier 任务优先级
→ 每 Worker 候选第一步
→ 动态 blocker 与近期失败边过滤
→ 本 Tick destination reservation
→ Core ingress slot 调度
→ 完整 plan
→ next state.events 校正
```

成功不等于所有 Worker 每 Tick MOVE。成功是：

- 不再对同一 `MOVE_DESTINATION_OCCUPIED` 边无限重试；
- carrying Worker 保持最高优先级，但不能挤爆 Core/相邻入口；
- 一个目标格同 Tick 只允许一个己方 Worker 认领；
- 等待必须有确定理由、退避时间和下一次重规划条件；
- 交通优化不能削弱可见资源抢占、Core 满仓事务、受伤恢复、事件账本和有界 BFS。

## 3. 冻结面与边界

不得改变：

```text
cargo > visible resource > frontier
Core full HOLD / EVICT / RECOVERY / cooldown
Worker cap=8
有界 cardinal BFS 和 PATH_NODE_CAP
当前 state 才可作为资源/敌方事实
Vanguard 只 SWEEP 当前可见相邻敌方
完整 plan / 202 / received / next events 语义
```

本批不做：

```text
Ranger SHOOT、Beacon、Core move、进攻追击
Worker cap > 8
随机逃逸
客户端预测敌方下一位置
将动态失败写入 permanent_obstacles
改变 Core 满仓容量扩张规则
```

## 4. 方案比较

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 失败后简单 WAIT | 最小改动 | carrying Worker 可无限停滞，不能解决入口排队 | 禁用 |
| 把失败目标写为永久障碍 | 快速绕开 | 敌方/友军会移动，污染地图且损失可达路线 | 禁用 |
| 每 Worker 随机转向 | 可能暂时脱困 | 不可回放、容易争同格、损害收益分析 | 禁用 |
| 单层全局路径重规划 | 可减少冲突 | 复杂，仍需要失败/入口语义 | 不足 |
| 有界动态 blocker + reservation + Core ingress 队列 | 可解释、事件校正、保留任务优先级 | 需新增交通 memory 与序列测试 | 选用 |

## 5. 数据模型

### 5.1 DynamicTrafficMemory

与永久障碍严格分离：

```text
dynamic_blocked_edges: (worker_id, from_pos, direction) -> retry_after_tick
dynamic_blocked_cells: position -> retry_after_tick
core_ingress_queue: ordered worker UUIDs
last_progress_tick: worker_id -> tick
last_move_failure: worker_id -> {reason, position, tick}
```

上限：

```text
edges <= 512
cells <= 256
worker records <= current controlled Worker count
TTL = 2 resolved ticks for MOVE_DESTINATION_OCCUPIED / MOVE_DEPENDENCY_FAILED
TTL = 4 resolved ticks for CELL_UNIT_LIMIT near Core
```

规则：

- `MOVE_BLOCKED_TERRAIN` 才可影响永久障碍候选，但仍需当前 obstacle state 佐证；
- `MOVE_DESTINATION_OCCUPIED`、`MOVE_CONTESTED`、`MOVE_SWAP_BLOCKED`、`MOVE_DEPENDENCY_FAILED` 仅影响短期 traffic memory；
- `UNIT_MOVE_SUCCEEDED` 清除该 Worker 的相关 failure/edge；
- TTL 过期后必须重新从当前 state 评估，不保留敌方历史位置。

### 5.2 TickReservation

每次 `economy_plan` 本地创建、绝不跨 Tick 保存：

```text
reserved_destinations: position -> worker_id
reserved_edges: (from_pos, to_pos) -> worker_id
reserved_core_ingress: optional worker_id
```

排序优先级：

```text
1. Core full transaction leader / evictor
2. carrying Worker return/deposit
3. hp=1 Worker return/heal
4. visible resource claimant
5. frontier worker
6. Vanguard guard
```

同优先级以 `path_length -> worker UUID` 稳定排序。

### 5.3 Core ingress

Core 与四个相邻格不是普通路段：

```text
Core cell: only one Unit can share with Core
adjacent cells: ingress lanes
```

规则：

- 当任一 carrying Worker 进入 Core Manhattan 距离 2 内，建立按 UUID 的 ingress queue；
- 队首才允许占用目标 Core 格或下一 ingress 格；后续 carrier 选择不与队首冲突的 hold/绕行候选；
- 若所有非冲突候选不可达，`CORE_INGRESS_HOLD`，并记录 queue position；
- `DEPOSIT_SUCCEEDED`、Core full transaction、Core ID 变化和队首失去 cargo 都重新计算队列；
- 不影响 Core full EVICT/RECOVERY：该事务优先级高于普通 ingress。

## 6. 算法

### 6.1 两阶段计划

阶段一只决定任务，不决定最终第一步：

```text
Worker -> RETURN_CORE / HEAL / RESOURCE(target) / FRONTIER(target) / WAIT(reason)
```

阶段二生成可执行移动：

```text
按 traffic priority 排序
for each movable Worker:
  path = bounded BFS(start, target,
                     permanent_obstacles ∪ current_obstacles ∪ active_dynamic_cells,
                     occupied_current)
  candidate = first path step
  if edge in short backoff or destination reserved:
      search alternate first-step candidates in cardinal stable order
  if candidate satisfies Core ingress reservation:
      reserve destination and edge; emit MOVE
  else:
      emit WAIT with TRAFFIC_HOLD / CORE_INGRESS_HOLD / DYNAMIC_BACKOFF
```

实现限制：不必执行完整多智能体路径规划；只需从有界 BFS 的第一个层级生成最多四个候选方向，按 `path_length, direction order` 选择未冲突项。

### 6.2 动态失败恢复

| event | 下一 Tick 行为 |
|---|---|
| `MOVE_DESTINATION_OCCUPIED` | 标记该 Worker 的失败 edge + 目标 cell 2 Tick；重新选替代第一步 |
| `MOVE_CONTESTED` | 标记目标 cell 2 Tick；更高优先级 Worker 保留 reservation |
| `MOVE_SWAP_BLOCKED` | 标记双向 edge 2 Tick；至少一方 HOLD |
| `MOVE_DEPENDENCY_FAILED` | 标记 edge 2 Tick；下 Tick 从 current state 重排 |
| `CELL_UNIT_LIMIT` | 若 Core 邻域，进入 ingress queue 4 Tick；其他位置 short cell block |
| `MOVE_BLOCKED_TERRAIN` | current state 有 obstacle 时加入 permanent memory；否则只记录异常 |
| `UNIT_MOVE_SUCCEEDED` | 清除该 Worker traffic failure，记录 progress |

不允许重复 3 次同 `worker_id + position + direction + reason` 后仍发相同 MOVE。第三次后必须产生不同候选或 `DYNAMIC_BACKOFF`，并将计数写入 journal。

## 7. 角色半径与收益控制

当前角色半径保持 Chebyshev 方环：`21 / 33 / 51 / 75`。交通层不改变资源和 cargo 优先级，但新增收益约束：

```text
frontier task score =
  path_length
  + return_distance_to_core * return_weight
  + traffic_backoff_penalty
  + revisit_penalty
```

初始只记录这些分量，不在本批修改分数权重。原因：当前真实 path_length 已得到，最新样本的长交付（24/32 Tick）需先归因于远距离、交通等待还是 Core ingress，再决定缩半径或调节评分。

## 8. 可观测性契约

每 plan 的紧凑摘要新增：

```text
traffic:
  priority_by_worker
  requested_direction_by_worker
  final_action_by_worker
  hold_reason_by_worker
  reserved_destinations_count
  reserved_edges_count
  ingress_queue
  dynamic_blocked_edges_count
  dynamic_blocked_cells_count
  repeated_failure_count_by_worker

metrics:
  path_length
  harvest_to_deposit_latency
  core_ingress_hold_ticks
  dynamic_backoff_ticks
  move_failure_by_reason
```

不写完整敌方 object、token、Cookie 或完整 state。私有 Obsidian 敏感材料段仍是独立授权存储。

## 9. 测试矩阵

1. 同 Tick 两 Worker 目标相同：更高优先级/UUID winner MOVE，另一 Worker `TRAFFIC_HOLD`。
2. 已知 dynamic blocked edge：下一 Tick 选择替代 cardinal 第一步，不重发失败方向。
3. 连续三次相同 `MOVE_DESTINATION_OCCUPIED`：不再产生相同 MOVE。
4. Core ingress 两 carrier：队首进 Core，后者 HOLD/绕行；`DEPOSIT_SUCCEEDED` 后队列推进。
5. Core full EVICT/RECOVERY 始终压过 ingress queue。
6. hp=1 Worker 回 Core/HEAL 压过资源，但 carrying hp=1 仍先交付。
7. 资源唯一认领、frontier radius、Vanguard visible SWEEP 不回归。
8. memory TTL/size 受限；1000 synthetic ticks 无增长。
9. journal 摘要没有完整 objects/凭据，包含 reservation/hold metrics。
10. characterization sequence 保持当前非冲突场景动作不变。

## 10. 上线门

```text
local 45+ tests + compileall + shell syntax
→ GitHub CI
→ pxed staged suite
→ Arena-only restart
→ 新 session protocol verification
→ 30 Tick traffic smoke
→ 50 Tick economy window
```

成功门：

```text
- 202/received 不回归
- 同一 worker/edge 的重复 MOVE_DESTINATION_OCCUPIED 不超过 2 次
- MOVE failure rate < 5%，且无单 Worker 连续失败 >= 3
- 50 Tick 内至少无 Core traffic deadlock
- harvest/deposit 与已上线基线不退化
- RSS、journal 单条大小和 path node cap 稳定
```

失败回滚：仅回退 traffic 子批的 `arena_agent` 文件、restart `arena-hero-agent`；保留日志。绝不重启总 Supervisor、Chrome、囤囤鼠或农场。

## 11. 后续独立路线

本交通批稳定后才可继续：

```text
T1: 使用指标校准近/中/远半径与 return_weight
T2: Core HP/shield + 保守 REPAIR_SHIELD
T3: Vanguard scout，先扩大合法视野
T4: Ranger SHOOT，需射线/目标/SHOT_MISSED 事件级设计
T5: Beacon、战斗、Core move
T6: cap > 8，需要人口维护费/吞吐/RSS 窗口证明
```

## 11. 线上验收结果

上线版本：`ad1bf98`。

```text
CI: PASS
pxed staged: 46 tests + compileall PASS
new session: 72/72 HTTP 202, 72 Agent received
50 Tick window: 50/50 HTTP 202, UNIT_MOVE_FAILED=0
经济: 3 DEPOSIT_SUCCEEDED, 2 HARVEST_SUCCEEDED
traffic: CORE_INGRESS_HOLD=116, repeated_failures=0
RSS: ~19 MB
```

原先 `ca38...` 在同一位置连续请求 RIGHT 并连续 `MOVE_DESTINATION_OCCUPIED` 的失败风暴已消失。新日志显示 carrier ingress queue 实际推进：队首依次变化并产生 `DEPOSIT_SUCCEEDED`，资源在观察窗口内 `27 → 28`。因此 `CORE_INGRESS_HOLD` 是可解释的吞吐排队，而不是静默死锁。

交通层目前冻结；下一步只用已记录的 `path_length`、ingress wait、harvest-to-deposit latency 和资源/已结算 Tick 做参数评审，不再修改 reservation/queue 语义，除非出现新的事件级 P0。

## 11.1 Ingress staging 吞吐修复（已上线）

交通层首次上线后发现新的 P1：所有非队首 carrier 无论距 Core 多远均 `CORE_INGRESS_HOLD`，队列可增长到 6，安全但人为拉长交付时延。修复 `af6722b` 将 hold 限制为 Core Manhattan 距离 `<= 3`；远端 carrier 仍通过 reservation 前进，只有进入最后 ingress 区域才单列。

线上验收：

```text
CI + pxed staged 47 tests: PASS
new session: 15/15 HTTP 202, 15 Agent received
UNIT_MOVE_SUCCEEDED: 97
UNIT_MOVE_FAILED: 0
DEPOSIT_SUCCEEDED: 1
远端 6 carrier 全部继续向 Core 前进；队首到达并交付
resources: 2 → 3
CORE_INGRESS_HOLD: 0（样本中尚未进入 <=3 staging 区）
```

### 11.2 未归因资源突变审计

旧 session 出现 `resources 30 → 0`、population 仍为 7、Core ID/state/hp/shield 未变化，且 journal 没有 `CORE_SPAWN_SUCCEEDED`、Core destroyed 或 upkeep 事件。不能将它归因为本 Agent 收益/损失，也不能据此调角色半径或 cap。

已上线只读审计 `cff81a3`：每 plan 记录 `upkeep_next_tick` 与受控 Core 的 `hp/shield/state`。新样本显示 `upkeep_next_tick=0`、Core `NORMAL/hp=5/shield=5`。若资源再次突变，必须先检查这三个权威字段和完整 state/events；未经归因不得改变经济策略。

## 12. 自审

- 找到的是当前 state/events 证实的动态交通死锁，不是猜测性优化。
- 动态阻塞与永久障碍严格分离。
- 不使用历史敌方坐标或雾区信息。
- 交通 reservation 是每 Tick 临时结构，不引入跨 Tick 预测。
- Core ingress 与已有容量事务分层，避免再次混淆满仓与普通返程。
- 改动按交通批独立发布，不与 Ranger、Beacon、战斗或 cap 扩张混合。
