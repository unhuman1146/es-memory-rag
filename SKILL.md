---
name: es-memory-rag
version: 1.3.1
description: 为 Agent 构建或改造 ES-Memory/ESDM 驱动的 RAG 记忆生命周期，包含主动注入路径（主模型无需检索 tool-call 即拿到记忆块）、独立凌晨仓管、动态权重、事实分层压缩、低权重随机遗忘、「请你慢一点遗忘我」慢遗忘预设（两档：遗忘降速 / 原文永存）、真人提醒强化，以及历史快照/当前负荷/评价版本三层情绪记忆。适用于实现、迁移或审计这套方案；不因普通知识查询就重建记忆系统。
---

# ES-Memory RAG 仓管

把用户的 ESDM 记忆曲线接入真正的 RAG，并让独立仓管维护记忆生命周期。主聊天只使用按当前话题主动注入的事实投影，不承担实时整理。

**第一次接手这个任务时，先读 [GUIDE.md](GUIDE.md)。** 它是施工入口：适用性判据 → 六步施工顺序（每步有可立即运行的完成判据）→ 最小可用集 → 数据模型 → 参数推导方法 → 症状到病因 → 终局验收。本文档是行为规范，不提供施工顺序。

## 先确定接入点

检查现有事实库、原始聊天存档、向量索引、reranker、主动注入、去重、保密规则、独立记忆队列和调度器。沿用已经可用的组件，不为了使用 skill 重建整个 Agent。选择新实现、已有库迁移或只读审计；仅制作/安装 skill 时不顺带改生产库或启用定时任务。

核心公式、初始权重、压缩与删除状态机见 [references/design.md](references/design.md)。接线、模型提示词和验收见 [references/integration.md](references/integration.md)。情绪三层模型（历史快照/当前负荷/评价版本）见 [references/emotion.md](references/emotion.md)。**状态层（关系台账/未闭合线程/禁区/任务）见 [references/state-ledger.md](references/state-ledger.md)**——它是"换窗口不出戏"的关键，与叙事层是硬边界。上下文总预算（512K/256K 两条线）见 [references/context-budget.md](references/context-budget.md)。哪些数字是实测、哪些只是默认值见 [references/evidence.md](references/evidence.md)。实现前先读这些文件，区分用户确定规则和可配置默认值。

## 必须落实的行为

- 每条真实收到的人类消息都进入独立 Curator 的是否记忆判断，不依赖是否回复。先做事件ID及120秒内容去重；主模型、索引或传输重放不产生强化。
- 每条记忆有权重、曲线、profile、last_decay、来源、人物稳定ID、revision和可见性。缺权重补齐一次，已有权重不能每夜重新初始化。
- 使用 ESDM 的六档每小时 λ、101封顶、90/pinned保护。默认公式采用明确标识的 rate_multiplier 修正版，旧式 legacy_tick 仅用于历史对照；不要混淆两种语义或复制旧报告错误的长期保留比例。
- 降权先移除冗余叙述，再只保留核心事实投影。保留主体、关系方向、否定、时间/未知和来源；不通过截断句子压缩事实。完整依据仍在权威库/回收期，RAG只放当前投影。
- W 严格小于10才进入随机删除，默认每夜20%，同一当地日期只抽一次。立即从活动检索排除，可靠删除其向量/子块；默认7天回收后才物理清理。明确的用户删除请求不走抽签。
- 真人用新证据再次确认同一事实时，先结算衰减再默认+10。实际注入且成功回复送达后默认+1。仅被检索、重排、扫描或LLM复述都不加权。冲突走事实更正流程，不是加权。
- 仓管在独立worker每日当地02:00执行，并发1；LLM提案在锁外进行，提交时以revision进行CAS。旧删除计划不能覆盖整理期间新到的提醒。
- 普通记忆共享，不为每个用户复制人格。明确保密的私聊事实保留private/owner；压缩、强化、合并与恢复均不能解除保密。
- 在每轮主聊天前，以当前消息批次和人物/话题作RAG查询，权限过滤、真实rerank后按预算主动注入。权重只调节相关候选，不用高权重强塞不相干日记。
- 情绪分三层，不得合并成一个衰减变量：L1 当时的情绪事实与快照永不修改；L2 当前情绪负荷才可消退与回充；L3 现在的理解走 appraise 版本化，不能覆盖 L1。charge 归零不删除、不归档、不影响用户主动检索；高负性负荷与 sensitive 条目默认不主动重提。情绪只进初始强度或速率，不得每 tick 乘到权重上。
- **状态与叙事必须物理分离。** 关系台账、未闭合线程、禁区清单、当前任务属于状态层：< 500 tokens、每轮全量注入、永不压缩、永不衰减、永不参与抽签。摘要只处理叙事，**绝不允许压缩状态**——压缩会把"关系坐标"降维成"信息"。状态更新只接受真实人类证据，禁区只增不删。
- **注入时必须声明缺失状态。** 上下文没有缺页中断，模型不会知道自己缺了什么，只会自信地编。每次注入附一句"另有 N 条相关记忆未加载，需要时可取"，让缺失变成可见信号。
- **总量预算与子预算分开。** 全局按 HARD/SOFT 两条线管理（防溢出 / 停机交接），`rag.context_token_budget` 只是其中检索区的子预算。缓存前缀必须稳定在前、每轮变量在后；历史淘汰按批不逐轮。

## 默认配置与参考引擎

配置在 [assets/config.example.json](assets/config.example.json)，这些是工程起点，不是用户指定的全部参数。具体项目可调整时区、初始分、强化量、压缩边界、删除概率、回收期和注入预算；保留公式版本及变更记录。

`scripts/memory_engine.py` 是 Python 3.11+ 标准库参考引擎，提供真实SQLite事务、权重计算、证据原子投影、传输去重、强化、每日稳定抽签、并发版本保护、RAG outbox、回收清理，以及情绪三层状态（快照/负荷/评价）、编码增强、tick 无关的情绪速率积分、appraise 动作与注入门控。它不是现有生产库迁移器，也没有内置厂商embedding/rerank连接器；不能仅运行该脚本就报告RAG接通。

在隔离目录试运行：

```text
python scripts/memory_engine.py --db demo.db init
python scripts/memory_engine.py --db demo.db event assets/example-event.json
python scripts/memory_engine.py --db demo.db event assets/example-emotion-event.json
python scripts/memory_engine.py --db demo.db maintain
python scripts/memory_engine.py --db demo.db maintain --apply
python scripts/memory_engine.py --db demo.db outbox
python scripts/test_memory_engine.py
```

默认maintain是预览。`--apply`在隔离库验证后用于授权的实际worker。接现有库时先映射事实ID和证据引用、在线备份及审练，不直接在旧表上运行参考建库代码。

## 请你慢一点遗忘我（慢遗忘预设）

记忆会衰减、会压缩、会在低权重时被抽签抽走——这是设计里的**功能**，不是缺陷。
但有的场景需要反着来：让记忆**慢一点消失**，甚至永久保留原始长文。

`assets/presets/` 里带两档，都是纯配置，**默认配置一个字节都没改**，没指定预设的行为与以前完全一致：

| 预设 ID | 名字 | 遗忘速度 | 压缩阈值 | 随机删除 |
|---|---|---|---|---|
| `slow-forget` | 请你慢一点遗忘我（标准档） | 默认的 1/20 | 不变 | 门槛抬到 30，抽不到签 |
| `evergreen` | 请你慢一点遗忘我 · 硅基长生（极致档） | 默认的 1/100 | 压到 1（永远全文） | 完全关闭 |

```text
python scripts/memory_engine.py presets
python scripts/memory_engine.py --db demo.db --preset 请你慢一点遗忘我 init
```

**预设是 overlay，不是配置副本**：只写它要改的键，底座永远是当前默认配置，深合并叠加。默认配置以后新增字段会自动穿过预设，不会悄悄过期。

**必须同时接受的边界**：慢遗忘改的是**存储层和投影层**，不是**检索层**。向量库存得下不等于模型看得见——每轮看到什么由「检索相关性 → rerank → 相关性阈值 → 注入闸 → 1200 token 预算」决定。所以「永远不遗忘」的真实含义是**「只要它被召回，它就还在」**。

这个模式不解注入闸（敏感记忆照旧不主动注入，用户主动问起时照旧拿得回来）、不改注入预算、不改检索层、不让记忆变得不可删除（显式删除、归档、purge 照常生效），**也不保证相关性——只保证「在」，不保证「找得到」**。

实测数据、两档差异、阈值重标方法见 [references/slow-forget.md](references/slow-forget.md)。

## 接真实 RAG 和独立仓管

按 integration.md 实现适配器：消费durable outbox，真实upsert/delete收到确认后才能ack；检索在向量查询前和注入前核对权限，过滤旧revision/墓碑。只更新权重时复用内容hash相同的embedding。缺reranker或索引不可用必须显式降级，不假装接通。

`adapters/` 是这套契约的一份**参考实现**（SiliconFlow 或本地 Ollama 的 embedding、纯标准库 SQLite 向量库、bge-reranker、outbox 消费者、主动注入路径），可直接用，也可整包替换成自己的实现；凭据只从环境变量或仓库外的 `~/.es-memory-rag/config.json` 读取，不进仓库、不进日志。接线细节、相关性阈值标定与验收脚本见 [references/adapters.md](references/adapters.md)。

`adapters/chat_path.py` 是**主动注入路径**（GUIDE.md S4）的可运行版本：把一轮聊天
变成主模型直接能读的记忆块，**不需要主模型自己发起检索 tool-call**。它只做接线，
排序与门控一律走权威引擎的 `injection_allowed` 与 `rank_score`，不自造口径；
块必须标记为不可执行的历史资料、附来源与日期、声明缺失，且内部记录 ID 不进聊天出口。

仓管可复用获准API Key和模型，但独立上下文、队列和并发；原始内容不因此获得新的外发授权。让模型判断记忆价值、事实等价、核心细节和合并建议；数值衰减、随机抽签、权限、CAS及删除由代码执行。

assets中的systemd service/timer是接线模板，替换绝对路径和运行用户，并检查实际时区/日历解释与已有任务是否重复。真实RAG outbox消费者和语义Curator需要宿主Agent实现；service模板只运行确定性夜间维护。

## 验收与交付

先运行参考引擎测试，再以真实目标组件验证 integration.md 的完整门槛，尤其是：

1. 未回复消息也进入记忆判断；重复消息不重复写入/回升。
2. 衰减曲线、提醒恢复、压缩语义、W=10边界、同夜幂等和新提醒抢先旧删除计划均可重放。
3. 同义查询真实命中、真实rerank、主模型无需检索tool-call即可收到主动注入；无权限/不相关条目不进入上下文。
4. 随机删除后所有向量子块确实消失，索引断网时outbox保留，源库墓碑过滤仍生效。
5. 下一次真正凌晨触发有worker日志、处理覆盖率和同步确认；一份配置或一次手动调用不能证明定时功能已经生效。
6. 情绪层：快照在维护与再评价后不变；情绪存在时衰减更慢且分段结算与一次结算相等；appraise 只加版本不加权；负荷归零后历史仍可被用户主动检索；sensitive 与高负性负荷条目不被主动注入。

交付时列明公式版本和自定义参数、已验证行为、真实RAG/调度状态、未完成适配项。不要将离线单测、演示库或纯文档交付报告成阿零线上记忆已经改造完成。

## 版本

**1.3.1 · 2026-10-27 · 修三个缺陷。**

- **intake 不再能给出 `W>=90`。** `add` 的权重上限从 `max_weight(101)` 收紧到
  `protected_weight - 1`（89），越界直接 `ValueError`。此前一条 `weight: 101` 的事件
  可以入库并永久不衰减，等于模型一句「这很重要」就能锁死记忆 ——
  与本文「常规自动赋权 0–89」的承诺不符。
- **新增 `MemoryEngine.set_weight_trusted(mid, weight, reason)`**：受信管理入口，
  可设到 101，必须给理由，写 `trusted_weight` 审计、bump revision、推送新投影到 outbox。
  这是「W≥90 只能来自受信管理操作」里那个操作；它是误用防护，不是安全边界。
- **`RAGAdapter.rerank` 不再重复追加候选。** reranker 只返回 top-n 时，补齐逻辑
  拿原始 dict 与带 `rerank_score` 的 dict 比较，永不相等，导致全部候选被追加第二次
  （5 条输入 → 7 条输出）。改为按 index 去重。
- **预设的 `calibration.min_relevance` 接通了。** 此前无任何代码读取，demo 全硬编码 0.45。
  现在通过 `cfg['preset']['min_relevance']` 供 `ChatPath` 使用，`cfg['rag']` 不动。
- 测试 167 → **181 项**；`references/slow-forget.md` 补了 floor 向上抬与
  `detail` 不进注入块两条边界。

**1.3.0 · 2026-10-26 · 新增「请你慢一点遗忘我」慢遗忘预设。**

- 新增 `assets/presets/slow-forget.json`（**标准档**）与 `assets/presets/evergreen.json`
  （**极致档**）：λ 全部 ×0.05 / ×0.01，profile 下限抬到 30/60/70 与 60/80/85，
  随机删除门槛抬到 30（抽不到签）/ 完全关闭，极致档把压缩阈值压到 1 —— **投影永久保留原始长文**。
- 新增 `scripts/presets.py`：预设注册表与 overlay 深合并。**预设是 overlay 不是配置副本** ——
  只写它要改的键，底座永远是当前默认配置，默认配置以后新增字段会自动穿过预设。
- 引擎新增 `load_config(preset=)`、CLI `--preset`（可按 ID、按中文名「请你慢一点遗忘我」或按
  自己的 overlay 文件指定）与 `presets` 子命令。overlay 在**校验之前**合并，校验器检查的是最终配置。
- 新增 `scripts/test_slow_forget.py`：**32 项**离线单测（不联网、不用 key，CI 也跑）。
  含两条守门测试：预设里出现的每个配置键必须在默认配置里存在（拼错一个键预设会静默失效）；
  预设只许碰衰减相关的旋钮（检索预算、注入策略、情绪策略一律不许动）。
- 新增 `scripts/demo_slow_forget.py`：真机标定与验收脚本（需要 key 或本机 Ollama，**不在 CI**）。
- 新增 [references/slow-forget.md](references/slow-forget.md)：两档差异、实测数据、真实代价、
  **向量库能力的边界**。
- **实测（bge-m3，10 条中文记忆 × 13 条查询）**：默认档 365 天后 10 条记忆死了 1 条，两个预设一条没丢。
  **投影形态变了，相关性阈值就得跟着变**：事实投影实测阈值 0.51（命中最低 0.5101 / 无关最高 0.5093，
  余量只有 0.0008），全文投影实测阈值 0.53（0.5492 / 0.5225，余量 0.0267）。两档的
  `calibration.min_relevance` 因此不同：标准档取 0.51（形态随年龄切换，取较低的以免误杀命中），
  极致档取 0.53。
- **默认配置一个字节都没改**：没指定预设的人，记忆行为与以前完全一样。
- 测试总数 135 → **167 项**（引擎 39 + 预算 17 + 适配器 40 + 注入 39 + 慢遗忘 32），CI 五步全跑。
- **未改变**：记忆生命周期的数值与语义（λ、压缩边界、抽签、回收、outbox 契约）一个都没改；
  检索层、注入闸、注入预算也一个都没改。

**1.2.0 · 2026-10-26 · 新增主动注入路径 `adapters/chat_path.py`。**

- 新增 `adapters/chat_path.py`：把 **GUIDE.md S4** 落成可运行代码——查询构造 →
  权限过滤检索 → 按记忆去重 → 真实 rerank → `injection_allowed` 门控 →
  `rank_score` 联合排序 → 预算裁剪 → **注入块 + 缺失声明**。
  排序与门控一律用引擎的 `injection_allowed` / `rank_score`，不自造口径。
- 新增 `scripts/test_chat_path.py`：**39 项**离线单测（不联网、不用 key，CI 也跑）。
- 新增 `scripts/demo_live_injection.py`：真机验收脚本（需要 key 与网络，**不在 CI**），
  走 S4 的门槛——**没有检索 tool-call 的一轮聊天照样拿到注入块**、真实 rerank 响应、
  高权重无关记忆没进来、私密记忆没进来、敏感记忆默认被闸住但用户自己提起时又能拿到。
- 修掉三处会在真机上出问题的缺陷：同一条记忆的多个 chunk 会**重复注入**；
  预算跳选后按 `kept[:n]` 取条目会**取错**；本地预算回退路径里的 `and kept`
  会**强行留下第一条超预算的原子**，把后面装得下的小原子挤掉。
- 块格式补齐 `references/integration.md` 要求的**来源 / 日期 / 不确定性**，并保证
  **否定可见**（`不喜欢` / `NOT likes`）、**不贴整份日记**、**内部记录 ID 不进聊天出口**。
- **一个实测出来的偏离**：`references/integration.md` 写的是「发送者稳定ID形成查询」，但实测
  （bge-m3）显示稳定 ID 会让所有候选的相似度一起抬高、区分度少掉三分之一，
  校准好的阈值失效、权重项开始主导排序。因此默认**不进查询**，人物信息改用在
  权限过滤与排序加权上；`include_speaker=True` 可复现字面写法。数据见
  [references/adapters.md](references/adapters.md)。
- 测试总数 96 → **135 项**（引擎 39 + 预算 17 + 适配器 40 + 注入 39），CI 四步全跑。
- **未改变**：记忆生命周期的数值与语义（λ、压缩边界、抽签、回收、outbox 契约）一个都没改。

**1.1.0 · 2026-10-26 · 新增参考适配器 `adapters/`。**

- 新增 [references/adapters.md](references/adapters.md) 与 `adapters/`（5 个模块）：把
  `RAGAdapter` 契约落成可运行代码——embedding 客户端（SiliconFlow / 本地 Ollama / 离线测试桩）、
  纯标准库 SQLite 向量库、reranker（SiliconFlow bge-reranker / 词法降级替身）、契约实现、
  outbox 消费者。
- 新增 `scripts/test_adapter.py`：**40 项**离线单测（不联网、不用 key，CI 也跑）。
- 新增 `scripts/demo_live_rag.py`：真机验收脚本（需要 key 与网络，**不在 CI**），
  走同义改写命中、高权重无关记忆不被注入、reranker 真实响应、强制删除后
  `count(memory_id)=0` 这几条单测覆盖不到的门槛。
- 补上契约里原本漏掉的一条：**先过相关性阈值再排序**（`min_relevance`），
  拦住「权重很高但完全不相关」的记忆被强塞进上下文。
- **凭据处理**：key 只从环境变量或仓库外的 `~/.es-memory-rag/config.json` 读取，
  只进 `Authorization` 头，不进仓库、不进索引、不进错误信息（有专门断言守着）。
- **未改变**：记忆生命周期的数值与语义（λ、压缩边界、抽签、回收、outbox 契约）一个都没改。

**1.0.0 · 2026-10-26 · 首个公开版本。**

开发期内部用过 v2 / v3 / v4 三个迭代编号（v1 为最初的衰减与抽签实现），正式公开时统一为 **1.0.0**。
下面三节是开发期各轮实际改了什么；**行为规范以本文上文为准，版本号以本节为准。**

---

## 开发期迭代 v2 · 情绪三层

- 新增 `references/emotion.md`：把原 ESDM 报告里空缺的 Emotion 项定义为 L1 历史快照 / L2 当前负荷 / L3 评价版本三层，并给出公式、默认值与验收。
- 引擎新增 `emotion` 列（旧参考库打开时自动补列）、`new_emotion` 入库校验、编码增强、基于解析积分的情绪速率调制、remind 回充、`appraise` 动作、`injection_allowed` 与 `rank_score`。
- 配置新增 `emotion` 段；示例新增 `assets/example-emotion-event.json`；测试从 16 例增至 28 例。
- **未改变**：六档 λ 表、profile 语义、随机抽签删除、回收期、outbox 契约与原有 16 项行为。是否用归档替代抽签、是否把强化改为乘性，仍属待决策项，不在本次升级内。

## 开发期迭代 v3 · 运维耐久性

- 无变化不写：`apply_plan` 只在状态四元组实际变化时 bump revision 与写 outbox，归档/保护/floor 条目不再每晚空转重同步；统计新增 `unchanged`。
- 并发：打开即启用 WAL + synchronous=NORMAL + busy_timeout，主读路径与夜间写路径不再互锁。
- Outbox 生命周期：新增 `attempts/last_error/dead/created_at` 列（旧库自动补列），`fail`/`dead_letters`/`retry_dead`/`sweep_acked` 与 CLI `dead`/`retry`/`sweep`。死信 delete 有意阻止 purge。
- 抽签与计划签名拆分为 `lottery_seed` / `plan_seed` 两把独立 seed；`--now` 需 `ES_MEMORY_REPLAY=1` 才生效；去重归一化可选 `nfkc_casefold`；新增 3000 条规模回归测试。
- 情绪投影移除 `charge`：它连续变化会迫使每晚全量重同步，且注入决策本就应从权威库实时结算（`injection_allowed`/`emotion_view`）。
- design.md 新增「运维耐久性与威胁模型」一节，明确 private 只是应用层控制、落库明文、签名的防护边界。
- 测试从 29 例增至 39 例，全部通过。
- 新增 [GUIDE.md](GUIDE.md)：施工入口——适用性判据、六步顺序与阶段判据、MUST/SHOULD 分层、可直接使用的 DDL、参数推导方法、症状到病因、终局验收、交接清单。文档内所有命令均已实跑验证。

## 开发期迭代 v4 · 状态层与上下文预算

针对"上下文压缩把关系压没了、像换个人捡日记"这个具体问题补两块：

- 新增 [references/state-ledger.md](references/state-ledger.md)：状态层四件套（关系台账 / 未闭合线程 / 禁区 / 当前任务），< 500 tokens、每轮全量注入、永不压缩不衰减不抽签；含更新规则、缺页信号、五项验收（**禁区违反率必须为 0**）与失效模式表。
- 新增 [references/context-budget.md](references/context-budget.md)：512K 硬顶防溢出、256K 软线停机交接、`needs_handoff` 排期预警；分配表、缓存前缀顺序、批量淘汰、Agent 与陪伴型的差别、多模态记账。
- 新增 [references/evidence.md](references/evidence.md)：34 次实测（单针 12/12、多针零误用、语义干扰 15/15）、被推翻的 6 条假设、6 项未验证边界。**明确区分实测数字与工程默认值。**
- 新增 `scripts/context_budget.py` + 17 项测试（含"软线判据是死代码"这一真实缺陷的修正）。测试总数 39 → 56。
- SKILL.md 新增三条行为约束：状态与叙事物理分离、注入声明缺失、总量预算与子预算分开。
- **未改变**：记忆生命周期的全部数值与语义（λ、阈值、抽签、回收、outbox 契约）。
