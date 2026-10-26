# ES-Memory RAG 仓管

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![版本](https://img.shields.io/badge/%E7%89%88%E6%9C%AC-1.0.0-5b8c7a)](https://github.com/unhuman1146/es-memory-rag/releases)
[![测试](https://img.shields.io/badge/%E6%B5%8B%E8%AF%95-56%20%E9%A1%B9%E5%85%A8%E8%BF%87-2ea043)](https://github.com/unhuman1146/es-memory-rag/actions)

把用户的 ESDM 记忆曲线接进真正的 RAG，并让**独立仓管**维护记忆生命周期。
主聊天只使用按当前话题主动注入的事实投影，不承担实时整理。

> **给 Agent 用：** 这是一个 Skill 包。把 `es-memory-rag/` 整个目录放进你的 Agent 的 skills 目录即可。
> 接入已有记忆系统前请先读 [GUIDE.md](GUIDE.md) 第 0 节——**不适用就明确不做**，
> 别为了用 skill 去重建整个 Agent。

## 它解决什么问题

大多数「让 AI 记得住」的方案把重点放在**存**，这里把重点放在**生命周期**：

- 记忆有**权重、曲线、档案**：随着时间衰减，可按重要性保留细节或压缩成核心事实投影
- 压缩是**有损的**，所以关键约束是「不丢主体、关系方向、否定、时间与来源」——不能把「甲不喜欢 X」压成「甲喜欢 X」
- 状态与叙事**物理分离**：关系台账、未闭合线程、禁区清单、当前任务属于状态层，**永不压缩、永不衰减、永不参与抽签**
- 上下文压缩会把关系降维成信息，所以正确的顺序是**把有损从写入时挪到读取时**：写入时保原文，读取时少给
- 上下文没有缺页中断——模型不知道自己缺了什么，只会自信地编。所以注入必须**声明缺失**：「另有 N 条相关记忆未加载」
- 衰减、抽签、权限、CAS、删除全部由**代码**执行；模型只判断价值与事实等价

## 快速开始

```sh
git clone https://github.com/unhuman1146/es-memory-rag.git
cd es-memory-rag

# 建库（六张业务表 + WAL）
python scripts/memory_engine.py --db demo.db init

# 灌一条事件；同一条再跑一次会被 120 秒传输去重挡掉
python scripts/memory_engine.py --db demo.db event assets/example-event.json

# 夜间维护：默认是预览，--apply 才写
python scripts/memory_engine.py --db demo.db maintain
python scripts/memory_engine.py --db demo.db maintain --apply

# 待同步给 RAG 适配器的事件
python scripts/memory_engine.py --db demo.db outbox

# 测试：共 56 项
python scripts/test_memory_engine.py
python scripts/test_context_budget.py
```

需要 Python 3.11+，**零第三方依赖**（仅标准库）。

## 目录结构

```
es-memory-rag/
├── SKILL.md          行为规范（先读这份）
├── GUIDE.md          施工入口：适用性判据 → 六步施工顺序 → 验收
├── references/
│   ├── design.md           规则、公式与状态模型
│   ├── integration.md      RAG / 独立仓管接线契约与验收门槛
│   ├── emotion.md          情绪三层（L1 快照 / L2 负荷 / L3 评价）
│   ├── state-ledger.md     状态层四件套与失效模式
│   ├── context-budget.md   512K 硬顶 / 256K 软线
│   └── evidence.md         哪些是实测、哪些是默认值
├── scripts/
│   ├── memory_engine.py    参考引擎（SQLite 事务、outbox、抽签、CAS）
│   ├── context_budget.py   上下文组装与预算
│   └── test_*.py           56 项测试
└── assets/
    ├── config.example.json
    ├── es-memory-warehouse.service / .timer   systemd 接线模板
    └── example-event.json / example-emotion-event.json
```

## 两条最容易踩的边界

1. **参考引擎不等于 RAG 已接通。** 它不做 embedding、不做 rerank、不调模型；outbox 有记录只代表**待办已写**。
   真实接通需要你自己实现 `RAGAdapter`（`upsert` / `delete` / `search` / `rerank` / `count`），
   远端确认之后才 ack。
2. **缺 reranker 或索引不可用必须显式报 degraded**，不能把「没重排」当成「已重排」。

完整验收门槛见 [GUIDE.md 第 6 节](GUIDE.md)。

## 哪些数字是实测的

[references/evidence.md](references/evidence.md) 明确区分了两类数字，**不要把默认值当结论引用**：

- **已实测**（某商用长上下文接口，温度 0，合计 34 次调用）：tokenizer 系数 1 token ≈ 1.5 汉字；单针召回 12/12；语义干扰 15/15 严格正确、误取 0；延迟 2.9–21.6s 且不随长度增长
- **未标定的工程默认值**：六档 λ、60/30 压缩边界、20% 日删除概率、情绪参数

另外，本包的 56 项测试是**离线单测**：它们证明参考引擎的行为符合规范，**不证明**你的线上 RAG 或凌晨调度已经生效。

## 许可

MIT，见 [LICENSE](LICENSE)。

第三方引用见 `NOTICE.md`。
