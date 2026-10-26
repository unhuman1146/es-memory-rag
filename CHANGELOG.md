# 更新说明

## 1.1.0 · 2026-10-26

**新增参考适配器，把 `RAGAdapter` 契约落成可运行代码。**

发布内容：

- `adapters/` —— 参考实现（5 个模块，纯标准库）：
  - `adapters/embedding.py` —— embedding 客户端：`SiliconFlowEmbedding`（OpenAI 兼容 `/v1/embeddings`）、
    `OllamaEmbedding`（本地 `/api/embed`，**不需要 key**）、`HashingEmbedding`（离线测试桩）；
    key 解析顺序为 参数 > `SILICONFLOW_API_KEY` > 仓库外的 `~/.es-memory-rag/config.json`
  - `adapters/vector_store.py` —— 纯标准库 SQLite 向量库：分块存储、余弦检索、ACL 预过滤
  - `adapters/rerank.py` —— `SiliconFlowReranker`（bge-reranker-v2-m3）与词法降级替身
  - `adapters/rag_adapter.py` —— `RAGAdapter` 契约实现（`upsert`/`delete`/`search`/`rerank`/`count`）
  - `adapters/sync.py` —— outbox 消费者：被新 revision 取代的事件记为 skipped 并确认；真实执行才确认；
    失败调用 `fail()` 并按 `outbox_max_attempts` 进死信；**没有实际执行绝不确认**
- `references/adapters.md` —— 接线文档：两种 embedding 后端、凭据处理、契约对应表、
  同步语义、相关性阈值标定、验证方式
- `scripts/test_adapter.py` —— **40 项**离线单测（不联网、不用 key，CI 也跑）
- `scripts/demo_live_rag.py` —— 真机验收脚本（需要 key 与网络，**不在 CI**）

本版同时补上契约里原本漏掉的一条：**先过相关性阈值再排序**（`min_relevance`），
拦住「权重很高但完全不相关」的记忆被强塞进上下文。阈值按模型标定，文档给了
bge-m3 + 短中文事实投影的实测值（0.45）并明确要求换模型后重新标定。

**凭据处理**：key 只进 `Authorization` 请求头，不进仓库、不进索引、不进错误信息
（有专门断言守着）。仓库根目录下的 config.json 已被 .gitignore 排除。

**未改变**：记忆生命周期的数值与语义（λ 表、压缩边界、抽签、回收期、outbox 契约）一个都没改。
原 56 项测试全部仍然通过，现共 **96 项**（引擎 39 + 预算 17 + 适配器 40）。

## 1.0.0 · 2026-10-26

**首个公开版本（GitHub 开源）。**

发布内容：

- `SKILL.md` —— 行为规范：记忆分级、权重曲线、压缩与删除状态机、情绪三层、状态层与上下文预算的硬边界
- `GUIDE.md` —— 施工入口：适用性判据 → 六步施工顺序（每步带完成判据）→ 最小可用集 → 数据模型 → 参数推导 → 症状到病因 → 终局验收
- `references/` —— 6 份展开文档：`design` / `integration` / `emotion` / `state-ledger` / `context-budget` / `evidence`
- `scripts/memory_engine.py` —— Python 3.11+ 标准库参考引擎：真实 SQLite 事务、权重计算、证据原子投影、传输去重、强化、每日稳定抽签、并发版本保护、RAG outbox、回收清理，以及情绪三层状态
- `scripts/context_budget.py` —— 上下文组装模块（HARD/SOFT 两条线、缓存前缀顺序、批量淘汰、多模态记账）
- `scripts/test_memory_engine.py` / `scripts/test_context_budget.py` —— 共 **56 项**测试（引擎 39 + 预算 17），全部通过
- `assets/` —— 配置样例、systemd service/timer 接线模板、两个事件样例

本版同时修掉一处工程缺陷：参考引擎在 Windows 上按 locale（cp936/GBK）写 stdout，
调用方按 UTF-8 读取会抛 `UnicodeDecodeError` 并丢掉输出尾部。已在两个脚本内强制 UTF-8 输出。

### 开发期迭代编号（未单独发布）

正式公开前内部用过 v2 / v3 / v4 三个编号，功能已全部并入 1.0.0：

| 内部编号 | 主题 | 实际改动 |
|---|---|---|
| v2 | 情绪三层 | L1 历史快照 / L2 当前负荷 / L3 评价版本；编码增强、解析积分速率调制、`remind` 回充、`appraise` 版本化、`injection_allowed` / `rank_score` |
| v3 | 运维耐久性 | 无变化不写（apply_plan 四元组未变则不 bump revision）；WAL + busy_timeout 并发；outbox 死信与 `fail` / `retry_dead` / `sweep_acked`；抽签与计划签名两把独立 seed；`--now` 需 `ES_MEMORY_REPLAY=1` |
| v4 | 状态层与上下文预算 | 新增 `references/state-ledger.md`（关系台账 / 未闭合线程 / 禁区 / 当前任务，永不压缩不衰减不抽签）；新增 `references/context-budget.md`（512K 硬顶 / 256K 软线）；新增 `references/evidence.md`（34 次实测与被推翻的假设）；新增 `scripts/context_budget.py` 与 17 项测试 |
