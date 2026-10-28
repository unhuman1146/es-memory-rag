# 1.4.0a3 发布检查

日期：2026-10-28。底座为 1.4.0a2 发布源码；已逐文件核对用户下载目录的 a2 压缩包，与所用底座一致。

本次按用户新确定的架构增加工作集换页，未继续增加联想或遗忘机制。源码、施工 skill 和接线说明可作为 alpha 包交付。

## 已运行的检查

运行环境：Windows / Python 3.11，临时数据库、离线模型替身；不需要 API key。

| 命令 | 结果 |
|---|---|
| python scripts/test_memory_engine.py | 43/43 |
| python scripts/test_context_budget.py | 17/17 |
| python scripts/test_adapter.py | 43/43 |
| python scripts/test_chat_path.py | 67/67 |
| python scripts/test_slow_forget.py | 35/35 |
| python scripts/test_release.py | 22/22 |
| python scripts/test_context_pager.py | 22/22 |

合计 **249/249 离线测试通过**。CI 已加入新测试与换页演示，未在 GitHub 上执行。

新增覆盖：500 条池与实际驻留分离，话题换页，大任务显式扩容及下一轮回落，未落盘数据保护，跨群/私密过滤，同名不同人归属，召回期间权限撤销，状态和必需工具结果超限明确拒绝，多轮无累积，以及真实参考 ChatPath 换页不删除、不强化记忆。

## 具体边界

- 交付物是施工 skill 与参考组件。没有修改阿零生产服务器，也未宣称 Hermes 的 native transcript / api_content sidecar 已完成换页。
- 未使用真实 MiniMax / SiliconFlow 接口，没有本次线上 token、费用、延迟或自然度盲评数据。
- 默认 token 计数仍为估算。模型完整协议、工具 schema 与多模态记账必须由宿主校准；固定开销太大时明确拒绝。
- 旧 ContextBudget API 保留兼容；α3 接入必须使用新的活动页路径。只升级 skill 文件或增大 SQL LIMIT 不会改变模型侧累积。
- 工具协议由宿主维护，正在运行的 tool_call/result 不能直接扔进原文分页器后随意删减。
- 原始记录的 persisted 以及 ACL 是宿主可信输入；模型生成的布尔声明不能作为落盘或权限证据。
- 默认普通记忆共享、显式保密保留权限；换页不改变权重、情绪、删除计划和仓管调度。

## 包装

发布包保留源代码、指南、配置示例、MIT 协议与测试；排除数据库、日志、凭据、运行缓存和 bytecode。MANIFEST.sha256 用于逐文件校验。新演示使用临时 SQLite 原文库验证 normal → large → normal，以及档案完整保留。
