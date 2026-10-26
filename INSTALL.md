# 安装：把 es-memory-rag 装进你的 Agent

[中文](README.md) | [English](README.en.md) — 英文速览就在下一段

> **English quick version**
> 1. Copy the **whole** `es-memory-rag/` directory into your agent's skills
>    directory (the folder that holds `SKILL.md` files).
> 2. The directory name **must** equal the `name:` in `SKILL.md` frontmatter.
> 3. Keep `references/` and `scripts/` next to `SKILL.md` — the doc links and
>    the reference engine both live there.
> 4. Verify: ask the agent "what is the name and description of the
>    es-memory-rag skill?", and/or run `python scripts/test_memory_engine.py`
>    (expect `Ran 39 tests ... OK`).
> Paths per agent are listed below; entries marked ✅ were verified on a real
> machine, entries marked 📌 are common conventions that were **not** verified here.

---

## 一、三条铁律

### 1. 整个目录一起复制，不能只拿 `SKILL.md`

`SKILL.md` 里全是**相对链接**（`references/design.md`、`references/emotion.md` …），
`scripts/` 里是**跑得起来的参考引擎**。只复制 `SKILL.md` 会得到一个「看起来装好了、
实际上点开全是死链、闸也跑不了」的技能。

要复制的是下面这些（`.git*` 与 `.github/` 是仓库管道，技能目录里不需要）：

```
es-memory-rag/
├── SKILL.md          ← 必须
├── LICENSE
├── README.md         ← 中文说明
├── README.en.md      ← 英文说明
├── INSTALL.md        ← 本文件
├── CHANGELOG.md
├── NOTICE.md         ← 第三方声明（本包无第三方代码）
├── references/       ← 6 份展开文档
├── scripts/          ← 参考引擎 + 96 项测试 + 真机验收脚本
├── adapters/         ← 参考适配器（embedding / 向量库 / rerank / outbox 消费）
├── assets/           ← 配置样例、systemd 模板、事件样例
└── agents/           ← openai.yaml
```

> 最少可用的组合是 `SKILL.md` + `references/` + `scripts/`；
> 上面是**完整**复制清单（推荐）。只做记忆库、不接 RAG 时可以不带 `adapters/`。

### 2. 目录名必须等于 frontmatter 里的 `name`

```
es-memory-rag/            ← 目录名
└── SKILL.md
    ---
    name: es-memory-rag   ← 必须与目录名完全一致
    ---
```

很多 loader 直接拿目录名去匹配 `name:`，不一致就**静默不加载**——不报错，只是技能
不出现。这是「装了半天不生效」最常见的原因。

### 3. 装完要能自证，不能靠"我觉得装好了"

见下面「四、验证安装」。

---

## 二、路径

### ✅ 本机实测存在的落点

| Agent | skills 目录 | 备注 |
|---|---|---|
| **Codex CLI** | `~/.codex/skills/<skill-name>/` | 本机实测；`~/.codex/skills/.system/` 是内置技能，别往里放 |
| **通用 skills 落点** | `~/.agents/skills/<skill-name>/` | 本机实测存在（`ui-craft` 在这） |
| **工具仓** | `~/.hub/skills/<skill-name>/` | 本机实测存在 |
| **MiniMax Code（Mavis）** | `<dataDir>/agents/<agent>/skills/<skill-name>/` | 本机实测；`<dataDir>` 是 Mavis 的数据目录（随安装而变，**不要硬编码**） |

> Windows 上 `~` = `C:\Users\<用户名>`。
> MiniMax Code 的 dataDir 位置随安装而变，用 `mavis` 命令或看会话工作区同级目录确定，
> **不要硬编码**。

### 📌 常见约定（本机未安装，未验证）

| Agent | 约定路径 | 说明 |
|---|---|---|
| Claude Code | `~/.claude/skills/<skill-name>/` | 用 `SKILL.md` 格式 |
| Gemini CLI | `~/.gemini/` | 主要是 `GEMINI.md` 规则文件，**不是** skill 目录 |
| Qwen Code | `~/.qwen/` | 同上 |
| Cursor | `<项目>/.cursor/rules/*.mdc` | **规则**目录，不是 skill 目录 |
| Windsurf / Cline | `<项目>/.windsurfrules` / `.clinerules` | 同上 |

> ⚠️ 后三类是**规则**机制（把规则文本喂给模型），不是 skill 机制。
> 本包的行为规范可以整份导出成 `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` /
> `QWEN.md` / `.cursor/rules/*.mdc` / `.windsurfrules` / `.clinerules` ——
> 但那属于**导出**能力，产出的是规则文本，不是可执行的 skill 目录。

---

## 三、三种典型失败

| 症状 | 病因 | 修法 |
|---|---|---|
| 技能列表里**根本没有**它 | 目录名 ≠ `name:`，或根本没放进 skills 根目录 | 对齐目录名；确认落在**根**目录下而不是根目录里散着 |
| 技能**出现了**，但一用就断链 / 闸跑不了 | 只复制了 `SKILL.md` | 把 `references/` `scripts/` `assets/` 一起补齐 |
| 技能出现了，但**行为没变** | 上下文里压根没注入，或被更早的同名条目遮蔽 | 问它"es-memory-rag 的 description 是什么"，对不上就是没加载 |

---

## 四、验证安装

**方法 A（不需要执行权限）**——直接问 Agent：

> 你的技能列表里有没有 `es-memory-rag`？把它的 name 和 description 原样念给我。

对上 `SKILL.md` frontmatter 里的那两行，就算装好了。

**方法 B（更硬）**——在**安装目录里**跑一次测试：

```sh
cd <你的 skills 目录>/es-memory-rag
python scripts/test_memory_engine.py     # 应输出 Ran 39 tests ... OK
python scripts/test_context_budget.py   # 应输出 Ran 17 tests ... OK
python scripts/test_adapter.py          # 应输出 Ran 40 tests ... OK
```

这一步能同时验证四件事：目录结构完整、Python ≥ 3.11、参考引擎真能跑、参考适配器真能跑。
跑不过就是没装对，不是"环境问题"。

> `scripts/demo_live_rag.py` 是**真机**验收（要 embedding 凭据和网络），
> 装技能时不必跑；想看 RAG 真接通的样貌再跑它。

---

## 五、升级

1. **先备份**旧版本（整个目录复制到别处，例如
   `~/.codex/skills/_backup/es-memory-rag-<日期>/`）
2. 覆盖：只换 `SKILL.md` `references/` `scripts/` `adapters/` `assets/` `agents/`
   （`CHANGELOG.md` `README.md` `README.en.md` `INSTALL.md` `LICENSE` `NOTICE.md` 是新增或
   更新的，一并放上）
3. **回读校验**：确认新 `SKILL.md` 的 frontmatter 有 `version:` 行，且
   `references/` 下 6 份文档都在
4. 跑上面**方法 B** 的三条测试

### 从 1.0.0 升到 1.1.0 的差异

- 新增 `adapters/` —— 参考适配器（embedding / 向量库 / rerank / 契约 / outbox 消费）
- 新增 `references/adapters.md` —— 接线文档
- 新增 `scripts/test_adapter.py`（40 项）与 `scripts/demo_live_rag.py`（真机验收）
- 补上契约里原本漏掉的一条：先过相关性阈值再排序
- **记忆生命周期的数值与语义一个都没改**

### 从 v1 升到 1.0.0 的差异

1.0.0 是**首个公开版本**，把开发期内部的 v2 / v3 / v4 三个迭代编号合并进来了。
相对 v1 多了这些：

- `references/emotion.md` —— 情绪三层（L1 快照 / L2 负荷 / L3 评价）
- `references/state-ledger.md` —— 状态层四件套与禁区违反率
- `references/context-budget.md` —— 512K 硬顶 / 256K 软线
- `references/evidence.md` —— 34 次实测与被推翻的假设
- `scripts/context_budget.py` + `scripts/test_context_budget.py` —— 17 项测试
- `assets/example-emotion-event.json`
- `SKILL.md` frontmatter 新增 `version:` 行
- **行为规范层面新增三条硬约束**：状态与叙事物理分离、注入必须声明缺失、
  总量预算与子预算分开

**记忆生命周期的数值与语义（λ、阈值、抽签、回收、outbox 契约）一个都没改。**
逐条对照见 [CHANGELOG.md](CHANGELOG.md)。

---

## 六、卸载

删掉整个 `es-memory-rag/` 目录即可，没有任何系统级注册表、服务或后台进程。
参考引擎是纯标准库、纯 SQLite 文件，删目录即删干净。

> systemd 模板（`assets/es-memory-warehouse.service` / `.timer`）**只是模板**，
> 本包**不会**自动安装任何定时任务。要真让它每天凌晨跑，得自己按
> [references/integration.md](references/integration.md) 接线。
