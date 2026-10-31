# ES-Mind 1.4.1 升级与复核

中文展示名：**墨渊·Agent识海集成系统**；英文：**ES-Mind**。这是原 ES-Memory RAG 的新品牌，管理 Agent 的长期记忆、情绪关联与上下文工作集，不宣称意识。本次没有仓库 / 包 / Skill / 导入 / CLI / 服务标识迁移，交付文件名继续使用 `es-memory-rag-1.4.1`。

此补丁基于 `ecce9d71ffc5695c8fb18cac4680233a65739621`（1.4.0）。默认 λ、权重、投影阈值、情绪衰减公式、注入 token 预算和状态台账策略不变。

## 自然遗忘、情绪余韵与明确删除

本次区分“忘了事情的细节，还留下感受”和“明确彻底删除”。打开旧库只增量添加
`memories.deletion_kind`（默认 `unknown`）、`emotion_cues`（默认空数组）及独立
`emotion_residue` 表；没有破坏式迁移。

- 普通压缩、衰减、换页：保留 L1 历史与 L3 评价版本，L2 按原公式衰减；状态台账不变。
- 自然抽签删除：来源记为 `natural`，回收期内原情绪仍存，真人提醒仍可恢复。墓碑不进入普通情绪视图或 RAG。
- 到期且向量删除已确认后的自然 purge：在同一写事务内结算 L2，保留经验证的抽象场景词与数值，然后清除本条原文、事实、来源、完整情绪 JSON（包括评价历史）及审计自由文本。
- 明确 `erase`：立即逻辑清除对应事件语义和该条情绪残留，不能用提醒恢复；其他记忆与关系不变。外部索引仍须消费 delete 并确认。
- 旧记录来源不明或没有事先提供场景词：不猜测、不从原文自动提取，也不从旧 appraisal / evidence 推导关联；不生成残留，以固定审计策略代码记录。旧 purge 的语义残留在下一次显式执行 purge 时清理。

场景词只允许 `rain`、`music`、`departure`、`reunion`、`care`、`conflict`、
`achievement`、`loss` 八个低信息量枚举，宿主可在已验证真人的 add 提案里提供：

```python
action['emotion'] = {'valence': -.5, 'arousal': .7, 'confidence': 1,
                     'cues': ['rain', 'music']}
```

不能提供姓名、地点、日期、原话、任意标签或其 hash。任意文本 hash 仍可匹配具体事件，不能代替匿名化。
残留表只存内部删除定位 ID、枚举 cue、valence、charge、结算时间、固定半衰期以及 ACL；不存任何 snapshot、appraisal、事件证据 ID 或可还原原事的文本。内部关联 ID 和 ACL 只用于定向删除与权限隔离，不返回调用者。

```python
feelings = engine.emotion_resonance(['rain'], requester='verified-owner-id')
# {'feelings': [{'cue': 'rain', 'valence': ..., 'charge': ...}], 'notice': ...}
result = engine.erase('internal-memory-id', requester='verified-owner-id')
# {'status': 'erased', 'revision': ..., 'delete_pending': True/False}
```

`emotion_resonance` 只按精确场景词返回允许访问的聚合数值，带“仅感受，不能据此推断历史事实”的声明，不返回事件 ID、来源、历史或条目数。多条同 cue 的 valence 按当前 charge 加权，charge 取最大值，避免重复记录放大负荷。残留按 purge 时保存的半衰期闭式指数衰减，读取频率和重启不改变结果；它不强化记忆、不新增事实、不自动触发模型。宿主必须显式接入场景与感受接口，**本补丁不宣称自动触景生情已经接通**。

ACL 同时核对残留保存时和当前源墓碑的 visibility / owner；private 只对 owner 可见。
`erase` 对 shared 也要求非空、匹配的 owner；owner 缺失则拒绝。
requester 字符串不是身份认证，宿主必须核验真实用户和明确删除请求；本参考引擎没有管理角色授权系统，不把未知 owner 放宽给任意管理员。

erase 在 `BEGIN IMMEDIATE` 中清对应残留及源语义、推进 revision，清旧 outbox payload / last_error 的自由文本，再排队当前 revision 的 delete。重试不会重复推进 revision 或重复排队；`delete_pending` 只表示该 delete 尚未确认。旧 upsert 按已有 supersession 规则跳过。并发 purge 与提醒共享 SQLite 写锁：先提交的提醒保留数据；先提交的 purge 使随后提醒明确失败。

这些是**应用与 SQL 层逻辑清除**，不等于取证级安全擦盘。SQLite 空闲页 / WAL、备份、快照、外部向量落盘及已经发出的请求或缓存有独立保留周期。宿主须处理索引确认、并发同步 worker、缓存与备份策略；本补丁不自动 VACUUM、不访问真实库、不承诺抹除所有物理副本。

## 向量身份与旧索引

向量复用现在同时要求投影 hash、全部 chunk 文本、向量维度和
`provider / model / version / dim` 一致。仅更新权重、ACL 或 revision，在身份不变时仍复用向量。
身份保存在 chunk metadata 的保留键 `_embedding_identity`；调用者不能覆盖它。

内置后端提供 `cache_identity()`。provider 含后端与端点位置的摘要；URL 用户名、密码、查询串、fragment、API key 不进入身份。`model_version` 默认为字符串 `1`。
同名模型换权重、部署或预处理时，部署者**必须更新** `client.model_version`，或在仓库外配置的 `embedding.version` 中指定稳定版本。软件无法从同一模型名字推知服务端权重已变。

自定义 embedding 可实现 `cache_identity()`，或传入：

```python
adapter = RAGAdapter(embedding, store, engine=engine,
    embedding_identity={"provider": "my-local-backend", "model": "my-model",
                        "version": "weights-2026-10", "dim": 768})
```

这四个值必须是非秘密标识；不要放 token、用户数据或含凭据的 URL。
只有 `embed()` 的旧自定义后端仍可写入和检索：适配器生成本实例临时身份，并从向量推定维度。
它不能跨实例安全复用旧缓存；重启应配置稳定身份并重建索引。

**旧索引缺身份时不猜测兼容性。** 单条 `upsert` 会重新 embedding；查询旧索引、混合模型索引或维度不符时明确报错，ChatPath 报检索失败并继续本轮聊天，不注入混用的向量结果。
不要仅消费 outbox 以为完成模型迁移：已 ack 的旧条目不会自动重新排队。

推荐停下索引写入 / 服务切换，在**新索引路径**建立当前模型索引；从权威库遍历全部活动、未删除、未 purge 的记忆，逐条 `upsert`，确认数量与抽样召回，再由宿主切换索引路径。保留旧路径用于回滚，按宿主数据保留政策处理。此步骤会调用你选用的 embedding 后端；本次补丁验证只用离线 HashingEmbedding，没有执行付费重建。

重建核心代码（在宿主已创建 `engine`、`embedding` 的受信运维环境中运行）：

```python
from adapters.vector_store import SqliteVectorStore
from adapters.rag_adapter import RAGAdapter
from adapters.sync import _metadata_from_row

new_store = SqliteVectorStore("index-current-model.new.db")  # 必须是新的空文件
assert new_store.total() == 0
new_adapter = RAGAdapter(embedding, new_store, engine=engine)
for record in engine.db.execute("""SELECT * FROM memories
        WHERE deleted_at IS NULL AND purged=0 AND profile!='archived'"""):
    row = dict(record)
    result = new_adapter.upsert(row['id'], row['revision'], row['text'],
                                _metadata_from_row(row, {}))
    assert result['ok']
new_store.close()
```

示例不改源库或 outbox，也不自动切换生产路径。宿主须确保快照一致并在切换前补齐重建期间的变更；普通请求继续执行权威 ACL / revision 检查。

## 候选数与检索边界

参考向量库为适配器按 memory_id 取最高相关 chunk，然后应用候选 limit。
直接调用 `SqliteVectorStore.search()` 仍默认返回 chunk，保持原接口。
RAGAdapter 在权威 ACL / revision / 墓碑 / 相关性过滤后扩大窗口补拉；ChatPath 再为自身去重、已注入排除、相关性与敏感情绪门控补拉。
检查仍在外部 rerank 前和注入前执行；一个已失效候选不能借重排复活。

两层各自有 `search_scan_limit=4096` 的扫描上限，支持配置；不再固定取 3 倍后立即放弃。
旧自定义 store / adapter 仍只需原来的 `search(query, acl_filter, limit)` 接口；忽略增大 limit 时以无进展条件终止。
参考 SQLite 搜索依旧是全量计算向量相似度，上限限制候选窗口，不是数据库扫描时间或大规模 ANN 性能承诺。

候选窗口与上下文 token 预算不同；本补丁不扩大注入预算。
命中窗口、模型相关性、阈值、ACL、情绪门控及预算都可能使其他记忆本轮不可见。
“本轮检索候选已加载”只描述这一轮，**不保证全库召回完整**。

## 实验与部署边界

旧模型实验的 34 / 15 / 75 次口径矛盾和额外无关输出，见 [evidence.md](evidence.md)。没有原始逐次证据的结论标为待核实。
离线 HashingEmbedding 只是可重复测试桩，不提供语义召回质量证据。实际 embedding / reranker、线上 sidecar、模型 token 预算和凌晨调度仍需宿主单独验收。
