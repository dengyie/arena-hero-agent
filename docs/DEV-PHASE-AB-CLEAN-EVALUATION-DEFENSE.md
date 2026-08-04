# DEV：Arena Phase A/B — Clean Evaluation 与经济保护防守层

- 状态：**已修复 Phase B retreat liveness，待 CI/pxed 暂存和线上复验**
- 日期：2026-08-04
- 前置：`DEV-OFFICIAL-STRATEGY-CONTRACT.md`、`DEV-FRONTIER-LIVENESS-RECOVERY.md`
- 适用仓库：`/Users/mango/project/arena-hero-agent`

## 1. 问题与目标

当前 Agent 已具备：当前可见资源分配、返 Core/交付、多 Worker traffic、有界 A* frontier、frontier completion liveness、保守 Ranger/Vanguard action、combat safety fuse、战斗 episode 账本和 clean-window attribution。

当前不缺“能否下命令”，而缺两项产品能力：

1. **Phase A：可归因评价**
   - fallback frontier 是否提高资源发现/采集/交付，还是只增加移动/节点；
   - combat episode 是否在干净窗口造成或避免经济损失；
   - cooldown 是否被触发、是否解除、触发时交付受到何种影响。

2. **Phase B：经济保护防守**
   - 当前已有 Unit 被攻击时，带货 Worker、受伤 Worker 和 Core 周边的经济动作不能被被动攻击挤占；
   - 防守必须只使用当前完整 private state，不能追逐迷雾敌人或主动扩张战斗范围；
   - 防守不得生产 Ranger/Vanguard、不得启用 Beacon 或 Core migration。

本设计完成 Phase A/B 的开发边界、数据口径、状态机、测试和上线验证。它不授权 Phase C 的单位生产/Beacon/Core migration。

## 2. 当前真实证据

最新 clean session 已验证：

```text
population/workers: 8/8
HTTP 202: 50/50
UNIT_MOVE_SUCCEEDED: 393
HARVEST_SUCCEEDED: 1
DEPOSIT_SUCCEEDED: 2
MOVE_DESTINATION_OCCUPIED: 4
window_contaminated: false
```

frontier liveness 修复后的 clean 50 Tick：

```text
completion transitions: 多次
completed-point EXPLORE + distance=0 + WAIT: 0
fallback assignments: 存在
no-candidate reasons: {}
NODE_CAP: 有界，未造成 CPU 风暴
```

combat 当前存在实现和 telemetry，但尚无 clean episode 结论：

```text
合法 Ranger SHOOT / Vanguard SWEEP：已开发
友军伤害 cooldown：已开发
confirmed friendly death：已开发
combat episode：已开发
clean combat episode：尚未出现/尚未验收
```

因此本设计不以历史污染窗口中的 `SHOT_HIT`、`WORKER_CARGO_DROPPED` 或人口变化证明战斗收益。

## 3. 全局不变量（Phase A/B 不得违反）

```text
carrying return/deposit
> injured return/heal
> current visible resource allocation
> frontier
> explicit reasoned WAIT
```

另有以下硬边界：

- 资源仅取当前 state 的 `RESOURCE.positions`；不使用资源历史坐标 HARVEST；
- 障碍可作为永久观察记忆；敌人不保存为历史目标；
- `202`/`received` 不是动作成功，只有后续完整 state event 是业务事实；
- `MOVE_DESTINATION_OCCUPIED` / `MOVE_CONTESTED` 是动态交通，不写 persistent frontier failure；
- frontier 每 Worker 每 Tick最多 8 次 path evaluation、每次最多 2000 nodes；
- ordinary Worker cap=8；exception recovery 的总 population 上限=19；
- Core full、Core damage、Worker cargo、Worker hp 风险优先于防守火力；
- 不自动生产 Ranger/Vanguard；不启用 Beacon、主动追击、Core migration、SELF_DESTRUCT。

## 4. 方案比较

### 4.1 Phase A：评价口径

#### A1. 仅累计事件数

优点：实现最小。

缺点：不能区分 fallback 前后、污染窗口、资源不可见、在途返程或战斗对交付的影响。无法用于调参。拒绝。

#### A2. 原始全 state/敌方轨迹日志

优点：信息量高。

缺点：违反 private-view 最小化、日志噪声高、会引入迷雾追踪的诱惑和存储膨胀。拒绝。

#### A3. Clean-window 汇总 + 有界 episode 快照

优点：

- 只使用当前 state/event 和已有 plan 意图；
- 以 `metric_window_eligible=true` 为唯一统计分母；
- 在 fallback、combat episode 边界记录紧凑快照；
- 能区分资源供给、探索、返程、交付和战斗损失；
- 不直接驱动 policy。

采用 A3。

### 4.2 Phase B：防守方式

#### B1. Ranger/Vanguard 主动追击防守

优点：可能更快接战。

缺点：改变探索、资源、人口与战斗风险；需要敌人未来位置，违反 private-view/当前风险边界。拒绝。

#### B2. 所有敌人可见即停止 Worker

优点：简单。

缺点：敌人远离经济路线时也冻结经济，造成可预期吞吐损失，且不能证明保护收益。拒绝。

#### B3. Current-state threat envelope + Core-local guard

优点：

- 仅当前可见敌人、当前持货/受伤 Worker、Core 周边坐标决定；
- carrying/health priority 已有，不重新排序；
- Worker 保持 return/heal；
- Vanguard 仅在 Core 防区且相邻时 SWEEP；
- Ranger 只有当目标不处于携货 Worker threat envelope 才允许 fire；
- 无当前威胁时仍保持现有合法 Ranger/Vanguard 行为。

采用 B3。

## 5. Phase A：Clean Evaluation 设计

### 5.1 统计资格

一个 state Tick 仅在以下全部满足时进入 Phase A 分母：

```text
metric_window_eligible=true
= source_audit.window_contaminated=false
  AND stale_tick=false
```

污染或 stale Tick：

- 不增加 clean totals；
- 不增加 fallback 成功/失败、combat损益或吞吐归因；
- 终止当前 attribution segment；
- 仍保留已有安全/协议 journal 事实，但显示 `excluded_from_evaluation=true`。

### 5.2 Fallback evaluation ledger

新增 session-local `FrontierEvaluationMetrics`，只观察、不改 plan。

#### 记录时机

1. 本 Tick `frontier_selection_source= fallback`：创建 fallback observation：

```text
worker_id（只保留 session hash / 不持久展示原始 id 的摘要可选）
tick
position
target
path_length
frontier_nodes_this_tick
visible_resources_at_assignment
```

2. 后续同一 Worker 最多观察 `FALLBACK_OUTCOME_WINDOW_TICKS = 24` Tick；遇到以下事件关闭：

```text
HARVEST_SUCCEEDED(actor)
DEPOSIT_SUCCEEDED(actor)
新 fallback assignment(actor)
Worker confirmed death
窗口污染/stale
窗口到期
```

#### outcome 分类（互斥）

```text
HARVEST_AFTER_FALLBACK
DEPOSIT_AFTER_FALLBACK
REPLACED_BY_NEW_FALLBACK
FRONTIER_COMPLETED_NO_RESOURCE
ABORTED_CONTAMINATED
ABORTED_DEATH
EXPIRED_NO_RESOLUTION
```

`DEPOSIT_AFTER_FALLBACK` 优先于 `HARVEST_AFTER_FALLBACK` 仅在同一观察对象已经有 harvest后继续跟踪时使用；一个 fallback observation 只计一个最终 outcome，避免重复分子。

#### 汇总字段

```text
fallback_assigned
fallback_harvest_after
fallback_deposit_after
fallback_completed_no_resource
fallback_expired_no_resolution
fallback_aborted_contaminated
fallback_aborted_death
fallback_mean_path_length
fallback_node_sum
```

不把 `HARVEST` 直接归因给 fallback，除非 actor 在 observation window 内；不把资源发现或别的 Worker harvest 归因给 fallback。

### 5.3 Frontier coverage evaluation

每 clean Tick 汇总：

```text
frontier_completion_transitions
completion_cooldown_targets
fallback_assignments
no_candidate_reason counts
NODE_CAP delta（本 tick failure reason 增量，而非累计总值）
arrival_wait_ticks
completed targets
```

比率仅在分母>0时输出：

```text
fallback_harvest_rate = fallback_harvest_after / fallback_assigned
fallback_deposit_rate = fallback_deposit_after / fallback_assigned
frontier_node_per_completion = frontier_node_sum / completion_transitions
```

0 分母输出 `null`，不输出 0%，避免把“没有 fallback”误解释为差效果。

### 5.4 Combat episode evaluation

现有 `EventLedger.combat_episode` 保持为实时计数器；新增 `CombatEpisodeEvaluation` 在 episode close 时冻结一条摘要。

#### Episode 开始/结束

沿用现有开始事实：`SHOT_HIT`、`SHOT_MISSED`、`SWEEP_RESOLVED`、`UNIT_DAMAGED/ATTACK`、`WORKER_CARGO_DROPPED`、确认友军死亡。

结束：连续 8 Tick 无 combat event。

#### Episode snapshot

开始时记录 clean baseline（只存计数，不存敌方轨迹）：

```text
start_tick
clean_eligible_at_start
resource_supply.harvests / deposits
active carrying worker count
```

结束时写：

```text
end_tick
clean_eligible_full_episode
shots_hit / shots_missed
sweeps / sweep_targets_hit
outgoing_damage / incoming_damage
friendly_confirmed_deaths
friendly_cargo_lost
enemy_destruction_participations
harvest_delta
deposit_delta
cooldown_triggered
cooldown_until
outcome:
  CLEAN_COMPLETE
  EXCLUDED_CONTAMINATED
  INCOMPLETE_SESSION_END
```

一个 episode 在任一 Tick 污染后标记 `EXCLUDED_CONTAMINATED`；仍可保留安全/损失事实，不得参与 combat收益率。

#### 战斗评价门

不自动由 metrics 改变攻击策略。只有满足以下才允许未来人工评审战斗生产/位置策略：

```text
至少 3 个 CLEAN_COMPLETE episode
无 episode 出现 confirmed friendly death
friendly_cargo_lost=0 或有人工确认的业务例外
harvest/deposit delta 不低于同类 clean noncombat 基线
cooldown 至少一次真实触发、一次自然解除
```

这些是设计评审门，不是自动开关。

## 6. Phase B：经济保护防守层

### 6.1 Threat envelope

只从当前 state 计算，不保存敌方历史位置：

```text
THREAT_RADIUS = 3
CORE_GUARD_RADIUS = 3
```

定义：

```text
enemy_threatens(worker) =
  exists current visible enemy with Manhattan(worker, enemy) <= 3

core_threatened =
  exists current visible enemy with Manhattan(core, enemy) <= 3
```

距离 3 不是攻击距离或追击半径；它是对“当前可见敌人可在短期影响携货/受伤 Unit”的保护 envelope。敌人移出当前 state立即不再构成 threat。

### 6.2 Worker 防守规则

不增加新的 Worker action类型，只强化已有任务选择：

```text
if cargo > 0:
  RETURN_CORE / DEPOSIT 保持最高优先级
  若 enemy_threatens(worker): journal worker_defense_reason=CARRYING_THREAT

if hp <= 1:
  RETURN_HEAL / HEAL 保持第二优先级
  若 enemy_threatens(worker): journal worker_defense_reason=INJURED_THREAT

empty + enemy_threatens(worker):
  不分配该 Worker 为资源/探索攻击诱饵；
  若 Core 存在，目标=Core，intent=RETURN_SAFE
  到 Core 后 WAIT（不发 HEAL，除非 hp<=1）
```

`RETURN_SAFE` 是 plan intent 标签，不是新的官方 action。它只在当前 threat 下将空载 Worker 移至 Core；不追敌，不阻断其他不受威胁 Worker 的资源任务。

上线后真实日志发现瞬时 visibility flap 会导致 `RETURN_SAFE ↔ EXPLORE` 每 Tick切换、Worker 在两格间往返。修复为 sticky retreat transaction：首次当前 threat 将空载健康 Worker ID 写入仅己方 `safe_retreat_workers`；在到达 Core、开始携货或进入受伤治疗优先级前，即使下一 Tick威胁暂时不可见也持续 `RETURN_SAFE`。journal 必须同时区分当前 `threatened_workers` 与仍在返程的 `safe_retreat_workers`；后者不是持久敌方事实。

### 6.3 Vanguard 防守规则

当前 Vanguard legal local guard 扩展为：

```text
仅当 Vanguard 位于 Core_GUARD_RADIUS 内
AND 当前可见敌人正交相邻
AND (core_threatened OR 存在 carrying/injured Worker 处于 enemy threat)
→ SWEEP
否则 WAIT
```

这比当前“任何相邻可见敌人都 SWEEP”更保守，目的不是提高杀敌数，而是避免远离经济区域的战斗主动化。

若当前不存在 Core position，Vanguard WAIT。

### 6.4 Ranger 防守规则

现有 legal geometry 和 safety fuse 不变。新增 `ranger_fire_allowed` 必须同时满足：

```text
现有 gate（not core_full / population<19 / no cooldown / no friendly damage window）
AND 没有 carrying Worker 处于任一当前 visible enemy 的 threat envelope
AND Ranger 本身位于 CORE_GUARD_RADIUS 内，或 target 位于 Core 防区内
```

这不是追击，也不使 Ranger 走向目标。目的：当有携货 Worker 被当前敌人威胁时，优先让经济单位返 Core，不用 Ranger 的一次不确定 `SHOT` 换取更高掉货风险。

### 6.5 Core 防区与 priority 证明

Phase B 绝不改动：

```text
Core full transaction
capacity recovery
RETURN_CORE / DEPOSIT
RETURN_HEAL / HEAL
visible resource matching
frontier/path budgets
population policy
```

防守层只作用于：

```text
empty threatened Worker 的 frontier/resource assignment
Vanguard 是否可 local SWEEP
Ranger 是否可 legal SHOOT
journal defense reasons
```

## 7. 文件与 hook allow-list

| 文件 | Phase A | Phase B |
|---|---|---|
| `arena_agent/policy.py` | fallback observation hooks、episode close snapshot | current-state threat envelope、RETURN_SAFE、Vanguard/Ranger defense gates |
| `arena_agent/combat.py` | 无或只加纯 target filter helper | Core-local target/filter helper，不保存敌方历史 |
| `arena_agent/__main__.py` | clean evaluation summary journal | defense reason / threat counts journal |
| `tests/test_policy.py` | attribution、episode close、污染排除、fallback outcome | carrying/injured/empty Worker、Vanguard/Ranger defense gates |
| `docs/DEV-OFFICIAL-STRATEGY-CONTRACT.md` | 评价口径与真实验收 | 防守边界与真实验收 |
| `Note/Project/arena-hero-agent/详细开发与验收记录.md` | 开发/上线证据 | 开发/上线证据 |

禁止修改：

```text
model.py 的 private-view边界
allocator.py 的当前可见资源匹配算法
path.py 的 budget/node cap
部署脚本的全局 Supervisor 行为
任何认证材料处理
```

## 8. 测试计划

### Phase A

1. clean fallback observation 后同 Worker HARVEST：只计一次 `HARVEST_AFTER_FALLBACK`；
2. fallback 后 DEPOSIT：只计一次最终 outcome，不重复 harvest/deposit；
3. fallback window污染：标 `ABORTED_CONTAMINATED`，不计 clean rate；
4. fallback window过期：`EXPIRED_NO_RESOLUTION`；
5. `NODE_CAP` delta 按 tick 增量记录，不把累计值重复归因；
6. clean combat episode close 输出正确计数与 harvest/deposit delta；
7. contaminated combat episode 标 `EXCLUDED_CONTAMINATED`，不进 clean episode rate；
8. session end 时 open episode 标 `INCOMPLETE_SESSION_END`，不伪装 complete。

### Phase B

1. carrying Worker + visible敌方 threat：仍 RETURN_CORE，journal reason正确；
2. injured Worker + threat：仍 RETURN_HEAL；
3. empty Worker + threat：RETURN_SAFE 到 Core；敌方不在当前 state后恢复正常可见资源/frontier；
4. 无 threat 时，既有 visible resource/return/frontier actions不变；
5. Vanguard 在 Core 防区外相邻敌人 WAIT；Core 防区内且经济风险存在才 SWEEP；
6. Ranger 在 carrying Worker threat envelope 内 WAIT；其他合法 Core-local shot保留；
7. 动态 MOVE failure 仍只进 traffic TTL；
8. Worker explicit action / `unassigned_workers=[]` 保持；
9. Core full/人口19 guard 与 Phase B 不交叉破坏。

### Noninterference fixed-state probes

对固定 state 比较 Phase A/B 前后：

```text
无敌方时：carrier/resource/injured/frontier plan 字节级等价
有敌方但不在 threat envelope：经济 actions 等价
有敌方且 carrying threat：仅被威胁空载 Worker / combat action 允许改变
```

## 9. 上线与验收

发布门：

```text
local unittest / compileall / bash -n / diff check
→ GitHub CI
→ pxed staged tests + compile
→ only supervisorctl restart arena-hero-agent
→ new-session 202 / received
→ 30 Tick smoke
→ clean 50 Tick attribution window
```

### Phase A 线上验收

```text
resource/fallback evaluation fields 存在
clean 与 contaminated窗口不混分母
至少一个 fallback observation 有明确最终 outcome 或过期原因
combat 无事件时 episode字段诚实为空
有事件时不把 202/received 当结果
```

### Phase B 线上验收

```text
无敌人窗口：计划与现有经济路径无回归
当前威胁出现：journal defense reason 出现
carrying/injured Worker 不被分配到 frontier/resource
Ranger/Vanguard 不扩大至追击
真实 combat episode 时再评价安全性与经济损益
```

## 10. 回滚

Phase A/B 均只回滚 `arena_agent` 源文件和 tests，使用上一已验证 commit：

```text
restore files
supervisorctl -c /personal/pxed/supervisord.conf restart arena-hero-agent
```

回滚验收：RUNNING 不足够；必须确认 state → 202 → received 与后续 state events。

## 11. Phase C 明确排除与进入门

本设计不实现：

```text
automatic Ranger/Vanguard spawn
Beacon pickup/drop/expedition
Core migration
active pursuit
historical enemy targeting
```

进入 Phase C 前必须人工审查以下 clean evidence：

```text
≥3 clean complete combat episodes
无 confirmed friendly death
cargo loss=0 或明确经济补偿证据
fallback/探索没有拉低 harvest→deposit throughput
cooldown 已真实触发并解除
population/upkeep/Core-full 没有风险
```

## 12. 设计自检

- 是否把 Phase A metrics 接到自动 policy 调参？否，全部仅观察；
- 是否把敌方历史位置持久化？否，只用当前 `visible_enemies`；
- 是否以 threat 名义追击？否，Worker只回 Core，战斗 Unit不移动；
- 是否改变 carrying/return/heal/visible resource 优先级？否；
- 是否将 combat hit 当收益？否，必须 clean complete episode 才可评价；
- 是否混入单位生产/Beacon/Core migration？否，明确排除；
- 是否给 0 分母伪造百分比？否，输出 null；
- 是否有污染/stale cut？有，统计不入分母并中止 attribution segment；
- 是否有测试、防回归和回滚？有，见第8–10节。
