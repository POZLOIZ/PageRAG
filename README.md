<p align="center">
  <img src="assets/logo.svg" width="88" alt="PageRAG">
</p>

<h1 align="center">PageRAG</h1>

<p align="center">一个跑在本地的文档问答工作台，回答里的引用能翻回原文</p>

<p align="center">
  <a href="https://github.com/POZLOIZ/PageRAG/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/POZLOIZ/PageRAG/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.120+-009688?logo=fastapi&logoColor=white">
  <img alt="MySQL" src="https://img.shields.io/badge/MySQL-8-4479A1?logo=mysql&logoColor=white">
  <img alt="Milvus" src="https://img.shields.io/badge/Milvus-2.6-00A1EA">
</p>

PageRAG 是一个跑在自己机器上的文档问答工作台。把 Markdown、TXT 或文本型 PDF 上传到知识库，就可以围绕这些资料连续提问。回答里的编号对应原文中的段落，点开能看到它出自哪一章、第几行或 PDF 的第几页。

它不把文档切碎后直接丢给模型。文档改动会留下修订记录，重建索引会生成新的版本，查询用到的证据也会保存下来。所以资料更新以后，旧回答仍然可以逐条核对。浏览器打开就是完整界面，问答、知识库、概览和设置都在同一个控制台里。

很多文档问答 demo 停在把几段文字塞给模型这一步。真正用起来，读者还会追问这句话从哪来、文档改了以后旧答案还算不算数、重建索引后引用还能不能点开。PageRAG 就是围绕这三个问题做的。

- **连续追问**：选好知识库就能像聊天一样提问，回答边生成边显示，刷新页面后对话还在。
- **引用回读**：回答里的编号对应具体来源，点开就能看到它来自哪篇文档、哪一章、第几行或第几页。
- **修订与重建**：文档更新后重新处理，新问题用新内容回答，旧回答的引用仍然能核对。
- **跨知识库**：在问答页选择全部知识库后提问，系统会分别召回各库候选，再合并排序。
- **混合检索**：语义召回和 BM25 关键词召回都会执行，结果合并去重后交给重排模型排序。

![问答页：连续追问与正文引用](assets/screenshots/chat.png)

## 目录

- [快速开始](#快速开始)
- [功能](#功能)
- [使用](#使用)
- [工作方式](#工作方式)
- [文件与数据](#文件与数据)
- [配置](#配置)
- [项目结构](#项目结构)
- [测试](#测试)
- [常见问题](#常见问题)
- [参与贡献](#参与贡献)
- [致谢](#致谢)

## 快速开始

### 准备服务

PageRAG 需要以下服务：

- Python 3.12 和 [uv](https://docs.astral.sh/uv/)
- MySQL 8
- Milvus 2.6
- 一个兼容 OpenAI `/v1/chat/completions` 的生成模型接口
- 一个提供 `/v1/embeddings` 的嵌入服务
- 一个提供 `/rerank` 的重排服务。默认策略 `hybrid-rerank` 需要它，`dense` 和 `hybrid-rrf` 可以不用

### 建库与配置

下面的用户名和密码只是示例，需要换成自己的值。

```sql
CREATE DATABASE pagerag CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'pagerag'@'%' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON pagerag.* TO 'pagerag'@'%';
```

Milvus 默认连接 `http://127.0.0.1:19530`。没有开启认证时 token 留空。嵌入服务和重排服务的请求格式写在 `.env.example` 的注释里。

```bash
cp .env.example .env
# 编辑 .env，填入数据库、Milvus、模型和生成接口
```

### 启动

```bash
uv sync --locked
uv run pagerag-check
uv run pagerag init-db
uv run pagerag serve --host 127.0.0.1 --port 8000
```

`pagerag-check` 会检查虚拟环境、依赖和健康接口。加上 `--services` 还会连接 MySQL 并写入一张临时表，再读取 Milvus 的版本号，方便在第一次启动前排掉环境问题。

打开 `http://127.0.0.1:8000/` 进入控制台。`http://127.0.0.1:8000/docs` 是完整的接口文档，健康检查是 `GET /healthz`。

`init-db` 只创建缺失的表，重复执行不会改动已有表。升级已有数据库时，需要按新版表结构补列。

### 导入示例知识库

仓库自带一套从 FastAPI 官方仓库抽取的中文文档，可以直接用来演示真实资料上的问答。

```bash
uv run python scripts/seed_demo_kb.py \
  --base-url http://127.0.0.1:8000 \
  --collection 'FastAPI 官方中文文档'
```

脚本会调用正在运行的服务，建知识库、上传 33 篇文档，再重建索引。重复执行会跳过已经存在的同名文档。这套语料有 33 篇文档，正文约 18 万字，带 58 条可回答问题，问题清单在 `fixtures/demo/fastapi-docs/questions.jsonl`，导入后会切成约 630 个块。

需要重新生成示例文档时运行：

```bash
uv run python scripts/fetch_fastapi_docs.py
```

## 功能

### 连续追问

问答页选好知识库就能直接提问，回答会一边生成一边显示。对话保存在 MySQL 里，刷新页面或重新打开浏览器后还在，左侧可以搜索、改名和删除。追问时系统会把问题改写成一句适合检索的话，但生成回答仍然使用你原来问的那句。

### 引用能翻回原文

每条回答里的编号都对应右侧来源面板。面板会显示这条引用来自哪篇文档、哪一章、第几行或第几页，并把命中的段落和它前后的段落一起列出来。回答生成后会挑出其中的结论逐条核验，看有没有标引用、标出的引用是不是真的支持这句话。发现问题时会让模型修订一次，再重新核验。

### 文档更新与索引重建

同一篇文档可以上传新版本。点重新处理以后，新内容会单独切块并发布新的索引版本，旧修订切出的块不会被覆盖。旧回答的来源仍然指向当时的修订，点开还能核对。重建中断后，再点一次会从已完成的批次继续。

### 跨知识库问答

知识库选择器里有全部知识库。选中以后可以一次问所有库，系统会分别召回，再把候选合并排序。

### 检索与降级

默认会走两路检索，一路按语义找相近段落，一路用 BM25 按关键词和术语做词面匹配。两路结果合并去重后交给重排模型，命中的段落再连同前后段落组装成上下文。第一轮里同一篇文档最多占三个位置，剩下的名额按排序补上。主模型超时、限流、连接失败或返回 5xx 时会切到备用接口，两路检索里有一路失败，另一路的结果仍然可用。

## 使用

### 第一次问答

1. 打开 `http://127.0.0.1:8000/`。
2. 在知识库页新建一个知识库，上传 Markdown、TXT 或文本型 PDF，等待后台建完索引。
3. 回到问答页选择这个知识库，输入问题。
4. 点击回答里的编号，打开右侧来源面板核对原文。

![引用抽屉：来源、行号和原文](assets/screenshots/source-drawer.png)

### 更新一篇文档

在知识库页点文档右侧的更新，选择新版本文件。上传完成后点重新处理，页面会显示构建进度。新问题会使用新内容回答，旧回答里的引用仍然指向旧修订，点开还能看到当时的原文。

### 查看一次回答是怎么产生的

概览页的最近查询可以点开。查询详情里能看到当时召回了哪些候选、哪些证据真正进了上下文、回答引用了哪段原文，以及每一步花了多久。知识库页的文档详情则能看到这篇文档改过几次、索引建到哪一步，以及最后被切成了哪些段落。

| 概览 | 设置 |
| --- | --- |
| ![概览页](assets/screenshots/overview.png) | ![设置页](assets/screenshots/settings.png) |

| 查询详情 | 资料详情 |
| --- | --- |
| ![查询详情](assets/screenshots/query-detail.png) | ![资料详情](assets/screenshots/document-detail.png) |

### 同时问多个知识库

在问答页的知识库下拉框里选全部知识库，然后直接提问。系统会把每个知识库的召回结果放在一起排序，答案里的引用仍然按文档区分。跨库问答不写进左侧对话，刷新页面后不会保留。

## 工作方式

```mermaid
flowchart LR
  B[浏览器控制台] -->|HTTP 与 SSE| A[FastAPI 服务]
  A --> DB[(MySQL 8)]
  A --> MV[(Milvus 2.6)]
  A --> FS[原文件快照]
  A --> EM[嵌入服务]
  A --> RR[重排服务]
  A --> LM[生成模型]
```

MySQL 保存知识库和文档。每次修订、切出来的块、索引版本、查询记录、引用和对话也都在这里。Milvus 只存每个索引版本的向量。原文件快照和运行数据放在 `PAGERAG_DATA_DIR` 下，数据库只保存路径和哈希。

### 导入链路

上传的文件先存一份快照，再记下这次修订。解析器按标题、段落、代码围栏和表格边界切块，500 字符是合并段落时的软目标，单个段落超过 3000 字符才按句末切开。每个块都会记下字符区间、从 1 开始的行号、PDF 页码和内容哈希。

写入按批进行。每批的块先落库，再编码成向量写进 Milvus，最后记下确认状态。进程中途退出，最多重做最后一批。全部文档完成后发布新的索引版本，之后的查询才会使用新版本。

### 提问链路

一次查询开始时固定当时的索引版本，构建中的新版本不会影响这次回答。如果当前对话有历史，系统会先把追问改写成一句适合检索的话，检索和重排都用它，真正生成时再用你原来的问题。两路检索合并后重排，命中的段落会连同前后段落一起进入上下文，总量按 token 预算截断。生成完成后逐条检查引用，能修的地方最多修一次，最后通过 SSE 把状态、正文和结果推回浏览器。

```mermaid
sequenceDiagram
  participant U as 浏览器
  participant A as 应用
  participant R as 检索与重排
  participant M as 生成模型
  U->>A: 问题与最近对话
  A->>A: 固定活动索引版本
  A->>M: 追问改写
  M-->>A: 独立检索问题
  A->>R: 向量与 BM25 两路召回
  R->>R: 合并、重排、证据预算
  R-->>A: 证据单元
  A->>M: 原问题与编号证据
  M-->>A: 带引用回答
  A->>M: 引用核验
  M-->>A: 核验结果
  A-->>U: SSE 状态、正文与结果
```

## 文件与数据

| 格式 | 处理方式 |
| --- | --- |
| `.md` / `.markdown` | 按标题、段落、围栏和表格边界切块，保留章节路径与表头 |
| `.txt` | 按段落和结构边界切块 |
| `.pdf` | 逐页提取文本后切块，不解析表格，也不做 OCR |

单个文件最大 50 MB。扫描版 PDF、图片型表格和 `.docx` 暂不支持，需要先转成文本或 Markdown。

删除文档后，它不再参与检索和回答。历史回答里的来源记录还在，但点开只能看到引用信息，看不到正文。修改分块参数、嵌入模型或嵌入维度后需要重新处理。索引会记住自己是用哪套解析、切块和嵌入配置建的，换了配置不会复用旧索引。

## 配置

常用环境变量如下。`.env.example` 只是常用配置示例，全部可配置项和默认值见 `src/pagerag/settings.py`。

| 变量 | 作用 |
| --- | --- |
| `PAGERAG_DATABASE_URL` | MySQL 8 连接 |
| `PAGERAG_MILVUS_URI` / `PAGERAG_MILVUS_TOKEN` | Milvus 地址与认证 |
| `PAGERAG_LLM_BASE_URL` / `PAGERAG_LLM_MODEL` / `PAGERAG_LLM_API_KEY` | 生成模型 |
| `PAGERAG_EMBEDDING_BASE_URL` / `PAGERAG_EMBEDDING_MODEL` / `PAGERAG_EMBEDDING_DIMENSIONS` | 嵌入服务、模型与向量维度 |
| `PAGERAG_RERANK_URL` / `PAGERAG_RERANK_MODEL` | 重排服务，默认策略必填 |
| `PAGERAG_RETRIEVAL_STRATEGY` | `hybrid-rerank`、`dense` 或 `hybrid-rrf` |
| `PAGERAG_DATA_DIR` | 原文件快照与运行数据 |
| `PAGERAG_CHUNK_CHARS` / `PAGERAG_CHUNK_MAX_CHARS` | 分块软目标与硬上限 |

改分块参数、嵌入模型或嵌入维度后必须重建索引。索引身份还包含解析器版本、切块器版本和关键词分析器，换这些配置同样需要重新处理。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `src/pagerag/` | FastAPI 服务、导入、检索、生成、核验和控制台 |
| `tests/` | 离线测试 |
| `fixtures/demo/fastapi-docs/` | FastAPI 官方中文文档示例语料 |
| `scripts/fetch_fastapi_docs.py` | 重新抓取示例语料 |
| `scripts/seed_demo_kb.py` | 通过 HTTP 接口导入示例知识库 |
| `assets/screenshots/` | README 使用的运行截图 |
| `.github/workflows/ci.yml` | 推送后自动执行 ruff 与 pytest |

后端按链路拆在 `ingest.py`、`retrieval.py`、`answering.py` 和 `verification.py` 里，浏览器控制台在 `static/` 下，没有前端构建步骤。

## 测试

```bash
uv run pytest -q
uv run ruff check src tests
```

测试全部离线运行，不需要 MySQL、Milvus 或模型服务，也不读取本机 `.env`。用例覆盖分块与原文定位、检索融合、证据组装、引用核验、导入恢复、对话和状态接口。

GitHub Actions 在 `main` 分支的推送和拉取请求上执行 ruff 与 pytest，工作流见 `.github/workflows/ci.yml`。

## 常见问题

**支持哪些文件？**

Markdown、TXT 和带文本层的 PDF。扫描版 PDF、图片型表格和 `.docx` 需要先转成文本或 Markdown。

**一定要部署重排服务吗？**

默认策略 `hybrid-rerank` 需要。把 `PAGERAG_RETRIEVAL_STRATEGY` 改成 `dense` 或 `hybrid-rrf` 后可以不配置重排服务。

**文档更新后，旧回答还能核对吗？**

可以。每次修订单独切块，旧回答保存了当时的证据和原文位置。新修订需要重新处理，之后的查询才会使用新内容。

**资料会被发送到哪里？**

原文件快照、索引元数据和查询记录留在本机。正文会发送给你配置的嵌入、重排和生成服务。如果这些服务也部署在本机，资料不会离开这台机器。

**可以一次问多个知识库吗？**

可以。在问答页选择全部知识库，系统会分别召回再合并排序。跨库问答只留在当前页面，不写入左侧对话。

## 参与贡献

问题和改进建议可以直接开 issue。提交代码前请运行：

```bash
uv run pytest -q
uv run ruff check src tests
```

改动尽量围绕一个主题，并附上复现步骤或测试。涉及检索、分块或证据组装的行为变化，请在说明里写清楚预期影响。

## 致谢

示例语料来自 [FastAPI 官方中文文档](https://github.com/fastapi/fastapi)，上游提交 `993400812ce9f21a2a424e8366984b49b11f25f5`，按 MIT 许可使用。许可全文在 `fixtures/demo/fastapi-docs/LICENSE`。
