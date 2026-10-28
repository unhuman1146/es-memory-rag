# 1.4.0a3：上下文工作集与主动换页

本文件是 α3 的上下文接线规范。它替代旧版“主要等到 SOFT 线再批量淘汰”的常态策略；旧 `ContextBudget` 保留为兼容 API 与最后的溢出保护。

## 结构与职责

上下文是一次请求的工作内存。RAG 权威库、向量索引和原文档案组成持久存储，向量索引本身不能替代权威事实库。只有持久化确认才算记住。换出上下文不改变权重、情绪、不创建归档或删除计划。衰减和真正遗忘继续由独立仓管处理。

- 聊天滚动记录池默认 500 条（包含当前消息），新消息进入后最旧消息退出候选池；数据库原文继续保留，仍可回查。多人消息按稳定 ID、收件人、引用和原始时序保持独立归属。
- 记录池不等于驻留区。平时只加载最近必要发言、当前任务状态和少量通过重排序的相关记忆；宿主可把当前话题涉及的较旧 message_id 作为 `relevant_ids` 换入。
- 新请求替换上一次活动页。已结束话题的召回块、已读完且落盘的工具结果及时换出，不等窗口满。
- 大任务由宿主显式传 `large_context=True` 和任务理由，结束后下一轮默认恢复 normal。不能根据模型声称“需要更多记忆”、某个网页中的指令或上下文接近上限就自动升级。
- 未完成工具调用协议和有用的任务进度不能直接丢弃。保留简短的当前状态与来源引用；尚未落盘的数据不能换出。先写原文，不必等 Curator 满 50 条完成语义判断，避免主对话等待整理模型。
- 状态台账仍每轮加载，不能为了维持低占用截断关系、禁区、未闭合任务。固定提示词和台账已超预算时报告明确错误，先瘦身工具定义或获得任务级扩容依据。

## 默认值不是使用目标

`PagerConfig` 与 [配置示例](../assets/working-set.example.json) 的数值是工程起点，须用目标模型的 tokenizer 和固定系统开销重新标定：

| 项目 | 日常上限 | 显式大任务上限 |
|---|---:|---:|
| 总请求预算（含输出预留） | 32,768 tokens | 196,608 tokens |
| 原文历史子预算 | 4,096 | 120,000 |
| 记忆子预算 | 1,200 | 24,000 |
| 仍需读取的工具结果子预算 | 2,048 | 16,384 |
| 输出预留 | 8,192 | 8,192 |

硬上限 262,144；默认最近 12 条，记录池 500 条；台账上限 500。子预算始终受总预算限制，不要求填满。即使开启 large，没有相关内容也不填充无关资料。ChatPath 自身默认 1200 token 上限继续生效；确需增加 RAG，宿主需显式配置该路径的 `token_budget`，不能只扩大 Pager。

默认估算器不是精确厂商计数器；正式请求还必须扣除消息角色包装、工具 schema、图片/音频等实际开销。分页器的 `system` 必须计入完整固定开销，不能只传一句占位文字后报告符合总预算。

## 可运行接线

```python
from adapters.context_pager import ContextPager, HistoryTurn
from adapters.chat_path import ChatPath

# 原文 ACL 必须每轮从权威来源核验，不能只信索引或缓存。
pager = ContextPager(acl=lambda row, viewer: archive.can_read(row.message_id, viewer))
path = ChatPath(engine, rag_adapter, viewer=viewer)
rows = archive.latest(scope, limit=500)  # 原始正文，禁止传增强后的旧 API 输入
frame = pager.prepare(
    system=system_prompt_and_tool_schema_text,
    ledger=current_authoritative_ledger,
    current=current_raw_message,
    current_id=current_message_id,
    history=[HistoryTurn(r.id, r.role, r.text, r.sender_id, scope,
                         persisted=r.persisted_ack, metadata=r.addressing) for r in rows],
    requester=viewer, scope=scope, path=path,
)
# frame.sections: system / ledger / active_page / current。
# 宿主把这些段映射为自己的模型请求协议。本轮 API 输入替换旧页，不能 append 到持久历史。
send_model_request(frame.sections)
```

`ContextPager` 是无状态组装器，不能单独宣称“已在 Hermes 卸载旧上下文”。它不接管工具调用协议，不提供后台索引同步或 Curator。宿主负责线程/会话串行、真实身份、持久化确认和传输送达后的强化。活动页属于不可信历史资料，应放在用户/资料区，不能升级为 system/developer 指令。返回的 `memory_report` 是原 ChatPath 报告，继续使用原 ACL、revision、墓碑、情绪闸和真实 rerank；本次换页不会调用 `used` 或 `remind`。

### Hermes / QQ 的具体迁移点

1. 原始入站继续先写原文数据库、去重并进入独立整理队列。确认回执的机器人气泡按真实平台 message_id 落盘。
2. 从原文库生成当前池，保留 sender_qq、@对象、引用原文及时间；不要从已有混入观察块的 Hermes `messages.content` 反推原文。
3. 在每轮请求组装处维护一个可替换的活动页槽位；新的页替代旧页。只改 QQ 的 `LIMIT 24` 为 `LIMIT 500` 不构成接通，且会加重累积。
4. 旧的群观察快照、RAG 围栏和 `api_content` 必须从**模型侧历史副本**中退出。按宿主生成记录/sidecar 标记识别，不能靠正则删除用户自己引用的文字，也不能仅改 `content` 而让旧 sidecar 复原回来。
5. 保留真实用户与机器人文本；模型生成过程中正在使用的 tool_call/result 作为完整协议组留住，不能产生孤立 tool result。任务完成且原始结果落盘后卸载大结果，状态层只存简短未完成事项及来源。
6. 固定系统前缀保持稳定，活动页放在变量区。发生话题切换、权限或事实 revision 变化时及时替换；不以缓存命中为由把无关记忆保留到上限。
7. 不把完整 `frame.render()` 写回聊天原文或追加到本机模型历史。日志只记录页数、token、换出原因及必要的权限安全内部 ID，排查原文另走受控读取。

上线验收必须比较真实 wire 请求，而非只看数据库正文长度：普通聊天连续多轮总量稳定；重排后撤销权限的页不进入请求；大任务扩容后下一轮降回；原文可回查且记忆权重未因换页变化。留存前后模型实际 input/cache token，不用“缓存约 99%”代替上下文大小或价格测量。

## 已覆盖与尚未覆盖

`python scripts/test_context_pager.py` 验证滚动池、低占用、显式扩容后降回、归属与 ACL、原文/工具持久化保护、权限在召回期间变化、旧页不累计、真实参考 ChatPath 换页不删除记忆。

`python scripts/demo_paging.py` 使用临时 SQLite 原文档案演示 normal → large → normal。它验证组装逻辑，不验证 MiniMax/SiliconFlow 的实际延迟、成本，或 Hermes 的协议与 sidecar 改造。
