# Arena Hero 下一阶段：持久地图覆盖、全 Worker 经济与可验证运营

状态：历史阶段计划；Phase 0-3 已实装，现行与后续策略以 `docs/DEV-STRATEGY-REFACTOR.md` 和 Obsidian Arena 章节为准。
日期：2026-08-02
前置文档：`docs/DEV-RESOURCE-EXPLORATION-ECONOMY.md`
范围：仅 `arena-hero-agent`；不操作浏览器、不读取浏览器数据、不修改 9223/9224、囤囤鼠、农场或 Supervisor 其他服务。

## 1. 本轮事实基线

### 1.1 官方规则锚点

已依据 Arena Hero 官方 Skill / quickstart / world-and-ticks / map-and-vision / core-and-economy / units / commands / resolution-results / numbers 做交叉核对：

- 唯一正确命令节奏：`tick → state → complete plan → POST → received → next state.events`；
- `state` 是当前可见世界的完整替换快照，不是增量；
- Core 视野 5、Worker 视野 3、Vanguard 视野 4、Ranger 视野 5；全部己方存活对象视野并集才是 Agent 私有视野；
- 障碍永久有效，资源观察会在迷雾中过期；两者必须分存；
- Worker 每 Tick 只能做一个动作；`HARVEST` 后自然资源点消失；交付只能在同格己方 Core；
- 每四个已结算 Tick 补充区块资源配额；补充本身没有玩家事件；
- `202` 和 `received` 都只是计划存储成功；真实移动、采集、交付必须看后续 `state.events`；
- 世界坐标是有符号 int64，不得把坐标限制为小地图的宽高；
- 资源、障碍、Core、Unit 都不构成“可以使用浏览器隐藏地图”的理由；本阶段仍只使用 Agent 合法 state。

### 1.2 当前线上实测

当前新 session 的 transport/protocol 健康：

```text
Supervisor: RUNNING
RSS: ~18 MB
近期 HTTP status: 连续 202
近期 received: 与 Agent plan 同 Tick
近期 move events: UNIT_MOVE_SUCCEEDED
```

当前的真实经济结果（最近样本）：

```text
HARVEST_SUCCEEDED: 1
DEPOSIT_SUCCEEDED: 1
resources: 2 → 3
```

这证明如下链路已真实可用：

```text
探索 → 资源进入合法视野 → TO_RESOURCE → HARVEST
→ RETURN_CORE → DEPOSIT
```

### 1.3 当前主矛盾

当前 `arena_agent/policy.py` 已有：

- 可见资源抢占；
- 有货优先返 Core；
- 永久障碍记忆；
- 当前坐标 → 目标坐标的有界 BFS；
- 中远距离方环 waypoint；
- 资源/交付事件的基本观测。

但其 waypoint 是一次性有限列表：

```text
radius = 9, 15, 21, 27, 33
```

走完后策略会进入：

```text
EXPLORATION_EXHAUSTED → WAIT
```

最近 120 个 plan 的实测分布：

```text
EXPLORE: 48
EXPLORATION_EXHAUSTED: 53
RETURN_CORE: 15
TO_RESOURCE: 2
HARVEST: 1
DEPOSIT: 1
```

因此当前主问题不是协议、资源解析或单次回家，而是：

```text
有限巡航路线耗尽后，没有持久 frontier 覆盖与下一圈扩张。
```

## 2. 目标与非目标

### 2.1 目标

将一次性 waypoint 巡航升级为持续、可回放、资源优先的 Agent 视野探索与经济策略：

```text
持续覆盖新的合法视野
→ 资源出现立刻抢占
→ Worker 采集
→ 当前起点到 Core 的障碍感知路径返航
→ 交付
→ 从未覆盖 frontier 恢复巡航
```

必须满足：

- 不再因有限 waypoint 列表耗尽长期 WAIT；
- 不依赖 Cookie、DOM、网页缓存或隐藏资源坐标；
- 返航、去资源、探索全部使用同一条“当前坐标 → 目标”的路径 API；
- 任意一条路径都使用 `permanent_obstacles ∪ current_visible_obstacles`；
- 路径无解、资源失效、移动失败时有界退化，不 OOM、不假装成功；
- 保留完整每 Tick Agent plan、202、received、events 验证；
- 先强化 Worker 经济，再讨论 Core 自动生产、Beacon、战斗。

### 2.2 非目标

本阶段明确不做：

- 浏览器辅助资源数据源；
- Core 迁移；
- Beacon；
- 战斗/自动 Vanguard、Ranger 进攻；
- 为了调试重启/操作 Chrome；
- 无界全图寻路、无限对象/路径缓存；
- 使用未知资源记忆直接 `HARVEST`。

## 3. 方案比较

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| A. 固定方环列表（当前） | 预生成有限 9–33 半径 waypoint | 实现简单、可回放 | 列表耗尽，局部折返，覆盖不记录 | 淘汰 |
| B. 随机远距离移动 | 没资源时随机方向/目标 | 表面覆盖快 | 不可回放、重复率高、撞障不可诊断、回归难测 | 禁用 |
| C. 动态 frontier 覆盖（选用） | 根据 Agent 已观测视野、永久障碍、已完成目标，在当前探索 band 中选择可达的新覆盖中心；一圈完成再扩张 | 合法、确定性、持续、可计量、可恢复 | 实现和测试较多 | 选用 |
| D. 浏览器地图引导 | 用网页缓存给资源候选 | 发现快 | 增加数据边界、CDP 可用性与账户/坐标一致性风险 | 不在本阶段 |

## 4. 目标架构

```text
WebSocket state (权威、当前可见)
      │
      ├── Snapshot replacement
      ├── Event ledger (event_id 去重)
      └── ExplorationMemory
             ├── permanent_obstacles
             ├── resource_observations（TTL）
             ├── observed_view_centers / covered cells（有界）
             ├── frontier band / candidate cursor
             ├── failed targets（有界、带退避）
             └── per-worker assignment
      │
      ▼
Policy priority
  cargo return/deposit
  > visible reachable resource
  > persistent frontier exploration
  > explicit safe WAIT + reason
      │
      ▼
PathPlanner(start, target, known_obstacles, occupied)
  → bounded BFS / later A*
  → first cardinal MOVE
      │
      ▼
complete plan → 202 → received → next events
```

原则：

- Snapshot 永不和历史资源合并；
- Memory 只影响选择候选和规划，不能伪造当前资源；
- `state.events` 是动作结果唯一权威；
- 任何 path/queue/cache 都有明确最大规模；
- `WAIT` 必须带理由，例如 `NO_FRONTIER`, `PATH_EXHAUSTED`, `CORE_FULL`，不能静默。

## 5. 阶段计划

## Phase 0 — 部署与契约收敛（P0）

### 问题

本地 `deploy/pxed-deploy.sh` 和仓库 `deploy/supervisor-arena-hero.conf` 仍有历史 cookie/venv 模板漂移；而线上目前实际运行的是 token + `/usr/bin/python3` 的经过验证配置。直接执行仓库的 `--live` 部署存在覆盖健康线上 token 配置的风险。

### 改动边界

- `deploy/supervisor-arena-hero.conf`
- `deploy/pxed-deploy.sh`
- 部署模板的专门测试或 shell 静态断言

### 实现

1. 模板只 source `.env.protected` 并显式 `export ARENA_HERO_TOKEN`；不把 token 渲染进配置；
2. 默认使用可发现/可传入的 Python，而不是假设 `.venv/bin/python` 必然存在；
3. live 部署前检查：
   - token 文件存在且 mode=600；
   - token 变量名正确；
   - Python 可执行、`websockets` 可导入；
   - `arena_agent` 编译和单测通过；
4. deploy 成功不等于 live 健康；部署脚本输出下一步验收所需 session/tick 命令；
5. 绝不调用全局 `supervisorctl shutdown`。

### 验收

```text
模板不含 ARENA_HERO_COOKIE / ARENA_HERO_CSRF
模板不含真实凭据
bash -n PASS
远端 --dry-run one-tick PASS
live restart 仅影响 arena-hero-agent
```

### 回滚

恢复上一份已验证 Supervisor 配置；只停止 Arena 程序组，不动 Supervisor 总进程与 Chrome。

## Phase 1 — 持久中远距离 Frontier Explorer（P0）

### 目的

解决 `EXPLORATION_EXHAUSTED`，将有限方环改为持续的、视野覆盖驱动的中远距离探索。

### 新数据结构

```text
ExplorationMemory
  route_core: Position
  band_radius: int                 # 初始 9，随后 +6 扩张
  frontier_candidates: deque[Position]
  completed_targets: bounded LRU[Position]
  failed_targets: dict[Position, failure_count / retry_after_tick]
  observed_cells: bounded set[Position] 或观测中心网格
  last_progress_tick: int
  active_assignments: worker_id → target
```

不储存全图。默认界限：

```text
最多 observed cells / centers: 4096
最多 completed / failed targets: 1024
最多 candidates: 512
band radius: 9 → 15 → 21 → ...，上限由可配置 max_band_radius（建议 75）控制
```

达到最大 band 后不是无限原地 WAIT：

```text
从最旧的低价值 completed sector 重新扫描
```

但每个重扫必须满足冷却 Tick，避免来回抖动。

### Frontier 候选规则

1. 以 Core 为锚点按 6 格间隔生成 band 候选，6 接近 Worker 视野直径；
2. 候选按蛇形/邻接排序，减少跨 Core 折返；
3. 排除：永久障碍、已完成冷却内 target、失败退避内 target、当前己方占位；
4. 对每个候选先用路径 API 可达性试探；
5. 目标评分（高分优先）：

```text
+ 新视野预估面积
+ 与当前 Worker 路径连续性
+ band 外沿奖励（优先中远距离）
- 回 Core 预估距离权重
- 已观测/近期重扫惩罚
- 已知失败次数惩罚
```

固定的 tie-break：`score desc → path distance asc → x → y`。

### 资源抢占

```text
cargo > 0                    → RETURN_CORE / DEPOSIT
visible reachable RESOURCE   → TO_RESOURCE / HARVEST
otherwise                    → frontier EXPLORE
```

资源在当前 state 消失、`HARVEST_FAILED`、路径无解后，撤销 assignment 并从最新 state 重新选择；不追逐历史资源。

### 文件与接口

- `arena_agent/policy.py`：FrontierMemory、候选生成、评分、assignment；
- `arena_agent/model.py`：仅在需要时增加可靠 state 字段，不重构当前 Snapshot；
- `tests/test_policy.py`：动态 frontier、冷却、扩圈、资源抢占、路径无解、记忆上限；
- `arena_agent/__main__.py`：plan journal 增加 explorer 摘要。

### 验收

离线 fixture / synthetic sequence：

```text
- 连续 100 个无资源 state 不出现 EXPLORATION_EXHAUSTED
- 不随机；相同 state 序列得到相同动作序列
- 单调增加新覆盖 center，直到 band 完成
- band 完成后扩到下一层，而不是回 Core 附近
- 新资源在当前 state 出现时，当 Tick 抢占 explorer target
- 所有路径 bounded；不可达 target 退避，不涨 RSS
```

线上（先 dry-run，后 live）：

```text
连续 30 Tick:
  no EXPLORATION_EXHAUSTED
  探索 MOVE 有 events 验证
  202/received 对齐
  RSS 不持续上升
```

## Phase 2 — 统一路径规划与失败恢复（P0）

### 目的

将巡航、去资源、返 Core 统一为唯一的 `PathPlanner(start → goal)`；禁止任一路径退回到直线/单方向猜测。

### 现状与问题

当前已使用统一 `first_step()`，也已经并入 `permanent_obstacles ∪ current_visible_obstacles`。但尚缺少：

- path 失败原因与失败 target 退避；
- 多 Worker 占位与同格容量的统一处理；
- 对连续 `UNIT_MOVE_FAILED` 的具体恢复；
- 清晰的 route 成功/失败可观测性；
- A* 预留（BFS 仍足够时不强行引入）。

### 设计

`PathPlanner` 输入：

```text
start: 当前 Worker position
goals: 资源 / Core / frontier target
obstacles: permanent_obstacles ∪ current state.obstacle_cells
occupied: 当前 controlled Unit positions，goals 例外
bounds: start/goals/known obstacles 外扩 margin
node_cap: 30,000（可配置）
```

输出：

```text
PathResult(
  first_direction | None,
  status = FOUND | START_AT_GOAL | NO_PATH | NODE_CAP,
  explored_nodes,
  target
)
```

失败恢复矩阵：

| 情形 | 行为 |
|---|---|
| `UNIT_MOVE_FAILED / MOVE_BLOCKED_TERRAIN` | 将目标格附近标记为临时失败，下一 Tick 重新从当前 state 路径规划 |
| `UNIT_MOVE_FAILED` 其他动态占位/依赖 | 不将动态失败伪装为永久障碍；短退避后重试/换 frontier |
| `NO_PATH` 到资源 | 放弃本次可见资源目标，记录 `resource_unreachable`，继续 frontier |
| `NO_PATH` 回 Core | 不丢 cargo；进入 `RETURN_BLOCKED`，每 Tick 重新规划，必要时选 Core 邻格 fallback 仅在官方规则确认时启用 |
| `NODE_CAP` | 不扩搜索上限；目标退避并写 metrics |
| `DEPOSIT_FAILED / CORE_MOVING` | 保留 cargo，等待/重新定位；不继续采集 |
| `DEPOSIT_FAILED / CORE_RESOURCE_FULL` | 保留 cargo，进入 `CORE_FULL`，停止采集，绝不自毁/丢货 |

### 验收

```text
- 起点到 Core 被永久障碍墙阻断时，选择绕路第一步
- 障碍仅在历史 memory、不在当前 state 时，仍绕开
- 当前障碍新出现时，下一 Tick 重规划
- no-path / node-cap 返回可解释状态，不 OOM
- 有货 Worker 无论 explorer assignment 如何，都优先回 Core
```

## Phase 3 — 全 Worker 编组与经济闭环（P1）

### 目的

从“只调度排序后的第一个 Worker”升级为对所有 controlled Workers 完整分配动作；当前 state 已显示除了 Worker 之外还有 Vanguard，因此策略必须明确非 Worker 的安全默认，而不是隐式遗漏。

### 默认策略

1. 对每个 Worker 独立分配：

```text
cargo return/deposit > unique visible resource assignment > frontier assignment > safe WAIT
```

2. 同一资源最多分配一个 Worker，按 Worker UUID 稳定排序；
3. 不为不确定的动态占位做隐藏信息推断；
4. Vanguard / Ranger 本阶段显式 Agent `WAIT`，除非 Manual 优先动作存在；
5. 每 state 生成当前策略希望执行的完整全 Unit action set，确保不会由于上一 Tick 遗留计划造成意外行为；
6. Core 默认不提交 action，直至 Phase 4。

### 新约束

- 单格最多 2 个占位实体；Core 格通常只容一个 Unit；
- 多 Worker 不可争同一 Resource；官方按 UUID 结算胜者，但策略端要预先避免无收益冲突；
- Worker 视野较小，Vanguard 的 4 视野可合法扩大资源可见性；本阶段不驱动其移动，只利用 state 已给出的合法并集视野。

### 验收

```text
- 两 Worker + 两资源：稳定一对一分配
- 两 Worker + 一资源：仅一人 HARVEST/去资源，另一个探索
- 一 Worker carrying：其余 Worker 仍可继续经济动作
- Vanguard/Ranger 默认显式 WAIT，plan schema 有效
- 所有 Worker action 都来自同一当前 state，不能使用旧 object ID
```

## Phase 4 — 保守 Core 经济（P1，需独立批准后实施）

### 原则

自动生产必须晚于稳定的多 Worker 采集交付，并且不得因为“资源刚到 5”就立即 SPAWN 耗尽经济。

### 默认初始策略

在满足以下全部条件才考虑 SPAWN 一个 Worker：

```text
- Core NORMAL，不处于迁移；
- Core 格没有第二个 Unit 占用；
- 至少已有 K 次真实 DEPOSIT_SUCCEEDED（建议 K=3）；
- 当前资源 ≥ 10（Worker 成本 5 + 5 资源安全缓冲）；
- population < worker_target；初始 worker_target=2；
- 最近 8 Tick 没有 CORE_DAMAGED / upkeep deficit；
- 不存在 carrying Worker 因 CORE_RESOURCE_FULL 停滞。
```

生产结果必须看 `CORE_SPAWN_SUCCEEDED/FAILED`，不能从 202 推断。

Vanguard/Ranger、护盾修复、Core 迁移不在该阶段自动化。

## Phase 5 — 运营可观测性与回放（P1）

### 目的

将“进程/协议健康”与“经济效率”分开，允许日常判断策略是否真的在赚资源。

### 新 journal 摘要

每个 plan 仅记录摘要，不恢复完整 world objects：

```text
session, tick
policy_state
worker_assignments
active_target / frontier target
band_radius / coverage counters
path_status / path_nodes
visible_resource_count / resource memory count
unit positions and cargo
plan, HTTP status, received
next event types + values summary
```

### 每 session / 滚动窗口指标

```text
transport:
  202 rate, received rate, reconnect count, tick-to-post latency

path:
  found/no-path/node-cap, move success/failure, unique frontier centers

economy:
  harvest success/failure, deposit success/failure,
  deposited amount, resources delta, resource/hr,
  explore ticks per visible resource, harvest-to-deposit ticks

safety:
  RSS, plan size, memory collection sizes
```

### Obsidian 运维文档更新范围

实施后追加：

- Frontier explorer 的策略状态含义；
- `EXPLORATION_EXHAUSTED` 从“正常终态”调整为需要检查的异常/容量事件；
- Phase 1+2 新日志字段及只读检查命令；
- 指标解释：202/received、events、策略收益三层分离；
- 保持既有“不碰 Chrome/其他服务”的红线。

## 6. 代码与测试实施顺序

```text
Step 1  Phase 0 部署模板收敛 + template tests
Step 2  Phase 1 FrontierMemory / deterministic candidate scoring unit tests
Step 3  Phase 2 PathResult / failure matrix unit tests
Step 4  Phase 3 多 Worker assignment unit tests
Step 5  主循环接入 memory + compact journal fields
Step 6  local unittest + compileall + deploy shell syntax
Step 7  GitHub CI
Step 8  pxed one-tick dry-run
Step 9  pxed live bounded smoke: 202 → received → next events
Step 10 连续 30 Tick 观察，达到 gate 后保持常驻
Step 11 更新 Obsidian 运维记录
```

禁止跨越：未通过 Step 6–9 不启动长期新策略；未产生真实 harvest/deposit 证据，不进入自动 Core 生产。

## 7. 测试矩阵

### 单元 / 仿真

1. path：直达、单墙绕行、历史障碍绕行、动态占位、无路径、node cap；
2. explorer：中距离首次目标、路线连续性、已覆盖惩罚、扩圈、循环重扫冷却；
3. resources：新可见资源抢占、资源消失、HARVEST_FAILED、TTL；
4. economy：cargo return/deposit 优先、Core 满/移动失败；
5. assignment：多 Worker/多资源、同资源排重、非 Worker WAIT；
6. protocol：plan 包含合法 actions，不使用过期 UUID；
7. memory：每个 store 最大长度、长序列不无限增长；
8. observability：journal 不泄露完整 state/token，但保留决策解释。

### 线上验收分级

| 等级 | 证据 |
|---|---|
| Build verified | 单测、compileall、shell syntax、CI 均通过 |
| Protocol verified | 同一新 session 连续 state → 202 → received |
| Movement verified | `UNIT_MOVE_SUCCEEDED` 对应计划中的 MOVE |
| Economy verified | `HARVEST_SUCCEEDED` 后 Worker cargo 增加；随后 `DEPOSIT_SUCCEEDED` |
| Strategy verified | 连续窗口内 frontier 覆盖增长、无长期 exhausted、资源/小时有可量化值 |
| Production ready | Phase 0–3 全过，RSS/错误率/资源收益达到阈值后再独立评审自动生产 |

## 8. 回滚与安全

- 每阶段独立 commit；
- 回滚仅恢复 `arena_agent` 文件并重启 `arena-hero-agent`；
- 出现认证错误、非 202、RSS 异常、连续无解释 move failure 时停止 Arena 服务，保留日志诊断；
- 不运行 `supervisorctl shutdown`；
- 不触碰 Chrome、token 不入源码/日志/Obsidian；
- 不把 MANUAL receipt 误判为 Agent receipt；
- 不把旧 session 事件算入当前策略收益。

## 9. 设计自审

已检查：

- 没有用浏览器/迷雾隐藏信息；
- 不把当前无资源解释成世界无资源；
- 不把历史资源当作可 HARVEST 资源；
- 不以 202/received 作为经济收益证据；
- 不允许有限 waypoint 耗尽后无限 WAIT；
- 不允许无界 BFS 或无界地图缓存；
- 不过早自动生产 Unit；
- 不混入战斗、Beacon、Core 移动；
- 部署修复与策略修复分阶段，避免一个改动同时改协议、策略和运维；
- 每个上线结论均区分进程、协议、动作、策略收益。

待实现授权：Phase 0 + Phase 1 + Phase 2 可作为下一开发批次；Phase 3 在前者完成后实施；Phase 4 必须观察真实收益后单独确认。
