# Arena Hero 资源探索与 Worker 经济闭环

状态：待评审（文档阶段，尚未实现）
日期：2026-08-02
范围：Arena Hero Agent 的资源收集与基础经济，不涉及战斗、Beacon 或浏览器自动化

## 1. 目标

将当前“收到 state 后能提交 WAIT/基础计划”的协议客户端，扩展为完整的确定性 Worker 经济循环：

```text
探索 → 发现可见资源 → 移动 → HARVEST → 携货返回 Core → DEPOSIT → 继续探索
```

必须保持官方协议闭环：

```text
tick → state → 完整 plan → POST 202 → received → 下一 state.events
```

产品目标：

- 长期自动发现并采集资源；
- 采集成功后可靠回 Core 交付；
- 依据下一份 state.events 校正本地记忆，而不是凭 202 推断动作成功；
- 资源暂时不可见、已耗尽、移动失败时能继续探索，不永久 WAIT；
- 低资源、低内存、可恢复；
- 不依赖 Chrome、DOM 或浏览器页面。

非目标：

- 本阶段不加入战斗；
- 不加入 Beacon；
- 不自动生产第二个 Worker；
- 不改变 WebSocket/HTTP 传输协议；
- 不改变 9223/9224、囤囤鼠或农场服务；
- 不把浏览器画面中的资源坐标当作 Agent 隐藏信息。

## 2. 官方规则基线

依据官方 Arena Hero Skill、游戏规则、命令 API、结算结果和可靠命令循环文档：

- 每个账号最多一个存活 Core；
- Core 视野为 5，Worker 视野为 3；
- `state` 是当前可见世界的权威快照，必须整体替换；
- `OBSTACLE` 是永久地形，可进入永久障碍记忆；
- `RESOURCE` 是可消耗观察，可能被采集、部分消耗、补充或在视野外过期，不能当永久地图；
- Worker 成本为 5；初始资源为 5；初始人口为 1；
- 资源容量为 `max(10, population * 5)`；
- `HARVEST` 要求 Worker 空载且位于资源格，成功装载 1；
- `DEPOSIT` 要求 Worker 有货且与己方 Core 同格；
- Core 容量满时 `DEPOSIT_FAILED / CORE_RESOURCE_FULL`，Worker Cargo 保留；
- 资源每 4 个已结算 Tick 按区块配额补充；补充本身不产生玩家事件；
- `202 Accepted` 只表示计划入库；实际结果在下一份 `state.data.events`；
- 计划是完整替换，不是增量合并；每次提交必须包含本 Tick 全部希望执行的 Unit actions；
- 同一个 Tick 只能使用同一个逻辑计划的幂等键重试，不能换 body/key 猜测；
- Agent WebSocket 不发送 subscribe/ready/heartbeat 业务帧，标准库负责协议 Ping/Pong；
- `UNAUTHORIZED`、WS 1008 等认证问题必须停止，而不是无限重试。

## 3. 当前实现缺口

当前模型已经能识别可见 `RESOURCE`，并能在资源格执行 `HARVEST`、带货返回 Core、同格 `DEPOSIT`；但当 `state` 没有可见 `RESOURCE` 时，策略会直接返回 `WAIT`。

这会导致：

```text
Worker 在 Core + 当前视野无 RESOURCE
→ 没有目标
→ WAIT
→ Worker 永远不离开 Core
→ 没有新的视野
→ 永远发现不了资源
```

因此核心修复不是“把 WAIT 改成随机 MOVE”，而是增加受约束、可复现、基于障碍记忆的探索状态机。

## 4. 数据模型设计

### 4.1 当前快照与持久观察分离

`Snapshot` 继续只表示当前 state；新增独立的 `ExplorationMemory`，禁止把历史资源并入当前 `resource_cells`：

```text
Snapshot
  - tick/status/resources/capacity
  - controlled Core/Units
  - 当前可见 resource_cells
  - 当前可见 obstacle_cells
  - 当前 state.events

ExplorationMemory
  - permanent_obstacles: frozenset[Position]
  - resource_observations: dict[Position, ResourceObservation]
  - waypoints: tuple[Position, ...]
  - waypoint_index: int
  - active_target: Position | None
  - target_worker_id: str | None
  - last_event_id: bounded dedup set
  - last_action_failure: bounded metadata
```

### 4.2 资源观察

每个 `ResourceObservation` 至少包含：

```text
position
last_seen_tick
status = visible | targeted | depleted | failed | stale
failure_count
last_event_id
```

规则：

- 当前 state 重新看到资源时更新 `last_seen_tick`，状态回到 `visible`；
- `HARVEST_SUCCEEDED` 将该位置标记为 `depleted`，但不永久删除，允许未来补充后重新发现；
- `HARVEST_FAILED / NOT_RESOURCE_CELL`、`RESOURCE_DEPLETED` 将本次观察标记失效；
- 超过观察 TTL 的历史资源不能直接作为当前采集目标；
- 当前可见资源优先级高于历史 waypoint；
- 永久障碍只追加，不因一次不可见而删除；
- 事件用 `event_id` 去重，重连快照重复事件不能重复推进状态。

默认 TTL：资源观察连续 8 个已处理 Tick 未再次看见即 stale。TTL 只影响目标选择，不影响永久障碍。

## 5. 探索策略

### 5.1 设计原则

探索必须满足：

- 确定性：同一 Core/Worker/障碍记忆产生相同 waypoint 顺序；
- 有界：每次路径搜索有有限空间和节点上限，不能再触发无界 BFS/OOM；
- 低开销：只维护一个 Worker 的一个活动 waypoint；
- 视野优先：优先从 Core 周围开始，逐步扩大；
- 不使用隐藏地图、world seed 或浏览器坐标；
- 资源出现后立即抢占探索目标。

### 5.2 Waypoint 生成

第一版生成 Core 周边的确定性 manhattan 环：

```text
radius = 3, 5, 7, 9, ...
方向顺序 = UP, RIGHT, DOWN, LEFT
每个 radius 只产生有限候选点
```

候选点必须：

- 不在永久障碍记忆；
- 不等于当前己方 Core/其他己方 Unit 的占位格；
- 通过有限 BFS 可达；
- 不重复最近已完成的 waypoint；
- 到达或失败后再推进索引。

不直接以“朝资源方向随机走”代替 waypoint；随机会导致不可复现、重复撞墙和难以回放。

### 5.3 目标优先级

每个 state 按以下顺序选择一个 Worker action：

1. Worker 带货且不在 Core：回 Core；
2. Worker 带货且在 Core：`DEPOSIT`；
3. 当前可见资源中有可达目标：前往最近目标，距离相同时按坐标稳定排序；
4. Worker 已在可见资源格且空载：`HARVEST`；
5. 当前无可见可达资源：前往下一个探索 waypoint；
6. waypoint 不可达：标记失败并换下一个；
7. 没有可用 waypoint：`WAIT`，但必须记录 `exploration_exhausted`，不能静默卡死。

带货优先级高于探索和新资源目标，避免 Worker 丢失经济收益。

## 6. 事件驱动校正

下一份 state 的 `events` 是实际结算依据。需要处理：

### 成功事件

- `UNIT_MOVE_SUCCEEDED`：确认 Worker 位置变化；清理对应失败计数；
- `HARVEST_SUCCEEDED`：更新 Worker cargo 的本地期待值，标记资源观察 depleted；
- `DEPOSIT_SUCCEEDED`：记录存入数量，清除 Worker cargo 期待值；

### 失败事件

- `UNIT_MOVE_FAILED`：保留当前真实位置，当前 waypoint failure_count+1，必要时换 waypoint；
- `HARVEST_FAILED / NOT_RESOURCE_CELL`：废弃该次资源观察，回到探索；
- `HARVEST_FAILED / CARGO_FULL`：以新 state 的 cargo 为准，进入回 Core；
- `HARVEST_FAILED / RESOURCE_DEPLETED`：资源观察标记 depleted，换目标；
- `DEPOSIT_FAILED / CORE_NOT_PRESENT`：重新规划回 Core，不重复提交 DEPOSIT；
- `DEPOSIT_FAILED / CORE_RESOURCE_FULL`：保留 cargo，停止继续采集，进入保守 WAIT/资源消耗策略；本阶段不自毁、不丢货；
- `DEPOSIT_FAILED / CORE_MOVING`：等待 Core 迁移结束，保留 cargo；

未知事件只记录，不猜测其含义。

## 7. 计划与状态机

建议显式状态：

```text
BOOT
EXPLORE
TO_RESOURCE
HARVEST
RETURN_CORE
DEPOSIT
RECOVER_MOVE_FAILURE
PAUSE_CORE_FULL
```

状态不是服务器权威状态；每次新 state 仍以当前快照为准，FSM 只保存目标和探索记忆。

每个 state 仍生成完整计划：

```json
{
  "tick": 123,
  "unit_actions": {
    "worker-uuid": {"type": "MOVE", "direction": "RIGHT"}
  }
}
```

不跳过 `WAIT` 提交，不改变幂等键和 202/received 闭环。

## 8. Core 生产策略（本阶段默认关闭）

本阶段不自动 SPAWN：

- 初始资源为 5，Worker 成本也是 5；
- 先证明单 Worker 的探索、采集、交付闭环；
- 没有真实 `HARVEST_SUCCEEDED`/`DEPOSIT_SUCCEEDED` 样本前，不扩大人口；
- 未来生产必须有单独策略：保留维护费/容量余量、确认当前 Worker 任务不中断、确认资源收益足以覆盖成本。

## 9. 日志与可观测性

保留当前日志瘦身，不恢复完整 state.objects。每个 plan 日志增加：

```text
policy_state: EXPLORE/TO_RESOURCE/HARVEST/RETURN_CORE/DEPOSIT/PAUSE_CORE_FULL
active_target
waypoint
resource_visible_count
resource_memory_count
last_event_types
```

保留：

- session/tick；
- state summary；
- plan；
- HTTP status；
- received；
- 下一 state.events；
- RSS/重连/认证错误由运维侧读取。

不写 token、cookie、完整历史 state、无界路径队列。

## 10. 测试计划

### 单元测试

1. 当前可见资源优先于 waypoint；
2. 无资源时 Worker 从 Core 走向第一个确定性 waypoint；
3. 障碍阻挡时选择有限 BFS 路径；
4. 不可达 waypoint 在节点上限内返回失败；
5. 到资源格空载 Worker 生成 `HARVEST`；
6. 带货 Worker 生成回 Core 的 `MOVE`；
7. 同格带货 Worker 生成 `DEPOSIT`；
8. `HARVEST_SUCCEEDED` 标记资源观察 depleted；
9. `RESOURCE_DEPLETED` 切换目标；
10. `DEPOSIT_FAILED / CORE_RESOURCE_FULL` 不丢 cargo、不继续采集；
11. 重复 event_id 不重复推进 FSM；
12. 初始资源 5 时不生成 Worker；
13. plan 日志只保存摘要，不保存完整 objects。

### 线上验收

先 dry-run，再 live；不直接扩大生产：

```text
阶段 A：
  20 Tick 内出现探索 MOVE，不再全程 WAIT

阶段 B：
  发现资源后出现 MOVE → HARVEST

阶段 C：
  HARVEST_SUCCEEDED
  → cargo > 0
  → RETURN_CORE
  → DEPOSIT
  → DEPOSIT_SUCCEEDED

阶段 D：
  连续 30 Tick:
    202/received 与 Tick 对齐
    events 可解析
    RSS 稳定
    无 OOM
    无认证/重连风暴
```

评价指标：

- `resources` 增量；
- `HARVEST_SUCCEEDED` 次数；
- `DEPOSIT_SUCCEEDED` 次数及 amount；
- `UNIT_MOVE_FAILED` 比例；
- 资源发现到采集的 Tick 数；
- 采集到交付的 Tick 数；
- `WAIT` 占比（探索阶段与 Core 无资源阶段分别统计）；
- RSS 和日志增长速率。

## 11. 风险与回滚

风险：

- 探索 waypoint 可能进入资源不可见或死路；通过有限 BFS、失败计数和 waypoint 切换控制；
- 资源观察可能过期；通过 TTL 和 events 失效处理；
- 过早生产 Worker 造成资源耗尽；本阶段关闭生产；
- Core 满载时继续采集导致 Worker 卡死；`CORE_RESOURCE_FULL` 进入暂停状态；
- 资源字段/事件契约变化；未知字段/事件只记录，不猜测。

回滚：

- 恢复当前 `policy.py`/`model.py`/`__main__.py` 版本；
- 保留官方协议客户端和日志瘦身；
- 仅重启 `arena-hero-agent`，不重启 Supervisor 总进程、不碰 Chrome；
- 回滚后验证 `tick → state → 202 → received`。

## 12. 文档自审结论

- 没有把浏览器显示资源当作 Agent 隐藏信息；
- 没有把历史资源观察混入当前 state；
- 没有把 `202` 当作动作成功；
- 没有设计客户端 WebSocket 业务心跳；
- 没有开放无边界探索或无界 BFS；
- 没有在资源收益未验证前生产新 Worker；
- `CORE_RESOURCE_FULL` 保留 cargo，不会错误丢货；
- 资源补充不生成玩家事件，必须等待后续可见 state；
- 所有计划仍是完整替换，符合官方 API；
- 代码阶段必须先加测试，再 dry-run，再单次 live smoke，再连续线上验收。

评审门：本文档目前是“待评审”，尚未修改源码或线上策略。
