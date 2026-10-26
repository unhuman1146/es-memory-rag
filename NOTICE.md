# NOTICE

本仓库（ES-Memory RAG）包含**原创代码与原创文档**，发布协议见 [LICENSE](LICENSE)（MIT）。

## 第三方代码

**无。** 本包只依赖 Python 3.11+ **标准库**，不包含任何第三方源代码，
也没有需要署名的 vendored 代码或二进制。

CI（`.github/workflows/tests.yml`）额外安装 `tzdata`，那只是 GitHub runner
镜像在精简版 Linux 上找不到系统时区库时的兜底，**不进入发布产物**，也不被
仓库代码 import。

## 第三方内容

`references/evidence.md` 记录的实测数据来自对**某商用长上下文接口**的公开调用
（temperature 0，合计 34 次调用）。本仓库**未复制**该接口的任何代码、提示词
或响应原文，只记录了可复核的统计量（tokenizer 系数、召回率、延迟分布等）。

文档中出现的第三方名称仅用于**标注事实来源**（例如某接口的模型名、某 Python
标准库模块名），不表示与其存在关联或背书。
