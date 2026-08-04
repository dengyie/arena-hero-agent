# DEV：Arena Frontier Completion 与 Candidate Liveness Recovery

- 状态：**设计待确认，未实施**
- 日期：2026-08-04
- 触发证据：pxed clean session `2588c2c2ab99`，tick `50373–50410`
- 影响范围：`arena_agent/policy.py`、`arena_agent/__main__.py`、`tests/test_policy.py`、策略主契约、Obsidian 开发记录

## 1. 问题陈述

当前 Agent 的 frontier 机制已经具备有界 A*、每 Worker 预算、`NODE_CAP` 退避和 completed target 记录，但仍存在一个可达的 liveness gap：Worker 到达当前 active waypoint 后，可能在候选集被 `NODE_CAP`/backoff 暂时阻塞时再次取得同一个已完成 waypoint，最终发送显式 `WAIT`。

真实 clean-window 证据：

```text
Worker: 254818e1-7896-4c06-801d-539ca7cdaa35
50352–50372: [132,197] → [119,197]，正常到达 waypoint
50373–50410: position=[119,197]
             intent=EXPLORE, target=[119,197], distance=0
             action=WAIT
             traffic hold=None
             无 MOVE_FAILED / dynamic blocker
50411: 获得新 target [161,203]，恢复 MOVE
```

同期：

```text
frontier failure_reasons.NODE_CAP: 14 → 24
frontier failed: 14
某些 Tick path_nodes: 24,644 / 26,747（汇总所有 Worker evaluation）
协议正常：HTTP 202 / received
traffic holds: {}
```

这不是协议、Worker omission、资源分配或动态交通失败。

## 2. 根因模型

现有调用序：

```text
for unassigned Worker:
  complete_frontier_if_reached(worker)
  next_frontier(worker)
  desired[worker] = (target, EXPLORE)

later movement:
  if worker.position == target:
    RETURN_CORE → DEPOSIT
    RETURN_HEAL → HEAL
    RESOURCE → HARVEST
    EXPLORE → no action branch

end:
  actions.setdefault(worker, WAIT)
```

关键问题：

1. `complete_frontier_if_reached()` 正确将 active target 移出 `active_targets`，并写入 `completed_targets`；
2. 同一坐标仍可能留在 `frontier_candidates`；
3. `next_frontier()` 把 completed target 仅作为低优先级，而不是不可重领；
4. 当未完成候选因为 `NODE_CAP` retry/backoff、reservation 或预算暂不可用时，该 completed point 仍可作为 `START_AT_GOAL` 返回；
5. `EXPLORE + position==target` 没有完成/重新选择分支，最终由 explicit action completeness 填 `WAIT`。

因此现状违背探索 liveness：

```text
空载、健康、无资源、无交通 hold 的 Worker
不应在已完成 frontier target 上无限期保持 EXPLORE + distance=0 + WAIT。
```

## 3. 设计目标与不变量

### 必须实现

1. frontier completion 后同一 Worker 不能重新领取同一 completed waypoint；
2. 若正常候选暂不可用，必须选择有界 fallback 或记录明确的 `NO_FRONTIER` reason；
3. `EXPLORE + distance=0` 只能作为单 Tick completion transition，不能持续；
4. 不突破每 Worker 每 Tick 8 次 frontier evaluation、每次 2000 node 的既有预算；
5. 不改变优先级：

```text
carrying return/deposit > injured return/heal > current visible resource > frontier > explicit reasoned WAIT
```

6. 不用资源历史坐标采集，不使用迷雾敌方数据，不改变战斗/人口/Core-full 策略；
7. `NODE_CAP` 仍为诊断和退避事实，不被伪装为交通失败或永久障碍。

### 不做

- 不调 Worker cap、frontier max radius、A* node cap 或 combat 参数作为该问题的“修复”；
- 不在本 milestone 启用随机游走；
- 不降低返 Core/资源优先级；
- 不把 fallback 视为资源目标或允许 `HARVEST`；
- 不因一次 `NODE_CAP` 直接永久放弃一个 waypoint。

## 4. 方案比较

### A. 只在 `EXPLORE + position==target` 处补 `complete + WAIT`

优点：改动最小。

缺点：只隐藏了 same-target reselect；Worker 依然可以每 Tick WAIT，候选 liveness 和 `NODE_CAP`放大不改善。**拒绝。**

### B. 完成 waypoint 后永久排除 completed target

优点：不会重领已完成点。

缺点：地图/视野条件可能变化；永久排除过强，会丢失未来合法重访价值。**拒绝。**

### C. 引入完成冷却、deterministic fallback 和明确 no-candidate 状态

优点：

- completed waypoint 在短冷却内不可再次领取；
- 无正常候选时仍能在当前 Worker band 内取得有界、未完成、未退避的 fallback；
- fallback 仍走同一 A* budget 和 reservation；
- 当预算或候选确实耗尽时，产生可审计 `FRONTIER_NO_CANDIDATE_*`，而非伪装 `EXPLORE distance=0`；
- 能以指标验证 liveness，而非根据单个 Worker 日志猜测。

代价：需要扩展 memory、评分和测试矩阵。

**采用 C。**

## 5. 选择的架构

### 5.1 Frontier target 生命周期

```text
CANDIDATE
  ├─ path FOUND → ASSIGNED(worker)
  ├─ NO_PATH/NODE_CAP → BACKOFF(retry_after)
  └─ frontier budget exhausted → DEFERRED（不记 failure）

ASSIGNED(worker)
  ├─ Worker reaches target → COMPLETED(target, tick) + COOLDOWN(target, until)
  ├─ dynamic traffic failure → remains ASSIGNED；traffic TTL only
  └─ path NO_PATH/NODE_CAP → release + BACKOFF

COMPLETED/COOLDOWN
  ├─ cooldown active → 不可被下一次 frontier assignment 选中
  └─ cooldown expired → 可作为低优先级 re-exploration candidate（仅有明确需求时）
```

建议常量（需在实现前以 synthetic tests 固定）：

```text
FRONTIER_COMPLETION_COOLDOWN_TICKS = 16
MAX_FALLBACK_FRONTIER_CANDIDATES = 8
```

16 Tick 不是策略收益参数；它是防止同一完成点在候选缺失瞬间被立即重领的短状态去抖。它不永久禁止重访。

### 5.2 选择顺序

对某 Worker 的 frontier 选择：

```text
1. 若 active target 未到达且仍合法 → 保持 active target
2. 若 active target 到达 → 先完成并释放，不返回该点
3. 常规 candidate：
   - 未完成冷却
   - 未 reservation
   - 未 NODE_CAP/NO_PATH backoff
   - Worker band 内
   - 有界 A* FOUND
4. 若常规 candidate 无结果：
   - deterministic fallback ring
   - 与当前 Worker band 相同或更小
   - 排除 completed cooldown / reservation / obstacles / backoff
   - 最多 8 个候选、继续使用同一 frontier budget
5. 若预算耗尽：
   - 返回 `FRONTIER_BUDGET_DEFERRED`
   - action=WAIT，reason 明确，下一 Tick 重新开始预算
6. 若没有任何合法 candidate：
   - 返回 `FRONTIER_NO_CANDIDATE`
   - action=WAIT，reason 明确，不能保留 completed active target
```

`START_AT_GOAL` 仅在 active target 的完成检查之前或正常 completion transition 时消费；它不能让 completed target 成为新的 `desired EXPLORE`。

### 5.3 计划与动作语义

在 `economy_plan()` 中：

```text
if kind == EXPLORE and worker.position == target:
    这只能说明 planner contract 破损；
    重新走 completion/selection，而非由 setdefault 产生 WAIT。
```

实现应使该分支理论不可达；仍保留防御性 `FRONTIER_COMPLETION_RESELECT` journal reason，以免未来回归沉默。

### 5.4 可观测性

新增 journal 字段（仅指标，不改变 policy 以外的输入边界）：

```text
frontier:
  completion_cooldown_targets
  fallback_assignments
  no_candidate_reasons:
    FRONTIER_BUDGET_DEFERRED
    FRONTIER_NO_CANDIDATE
    FRONTIER_COMPLETION_RESELECT   # invariant breach / defensive path
  arrival_wait_ticks

worker_trace:
  frontier_completion_transition: bool
  frontier_selection_source:
    active | candidate | fallback | none
```

`arrival_wait_ticks` 定义：Worker 在 empty/healthy EXPLORE 状态、distance=0 且下一 Tick 尚未转移到新 target 的次数。上线验收目标是该值不再出现连续多 Tick；不是强制为零（单 Tick completion transition可接受）。

## 6. 文件改动清单（实施后）

| 文件 | 改动 |
|---|---|
| `arena_agent/policy.py` | 完成 cooldown、candidate filtering、deterministic fallback、明确 no-candidate result、planner invariant |
| `arena_agent/__main__.py` | frontier/worker trace liveness 指标输出 |
| `tests/test_policy.py` | synthetic multi-Tick completion、NODE_CAP/backoff、fallback、预算耗尽、资源/返程优先级回归 |
| `docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md` | 记录 frontier lifecycle 和真实上线验收 |
| `Note/Project/arena-hero-agent/详细开发与验收记录.md` | 同步设计、上线结果与未验收边界 |

不创建新 daemon、wrapper、定时任务或全局 Supervisor 配置。

## 7. 测试计划

### 单元/序列回归

1. 已到 active target：同 Tick clear active；下一目标不能等于 completed point；
2. 未完成 candidate 可达：仍优先正常 candidate；
3. 所有正常 candidate `NODE_CAP` backoff：使用 deterministic fallback，不重新选 completed target；
4. fallback 也不可用：显式 `FRONTIER_NO_CANDIDATE` + WAIT，intent target 为 null；
5. budget 用尽：`FRONTIER_BUDGET_DEFERRED`，不写 `failed_targets`、不增加 `NODE_CAP`；
6. 多 Worker reservation：fallback 不与其他 Worker 的 target/destination 冲突；
7. carrying Worker/visible resource Worker 优先级完全不变；
8. dynamic `MOVE_CONTESTED` 仍只写 traffic TTL，active frontier不进入 failed ledger；
9. completion cooldown 期内不重领同一点；过期后允许受控低优先级重访；
10. 每 current Worker 仍有 explicit action，`unassigned_workers=[]`。

### 非行为观测回归

固定 synthetic state 下比较改动前后的：

```text
Worker return/resource/Core actions 不变
仅已完成 target 的 frontier assignment/WAIT reason 改变
frontier evaluation <= 原有 8/Worker/Tick
frontier node cap <= 2000/evaluation
```

### 发布门

```text
local unittest + compileall + bash -n + git diff --check
→ GitHub CI
→ pxed staged tests + compile
→ 仅 restart arena-hero-agent
→ 新 session 202/received
→ state.events 的 MOVE/HARVEST/DEPOSIT 验证
→ clean 50 Tick liveness 窗口
```

## 8. 线上验收标准

### 必须

```text
- 连续 completed waypoint 后，不出现多 Tick：
  EXPLORE + distance=0 + same intent_target + WAIT
- fallback 或 explicit NO_CANDIDATE reason 可在 journal 清晰区分
- no unassigned workers
- HTTP 202 / received 连续健康
- 无 MOVE_FAILED 风暴、无 traffic hold 回归
- frontier NODE_CAP 不高于当前每同等负载基线；CPU 不回到旧 74% 级别
- HARVEST/DEPOSIT 和 RETURN_CORE 优先级不下降
```

### 诚实边界

- 资源当前不可见造成的 `FRONTIER_NO_CANDIDATE` 或探索不产生收入，不算修复失败；
- 只看到 `202` 不算业务成功；
- 需要至少一个 `metric_window_eligible=true` 的 50 Tick 窗口才能评价 liveness 和资源转化；
- 不在该 milestone 评价战斗收益。

## 9. 回滚

回滚只涉及 `arena_agent/policy.py`、`arena_agent/__main__.py` 与对应 tests；恢复上一已验证 commit 后：

```text
supervisorctl -c /personal/pxed/supervisord.conf restart arena-hero-agent
```

验收回滚：Supervisor RUNNING 只是进程信号；还需确认 state→202→received 和下一 state events。

## 10. 设计自检

- 是否把 current visible resources 与历史资源混用？否。
- 是否用随机动作掩盖 frontier gap？否，fallback deterministic 且有界。
- 是否放宽 A* budget/node cap？否。
- 是否改变 Core-full、人口、Worker cap、战斗或 Beacon？否。
- 是否把 dynamic traffic failure 误写为 frontier failure？否。
- 是否把 `WAIT` 全部消除？否；真正无候选/预算 defer 时仍有显式、有理由的 WAIT。
- 是否有明确 liveness 证明？有：已完成 waypoint 不可立即重领、completed cooldown、fallback/no-candidate reason 和 multi-Tick synthetic tests。
