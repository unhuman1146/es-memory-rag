# 参考适配器：把记忆库接到真实 RAG

`adapters/` 是**参考实现**，不是必须使用的组件。它把 `references/integration.md`
里的 `RAGAdapter` 契约落成可运行的代码，你完全可以整包替换成自己的实现。

```
adapters/
├── embedding.py       embedding 客户端（SiliconFlow / 本地 Ollama / 离线测试桩）
├── vector_store.py    纯标准库 SQLite 向量库（分块存储 + 余弦检索）
├── rerank.py          reranker（SiliconFlow bge-reranker / 词法降级替身）
├── rag_adapter.py     RAGAdapter 契约实现
└── sync.py            消费 outbox：写索引 → 确认 / 失败 / 死信
```

## 为什么参考引擎不自带 embedding

参考引擎只负责**记忆生命周期的确定性部分**（权重、衰减、压缩、抽签、CAS）。
语义判断（这条记忆值不值、两条是不是一回事）交给模型，向量检索交给检索系统。
所以引擎只产出 **outbox**，不直接调用向量 API —— outbox 里有记录只代表
**待同步已写**，不代表 RAG 已接通。

## 两种 embedding 后端

| provider | 端点 | 凭据 | 备注 |
|---|---|---|---|
| `siliconflow` | `POST /v1/embeddings`（OpenAI 兼容） | 需要 API key | 默认 `BAAI/bge-m3`，1024 维 |
| `ollama` | `POST /api/embed` | **不需要 key** | 本机推理，内容不出网 |

两者都只用 Python 标准库（`urllib`），与本包「零第三方依赖」一致。
`urllib` 默认会读 `HTTP_PROXY`/`HTTPS_PROXY`，本实现显式忽略代理环境变量，
避免本机代理没启动时整条链路卡死。

## 凭据从哪来（**绝不能进仓库**）

按这个顺序解析：

1. 构造函数参数 `api_key=`
2. 环境变量 `SILICONFLOW_API_KEY`
3. `~/.es-memory-rag/config.json`（**仓库外**的私有文件）

配置样例（放在仓库外）：

```json
{
  "embedding": {
    "provider": "siliconflow",
    "model": "BAAI/bge-m3",
    "dim": 1024,
    "rerank_model": "BAAI/bge-reranker-v2-m3"
  },
  "siliconflow_api_key": "sk-..."
}
```

key 只出现在 `Authorization` 请求头里；错误信息只包含状态码、主机名和响应体，
**不含 key**（`scripts/test_adapter.py` 里有针对这一点的断言）。
仓库根目录下的 config.json 已被 .gitignore 排除。

## 四步接上

```python
import sys
sys.path.insert(0, '.')                     # 从技能根目录运行
from memory_engine import MemoryEngine, load_config
from adapters.embedding import build_embedding
from adapters.rerank import SiliconFlowReranker
from adapters.rag_adapter import RAGAdapter
from adapters.sync import sync_once
from adapters.vector_store import SqliteVectorStore

engine  = MemoryEngine('memory.db', load_config())
store   = SqliteVectorStore('index.db')
adapter = RAGAdapter(
    build_embedding(),                      # 读 ~/.es-memory-rag/config.json
    store,
    engine=engine,                          # 权威库：检索时必须回查
    reranker=SiliconFlowReranker(),         # 没有它就明确报 degraded
    min_relevance=0.45,                     # 阈值按模型校准，别照抄
)

report = sync_once(engine, adapter)         # 消费 outbox
print(report)

hits = adapter.search('那个人平时在哪个城市工作', {'requester': 'qq:10001'}, limit=6)
ranked = adapter.rerank('那个人平时在哪个城市工作', hits)
print(ranked['status'], ranked['candidates'])
```

## 契约对应

| 契约方法 | 本实现 | 关键语义 |
|---|---|---|
| `upsert(memory_id, revision, text, metadata)` | `adapters/rag_adapter.py` | 分块 → embedding → 落库；**投影文本没变就不重新算向量**（只有权重/ACL 变时复用） |
| `delete(memory_id)` | `adapters/rag_adapter.py` | 删掉该记忆**所有** chunk 和**所有**旧 revision |
| `search(query, acl_filter, limit)` | `adapters/rag_adapter.py` | 向量检索后**回查权威库**，剔除已删/已回收/已归档/旧 revision/无权限条目；`score=.8*relevance+.2*(weight/101)` |
| `rerank(query, candidates)` | `adapters/rag_adapter.py` | 有模型才 rerank；**没有就报 `degraded`**，绝不把「没重排」当成「已重排」 |
| `count(memory_id)` | `adapters/rag_adapter.py` | 该记忆当前索引里的 chunk 数，删除后必须为 0 |

## 同步语义（`adapters/sync.py`）

- 事件已被**更新的 revision** 取代 → 记为 `skipped_superseded` 并确认，不重放旧内容
- 记忆在权威库里已消失 → 删掉它的向量并确认
- `operation='delete'` 或记忆已归档/已删 → 删掉**全部**向量块并确认
- 真实 upsert → 真的 embedding、真的写索引，成功后才确认
- **任何一次失败都调用 `engine.fail(event_id, error)`**：队列保留事件、记 `attempts`，
  达到 `outbox_max_attempts` 进死信。**没有实际执行就绝不确认**
- 死信的 delete 会阻止对应记忆 purge —— 未确认的删除不允许静默过期

## 相关性阈值

`score` 把相关性和记忆权重混在一起，所以**先过相关性阈值，再排序**。
`min_relevance` 的值取决于模型和语料：

- `bge-m3` + 短中文事实投影：约 `0.45`（本仓库 `scripts/demo_live_rag.py` 的实测值）
- 换模型、换语言、换投影风格后**必须重新标定**，别照抄，也别硬套别家模型的原始分数

阈值的作用就是拦住「权重很高但完全不相关」的记忆 —— 它不该被塞进上下文。

## 验证

```sh
# 40 项离线单测：不联网、不用 key、CI 也会跑
python scripts/test_adapter.py

# 真机验收：需要 key 和网络，不在 CI
python scripts/demo_live_rag.py
```

`scripts/demo_live_rag.py` 走的就是 `references/integration.md` 的验收门槛里
单测覆盖不到的那几条：同义改写命中、高权重无关记忆不被注入、reranker 真实响应、
强制删除后 `count(memory_id)=0` 且源库后过滤立刻不再返回旧命中。
