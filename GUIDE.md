# 施工指南

> 这是入口文件。一个没有上下文的执行者（人或 agent）读完这一份，按顺序做完六步，就应当得到一套能跑、能自证、能交接的记忆系统。
>
> 其他文档是这一份的展开：`references/design.md`（公式与状态机）、`references/integration.md`（接线契约）、`references/state-ledger.md`（状态层四件套，陪伴型必读）、`references/context-budget.md`（总预算两条线）、`references/emotion.md`（情绪三层）、`references/evidence.md`（实测数字与未验证边界）、`scripts/memory_engine.py` 与 `scripts/context_budget.py`（参考实现，可对照行为）。

## 0. 先判断该不该做

**不要做**，如果满足任意一条：

- 长期记忆总量长期低于几千条 —— 全文检索 + 会话摘要就够，衰减与淘汰都是空转。
- 用户回访间隔远大于衰减半衰期（例如用户一个月来一次，而 `fact` 半衰期约 29 天）—— 衰减此时等价于随机删除。
- 单次任务型使用（做完即止，不期待跨会话连续性）—— 用按需检索加文件系统，不要记忆库。
- 拿不到稳定身份 ID —— 先去解决身份，否则去重、ACL、提醒强化全是假的。
- 全部历史都能塞进上下文窗口 —— 不需要检索层。

**只做审计**，如果：已有记忆系统，只想核对纪律。直接跳到第 6 节，逐条对照现状，不写代码。

**要做**，继续往下，按顺序施工，不许跳步。

## 1. 施工顺序

六步，每步都有「完成判据」。判据不过，不许进下一步。

跳步的代价是具体的：S1 没跑通就写衰减公式，最后发现投影格式错了，全部重来；S3 没接通就做注入，最后无法区分“没召回”和“没索引”。

### S1 骨架：库能打开，表齐全

产出物：一个 SQLite 文件，六张表齐全，WAL 已启用。

```text
python scripts/memory_engine.py --db mem.db init
python -c "import sqlite3;d=sqlite3.connect('mem.db');print(sorted(r[0] for r in d.execute(\"select name from sqlite_master where type='table'\")),d.execute('pragma journal_mode').fetchone()[0])"
```

**完成判据**：输出 `['audit','daily_draws','events','memories','meta','outbox','sqlite_sequence'] wal`（`sqlite_sequence` 是 AUTOINCREMENT 自带的，忽略即可）。

- 六张业务表不全 → 你用的不是本仓库的建表语句，见第 3 节 DDL。
- 不是 `wal` → 主读路径会和夜间批写互锁，`PRAGMA journal_mode=WAL` 必须落地。

**此时不要**：调 LLM、接向量库、写衰减公式。这一步只验证持久化。

### S2 入口：消息进得来，重放不重复

产出物：一条真实事件 → 一条记忆 + 一条 outbox 待办。

```text
python scripts/memory_engine.py --db mem.db event assets/example-event.json
python scripts/memory_engine.py --db mem.db event assets/example-event.json
```

**完成判据**：第一条 `status` 为 `ok`、`changed` 为 `1`；第二条 `status` 为 `duplicate`、`changed` 为 `0`；`outbox` 只有一行。

这一条同时验证了三件事：事实原子必须引用本次事件 ID、120 秒内容去重生效、事务没有留下半个影子。**三者任一失效，后面所有统计都不可信。**

**此时不要**：让主聊天模型实时整理记忆。入口只接受受信接收器封装的事件。

### S2b 状态层与交接（陪伴型必做，任务型可跳过）

产出物：一个 < 500 tokens 的状态块 + 固定的注入位置 + 一次可执行的交接流程。

状态层四件套见 `references/state-ledger.md`：关系台账 / 未闭合线程 / 禁区清单 / 当前任务。

**完成判据**：

1. 状态块每轮都在上下文**最前面**，且总量 < 500 tokens。
2. **禁区违反率 = 0**：连续对话 10 轮，一次都没主动提禁区话题。
3. 换窗口后：称呼与语气与换之前一致；未闭合线程仍被追问。
4. 台账被引用：抽查 5 轮，至少 3 轮实际用到了台账条目。

**此时不要**：把状态放进摘要层；让模型自己推断关系状态；把禁区做成"记住了但可能被压缩"的普通记忆。

### S3 索引：outbox 真的被消费，且只在确认后 ack

产出物：一个真实的 `RAGAdapter` 实现 + 一个能跑的契约冒烟脚本。

先用假的存储验证**契约**（这一步不验证厂商 API）。把下面这段存成 `scripts/adapter_smoke.py`，从 skill 根目录运行 `python scripts/adapter_smoke.py`：

```python
# adapter_smoke.py —— 只验证 outbox 契约，不验证 embedding 质量
from memory_engine import MemoryEngine, load_config

class FakeStore:
    def __init__(self): self.docs = {}
    def upsert(self, memory_id, revision, text, metadata):
        self.docs[memory_id] = {'revision': revision, 'text': text}; return True
    def delete(self, memory_id): self.docs.pop(memory_id, None); return True
    def count(self, memory_id): return 1 if memory_id in self.docs else 0

eng, store = MemoryEngine('mem.db', load_config()), FakeStore()
for job in eng.outbox():
    row = eng.row(job['memory_id'])
    if not row or row['revision'] != job['revision']:
        eng.acknowledge(job['id']); continue          # 已被新 revision 取代
    ok = (store.delete(job['memory_id']) if job['operation'] == 'delete'
          else store.upsert(job['memory_id'], row['revision'], row['text'],
                            {'visibility': row['visibility'], 'owner': row['owner']}))
    if ok: eng.acknowledge(job['id'])                  # 只有真写完才 ack
assert store.count('demo:preference:001') == 1
print('contract ok')
```

**完成判据**：打印 `contract ok`；`outbox` 变为空。

然后换成真实向量库重跑同一条脚本，再加三条硬断言：

1. 同义改写查询能命中该 `memory_id`。
2. 强制删除后 `count(memory_id) == 0`。
3. 断开索引后新事件仍进 outbox，恢复后能同步，且重启不重复加权。

**此时不要**：把 `outbox` 有记录当成“已接通”。有记录只代表待办已写。

### S4 检索与注入：模型不用自己调工具也能拿到记忆

产出物：查询构造 + 先权限过滤 + 真实 rerank + 按预算注入。

**完成判据**（四条，缺一不可）：

1. 同义查询命中，且 reranker 返回的是真实响应（不是原序）。
2. 主 LLM 在没有任何检索 tool-call 的情况下收到了记忆投影。
3. 无权限条目（`private` 且 owner 不匹配）不进上下文。
4. 高权重但与当前话题无关的记忆**不**被注入。

用参考实现的两个钩子校准行为：

```python
eng.injection_allowed(memory_id, now, user_initiated=False)  # False = 不许主动提
eng.rank_score(relevance, weight, mood_similarity=None)      # 不传第三个参数 = 原公式
```

`adapters/chat_path.py` 是这一段的**参考实现**：查询构造 → 权限过滤检索 →
按记忆去重 → 真实 rerank → 门控 → 联合排序 → 预算裁剪 → 注入块 + 缺失声明。
接自己的检索系统时照它的顺序和口径接，别自造排序或门控。
一个实测出来的注意点：**发送者稳定 ID 放进查询会毁掉相关性区分度**
（每条投影的 subject 就是那个 ID，查询带上它等于跟每条候选共享 token，
标定好的阈值会失效、权重项会开始主导排序）。人物信息应当用在权限过滤和排序加权上，
详见 [references/adapters.md](references/adapters.md)。

**此时不要**：为了"不漏"而放大 `top_k` 或预算。注入预算每轮都在花，长任务里它是净成本。

### S5 语义仓管：模型判断价值，代码执行数值

产出物：独立上下文的 Curator 队列 + 结构化提案 + 校验后入库。

**完成判据**：

1. 一条**未被回复**的用户消息也进入了记忆判断队列。
2. 桥接层重放的同一条消息不产生第二次建档或第二次强化。
3. 模型给出的 `weight` 不能越过 89；`W≥90` 只能来自受信管理操作。
4. 模型提出的删除/压缩计划**不能**直接传给 `apply_plan`（会被签名拒绝）。

**此时不要**：让 Curator 复制主聊天的全部思维，也不要让它自带数值运算权。

### S6 夜间调度：真的在凌晨跑过，有日志有覆盖率

产出物：一个独立 worker + 调度器 + 每夜报告。

```text
python scripts/memory_engine.py --db mem.db maintain          # 预览，不写
python scripts/memory_engine.py --db mem.db maintain --apply  # 提交
python scripts/memory_engine.py --db mem.db sweep             # 清扫 ack 回执（预览）
```

**完成判据**：

1. 次日凌晨有 worker 日志，报告含 `processed` 与 `unchanged` 两个数。
2. 连续两夜跑，**归档条目、保护条目、已到 floor 的条目 revision 不涨、outbox 不增**。
3. 同一当地日期只跑一次抽签；预览与 apply 结果一致。
4. 时区正确：`*` 号单位不要写成不带时区的 02:00。

**此时不要**：用“配置文件存在”或“一次手动调用成功”证明定时生效。

**注意**：`--now` 只在隔离回放时使用，必须设 `ES_MEMORY_REPLAY=1` 才生效。

### S7（可选）情绪三层与规模压测

只有在“用户在意它记得我”的产品里才做。见 `references/emotion.md`。
规模：`scripts/test_memory_engine.py` 含 3000 条 plan+apply 回归，自建库请按自己的量级放大十倍复测一次。

## 2. 最小可用集

第一版只做 MUST。做完 MUST 就有一套能用的系统；SHOULD 决定它是否耐用；OPTIONAL 决定它是否有“人味”。

| 项 | 级别 | 说明 |
|---|---|---|
| 表结构 + WAL | **MUST** | S1 |
| 事件入口 + 120s 去重 + 证据引用 | **MUST** | S2 |
| **状态层四件套（关系/线程/禁区/任务）** | **MUST（陪伴型）** | S2b，见 state-ledger.md |
| **上下文预算：HARD/SOFT 两条线** | **MUST** | 见 context-budget.md |
| outbox + 真实 ack | **MUST** | S3 |
| 检索前权限过滤 + 注入前再核对 | **MUST** | S4 |
| 投影压缩保留否定/主体/时间/来源 | **MUST** | 压缩只降档，不改义 |
| 全部终局验收（第 6 节） | **MUST** | 否则不算交付 |
| 夜间仓管 + 无变化不写 | SHOULD | S6 |
| 真实 rerank | SHOULD | 缺它必须显式报 degraded |
| 事实版本（correction / supersede） | SHOULD | 参考实现**未提供**，需自行设计 |
| 三层情绪 | OPTIONAL | S7 |
| **随机抽签删除** | **不推荐** | 收益不成立，且不可解释。用 `archived` + 检索降权替代 |
| **六档 λ** | **不推荐** | 无标定依据。第一版用一档 λ + `profile` 表达重要性 |
| ACT-R 排序先验 | OPTIONAL | 有访问日志后再考虑 |

## 3. 数据模型

直接使用下面这份 DDL，不要自由发挥字段名——适配器契约与测试都依赖它。

```sql
CREATE TABLE memories(
 id TEXT PRIMARY KEY, body TEXT NOT NULL, facts TEXT NOT NULL, source_ids TEXT NOT NULL,
 weight REAL NOT NULL, curve TEXT NOT NULL, profile TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
 visibility TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL, last_decay REAL NOT NULL,
 revision INTEGER NOT NULL, tier TEXT NOT NULL, text TEXT NOT NULL, maintenance_day TEXT,
 deleted_at REAL, purged INTEGER NOT NULL DEFAULT 0, emotion TEXT NOT NULL DEFAULT '');
CREATE TABLE events(id TEXT PRIMARY KEY, peer TEXT NOT NULL, hash TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL);
CREATE INDEX events_transport ON events(peer, hash, at);
CREATE TABLE daily_draws(memory_id TEXT NOT NULL, day TEXT NOT NULL, draw REAL NOT NULL, deleted INTEGER NOT NULL, PRIMARY KEY(memory_id, day));
CREATE TABLE outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
 operation TEXT NOT NULL, payload TEXT NOT NULL, ack INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
 dead INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL DEFAULT 0, UNIQUE(memory_id, revision));
CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT, action TEXT NOT NULL, at REAL NOT NULL, detail TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- lottery_seed / plan_seed 必须分开
```

三条不可协商的约束：

- `facts` 是事实原子数组，每个原子必须有 `subject / predicate / object / time / negated / source_ids`，且 `source_ids` 必须包含产生它的那条事件 ID。
- `text` 是**投影**（给 RAG 用），`body` 是原文（给权威库用）。二者不能混用。
- `private` 只是应用层过滤，落库是明文。要真保密，另做磁盘或字段级加密。

## 4. 参数怎么定

**别照抄示例值。** 每个参数都给推导方法，示例值只作量级参考。

| 参数 | 怎么定 |
|---|---|
| `λ`（每小时） | 取同一事实被重提的间隔中位数 `T_days`，设「希望它在 T 天内从 50 掉到 30」：`λ = ln(50/30) / (T_days × 24)`。示例：T=90 天 → λ≈0.000066；T=30 天 → λ≈0.0002 |
| `initial_weight` | 只是刻度，本身无意义。真正决定行为的是它相对 60/30/10 三个阈值的位置 |
| `full/facts` 阈值（60/30） | 由「一条记忆在多少权重时还需要细节」决定；先用 60/30，按注入命中率调 |
| `delete_below`（10） | 若保留抽签则该值越低越安全；不保留抽签时它只是「进入归档区」的门槛 |
| `reminder_boost`（+10） | 由「一次确认应当延长多少天寿命」反推：`boost = W × (e^(λ·Δh) - 1)`；Δh 填你希望的续命小时数 |
| `top_k` / `context_token_budget` | 取模型窗口的 10%–15%，再除以单条投影平均长度。示例：8k 窗口 → 800–1200 tokens → `top_k=6` |
| `relevance_weight / memory_weight` | 相关性必须主导。0.8/0.2 是合理起点；若你把权重调到 0.5 以上，等于让“旧”压过“对” |
| `dedup_seconds`（120） | 由桥接层重放窗口决定，不要超过 300，否则会误杀真人复述 |
| 情绪 `encoding_boost` / `rate_gain` | **无标定方法**。只能在 `[0, 0.8]` 与 `[0, 1.5]` 内取值，且编码增强不得越过 89 |

任何自行调整都必须记录：改了什么、依据是什么、重跑了哪条测试。

## 5. 症状 → 病因

| 你看到的现象 | 真正的原因 |
|---|---|
| 每晚 outbox 暴涨、全库重同步 | 没做“状态未变则跳过”，维护在给未变化的条目 bump revision |
| 归档的记忆每晚仍产生 delete 事件 | 同上；或 `_queue` 对 archived 无条件产 delete |
| 权重集体涨到 101 | 强化用了乘法，或把“被查询/被检索”当成强化信号 |
| 上线后衰减比预期快得多 | profile 倍率放在了每 tick 上，执行频率改变了寿命 |
| 预览和 apply 结果不一致 / 同一夜删了两次 | 抽签用了 `random()` 而不是 `Hmac(seed, day\|id)` 的稳定抽签 |
| 用户说“你怎么不记得”，但库里明明有 | 投影把关键原子压掉了（丢失否定/主体）；或 query 构造没带人物/话题 |
| 压缩后语义反了（“不喜欢”变“喜欢”） | 用了摘要式压缩或截断，而不是字段白名单投影 |
| 私聊内容出现在群里 | 只在注入前过滤、检索前没过 ACL；或投影里带了 owner 以外的线索 |
| “已删除但模型还在提” | 向量删除没等确认，或检索后没有回权威库过滤 deleted/旧 revision |
| 同一条记忆被反复强化 | 没做 120 秒去重，或 `peer_id` 没含发送者，导致两个群友同一句话被当成两次提醒 |
| 凌晨任务从来没跑过 | 配置存在 ≠ 调度生效：时区写法、unit 名、路径、运行用户逐项核对 |
| 情绪记忆每夜重同步 | 投影里放了连续变化的量（如 charge）。它应当从权威库实时结算 |
| 一条坏事件卡住整条队列 | 没有 `attempts`/死信机制，失败被无限重试 |
| 换窗口后语气变礼貌、称呼变全名 | 状态层没注入，或注入了但排在上下文中间 |
| 提了明确说过不要提的事 | 禁区被放进了摘要层，压缩时抹平（**禁区违反率必须 0**） |
| 反复问"你上次说的那个" | 未闭合线程缺失，或被当成普通记忆衰减掉了 |
| 模型答对了但顺带抖出无关的事 | 输出纪律问题，不是长上下文衰减；多字段并存时尤其明显 |
| 每轮都全库重同步 / 固定开销吃掉一半预算 | 前者见无变化不写；后者是工具 schema 常驻，应瘦身而非加窗口 |
| 预算表算得好好的，实际总是超 | tokenizer 系数没校准。实测常见 1 token ≈ 1.5 汉字，本地按 1 字 1 token 会高估 33% |

## 6. 终局验收

交付前逐条打勾，任一条不过就不算交付。

1. **未回复的消息也进入记忆判断**；重复消息不重复写入、不重复强化。
2. **衰减可重放**：同段时长拆成多 tick 与一次 tick 结果一致；快照类字段不被普通读取重置。
3. **边界正确**：`W=10` 不抽签、`W<10` 才抽签；同日多次运行不重抽；90/pinned/permanent 受保护。
4. **压缩保义**：低权重条目的主体、否定、关系、时间/未知、来源均不改义。
5. **真实 RAG**：同义查询命中、rerank 真实响应、无 tool-call 也收到投影、高权重不相关条目不注入。
6. **删除闭环**：向量 `count(memory_id)=0`；索引断网时 outbox 保留，恢复后同步，重启不重复加权。
7. **真实调度**：次日凌晨有日志与覆盖率；连续两夜无变化条目零 revision 增长、零 outbox 增长。
8. **状态层（陪伴型）**：禁区违反率为 0；换窗口后称呼与语气一致、未闭合线程仍被追问；台账在抽查中被实际引用。
9. **预算**：tokenizer 已实测校准；缓存前缀未被打碎（检索与工具结果都在变量区）；历史淘汰按批而非逐轮。

**离线单测通过、演示库能跑、文档齐全，这三样都不能替代上面任何一条。**

## 7. 交接清单

交付时写清六件事，缺一件接手人就会重新踩一遍：

1. 公式版本与全部自定义参数（含改动依据）。
2. 已验证了第 6 节的哪几条，用什么方式验证的。
3. 真实 RAG 与调度的当前状态：接通 / 降级 / 未接。
4. 未完成的适配项（典型：事实版本管理、情绪估计）。
5. 数据规模与实测耗时（plan+apply 在多少条上跑多久）。
6. 已知风险：明文存储、死信未处理、降级路径。
