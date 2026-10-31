# 情绪三层模型（v2 新增）

原 ESDM 报告把模型命名为 Emotion-Strength Decay Model，但公式里没有任何情绪项，“情绪作为 λ 的调节因子”只出现在其“未来工作”一节。本文件把这一空缺定义为可实现、可测试的三层结构。

核心原则：**“我记得当时很难过”与“这件事现在还能让我难过”不是同一个量，不能共用一个会衰减的变量。** 前者是历史事实，后者是当前负荷。此外还有第三层：“现在的我怎么理解那时的我”，它是解释，会重写但不衰减。

## 一、三层的可变性

| 层 | 内容 | 是否随时间改变 | 驱动什么 |
|---|---|---|---|
| L1 历史层 | 情绪事实原子 + `valence_at_event` / `arousal_at_event` 快照 | **永不修改** | 内容相关性、可被检索的过去 |
| L2 负荷层 | `affect.charge`，当前还能唤起多少情绪 | **唯一随时间消退**，也可被再激活 | 是否主动重提、注入策略 |
| L3 评价层 | `appraisal.current` + `history` | 走 revision，**不是衰减** | 用哪一版理解回答 |

三条禁令：

1. **L3 不得覆盖 L1。** 用现在的理解改写当时的情绪，等于系统性篡改用户的自传体记忆。回答时用 `appraisal.current` 解释、用快照描述当时，二者并存。
2. **charge 归零不等于遗忘。** 归零只改变主动注入策略，不删除、不归档、不降低 L1 的检索权重。用户主动追问时必须完整返回 L1 与快照。
3. **情绪不影响事实可信度。** 高唤醒提升的是显著性与保持，不是准确性（闪光灯记忆现象：鲜明且自信，但未必更准）。事实更正永远走 supersede，不能因情绪分而保留旧版本。

## 二、为什么 L2 不能简单写成 `a × exp(-t)`

- **情感消退偏差（fading affect bias）**：自传体记忆的事件内容保留，伴随情绪强度下降，且负性情感平均消退快于正性。这支持 L2 独立衰减，并要求半衰期带 valence 方向差异。
- **消退不是擦除**：消退训练并不删除原始联结，而是新增抑制性记忆与之竞争；原始联结可在情境变化或时间推移后恢复（renewal / spontaneous recovery）。因此 L2 必须有回升通道，且归零后不得清除历史。
- **再巩固（reconsolidation）**：记忆被提取后进入可修改状态再存回。所以“回忆”是写入事件，不是只读 +1。本实现里只有真实人类 `remind` 会回充 L2，Agent 自己的检索与 `used` 不会。

## 三、强度折算与入库

情绪只接受有原文证据的估计，置信度不足时退回中性，不给全库加噪声：

```
strength a = clamp(arousal, 0, 1) × clamp(confidence, 0, 1)     # 无证据 → confidence=0 → a=0
```

`valence ∈ [-1,1]`，`arousal ∈ [0,1]`。**决定保持的是唤醒度而非情绪正负**；valence 只用于半衰期方向、敏感判定与检索时的情绪一致性。`snapshot` 记录 `valence / arousal / confidence / strength / at / evidence`，写入后冻结。

## 四、三个作用入口

### ① 编码增强（入库一次）

```
W0 = clamp(W_base × (1 + encoding_boost × a))
```

默认 `encoding_boost = 0.8`。**上限钳在 `protected_weight - 1`**：高唤醒不能意外获得 W≥90 的永久保护，也不能把已被显式赋予更高权重的条目拉低。

### ② 衰减速率调制（每 tick，但放进速率）

```
H      = charge_half_life_days × 24 × (1 + arousal_half_life_gain × a)
         × (negative_valence_half_life_factor 当 valence < 0)
charge(t) = charge0 × 2^(-t/H)
λ_eff(t)  = λ_base × profile_mult / (1 + rate_gain × charge(t))
```

`charge0` 是上次结算时的负荷。L2 的半衰期由冻结的 L1 快照推导：当时越强烈，负荷留得越久；负性情感平均消退更快。

**解析积分（保证 tick 无关）**：令 `c = ln2/H`，`A = rate_gain × charge0`，则

```
∫₀ᵀ dt/(1 + A·e^(-ct)) = T + (ln(1 + A·e^(-cT)) - ln(1 + A)) / c
```

在 T1 处拆分，第二段以 `A1 = A·e^(-cT1)` 起算，两项对数恰好望远镜相消，结果与一次结算完全相等。代码用 `log1p` 形式避免大 T 溢出。无情绪时积分退化为 `T`，与升级前逐位一致。

**禁止**把情绪每 tick 乘到权重上——那会让执行频率改变情绪强度，重蹈原报告 `B(Q)` 与 `profile["mult"]` 的覆辙。情绪只能进初始强度或速率。

### ③ 情绪一致性检索（每次查询）

```
base  = relevance_weight × relevance + memory_weight × (W / max_weight)
score = (1 - mood_weight) × base + mood_weight × mood_similarity     # 仅当提供 mood 时
```

`mood_similarity ∈ [-1,1]`，为当前用户情绪与记忆情绪的相似度，由调用方计算。未提供时严格退回原公式。情绪一致性只是排序信号，仍需先满足相关性阈值，且与权限、revision 过滤叠加，不得越权注入。

## 五、回充与再评价

- **真人 remind**：先结算到当前时刻，再 `charge += remind_recharge × (a - charge)`，默认 0.7，即向原始强度回补 70%，永不超过 `a`。同时 `last_evoked_at` 更新。
- **Agent 自己的 used**：只结算，不回充。
- **appraise（新增 action）**：要求真实人类事件、非空文本、私密条目的 owner 校验。写入新 revision，旧 current 进入 `history`（含证据与时间），并按 `reappraisal_charge_relief`（默认 0.25）比例降低 charge。**不加权、不改快照、不改事实原子。** 历史解释保留在权威库，不进 RAG 投影。

## 六、注入门控

`injection_allowed(memory_id, now, user_initiated)`：

- 显式 `sensitive=true`，或 `valence<0` 且当前 `charge ≥ auto_sensitive_charge`（默认 0.6）→ **禁止主动注入**，仅在用户自己提起（`user_initiated=True`）时允许。
- 其余情况允许。charge 随时间降到阈值以下后，自动恢复可主动注入。
- 该门控只负责“要不要主动提”，不负责“能不能查到”。权限过滤仍由原有 visibility/owner 规则执行。

## 七、配置默认值（工程起点，均未标定）

| 键 | 默认 | 含义 |
|---|---:|---|
| `enabled` | true | 关闭后情绪不参与衰减与编码 |
| `encoding_boost` | 0.8 | 编码增强幅度 |
| `rate_gain` | 1.5 | 负荷对速率的调制强度，a=1 时初期速率 ×0.4 |
| `charge_half_life_days` | 60 | 负荷基础半衰期 |
| `arousal_half_life_gain` | 2.0 | 当时唤醒度对半衰期的拉长 |
| `negative_valence_half_life_factor` | 0.6 | 负性情感半衰期缩短系数 |
| `remind_recharge` | 0.7 | 真人再提醒的回补比例 |
| `reappraisal_charge_relief` | 0.25 | 再评价带来的负荷缓解 |
| `auto_sensitive_charge` | 0.6 | 高负性负荷的自动敏感阈值 |
| `mood_weight` | 0.1 | 情绪一致性在排序中的权重 |

这些数值是可解释的工程默认，不是心理学标定结果。调整时必须保留公式形式与变更记录，并重新运行测试。

## 八、验收（已自动化）

`scripts/test_memory_engine.py` 的 `EmotionTests` 覆盖：

1. 无情绪/零置信时投影与衰减与升级前一致。
2. 编码增强封顶在保护阈值之下，且仍正常衰减。
3. 情绪存在时衰减更慢，且 240h 一次结算与两次 120h 结算**精确相等**。
4. 十年后瞬时衰减率回到基线（护盾过期，而非永久保护）。
5. 快照在夜间维护与 appraise 之后逐字段不变。
6. appraisal 只增 revision、保留历史、不给权重加成；历史不进投影。
7. appraise 拒绝传输回执伪造、越权私密访问与空文本。
8. 真人 remind 回充，Agent 自己的 used 不回充。
9. charge 归零后核心事实原子与快照仍在、仍 active、仍可被用户主动检索。
10. sensitive 与高负性负荷只拦主动注入，不拦用户主动发起。
11. 情绪排序分可选，未提供 mood 时与原公式一致，越界输入被钳制。
12. 情绪字段类型与取值范围校验。

## 九、边界

- 本模块实现的是记忆工程中的情绪参数化，**不构成心理评估或医疗建议**，不得用于推断用户的心理疾病或危机状态。
- 情绪估计必须来自用户原话证据；模型复述、第三方转述不得作为高置信来源。
- 原始聊天存档与情绪快照属于既有留存与保密策略，`private/owner` 约束在 appraise、回充、合并、恢复中一律保留。

## 1.4.1：遗忘后的抽象情绪余韵

本章 L1 / L2 / L3 的正常记忆行为不变。最终自然 purge 可把事先验证的固定场景枚举与已结算数值写入独立 `emotion_residue`，随后清完整事件语义；它不是保留旧 snapshot 或 appraisal。明确 owner-only `erase` 清本条余韵及事件语义。未提供 cue 或旧记录来源未知则不生成。原始事件已忘时，感受不能用作历史事实的证据。

输入、权限、衰减、迁移与宿主接线边界见 [1.4.1 升级说明](upgrade-1.4.1.md)。关系台账 / 禁区 / 未闭合线程不属于该残留表，也不被这些操作清除。
