> 来源：FastAPI 官方中文文档，MIT License。
> 原文：https://github.com/fastapi/fastapi/blob/993400812ce9f21a2a424e8366984b49b11f25f5/docs/zh/docs/tutorial/index.md
> 说明：代码示例已从仓库 docs_src 内联到正文。

# 教程 - 用户指南

本教程将一步步向你展示如何使用 **FastAPI** 的绝大部分特性。

各个章节的内容循序渐进，但是又围绕着单独的主题，所以你可以直接跳转到某个章节以解决你的特定 API 需求。

本教程同样可以作为将来的参考手册，所以你可以随时回到本教程并查阅你需要的内容。

## 运行代码

所有代码片段都可以复制后直接使用（它们实际上是经过测试的 Python 文件）。

要运行任何示例，请将代码复制到 `main.py` 文件中，然后使用 `uv run` 启动 `fastapi dev`：

```console
$ uv run fastapi dev

   FastAPI   Starting development server 🚀

             Searching for package file structure from directories
             with __init__.py files
             Importing from /home/user/code/awesomeapp

    module   🐍 main.py

      code   Importing the FastAPI app object from the module with
             the following code:

             from main import app

       app   Using import string: main:app

    server   Server started at http://127.0.0.1:8000
    server   Documentation at http://127.0.0.1:8000/docs

       tip   Running in development mode, for production use:
             fastapi run

             Logs:

      INFO   Will watch for changes in these directories:
             ['/home/user/code/awesomeapp']
      INFO   Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C
             to quit)
      INFO   Started reloader process [383138] using WatchFiles
      INFO   Started server process [383153]
      INFO   Waiting for application startup.
      INFO   Application startup complete.
```

**强烈建议**你在本地编写或复制代码，对其进行编辑并运行。

在编辑器中使用 FastAPI 会真正地展现出它的优势：只需要编写很少的代码，所有的类型检查，代码补全等等。

---

## 安装 FastAPI

第一步是设置你的项目并添加 FastAPI。

安装 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)，然后创建一个项目并添加 FastAPI：

```console
$ uv init awesome-project --bare
$ cd awesome-project
$ uv add "fastapi[standard]"

---> 100%
```

`uv add` 会在 `.venv` 中创建项目的虚拟环境，将 FastAPI 添加到 `pyproject.toml`，并创建 `uv.lock`，以便稍后可以安装相同的包版本。

**这些命令的作用：**

* `uv init`：创建一个新的 Python 项目。
* `awesome-project`：在一个使用此名称的新目录中创建项目。
* `--bare`：只创建最小的 `pyproject.toml` 文件，不生成示例 `main.py`、`README.md` 或其他文件。你将在本教程的后续步骤中自己创建应用程序文件。

然后，`cd awesome-project` 会在添加 FastAPI 之前进入新的项目目录。

`uv` 会使用系统中已安装的兼容 Python 版本，或者在需要时下载一个。

当你运行 `uv add` 时，它会选择 FastAPI 以及 FastAPI 依赖的所有包的兼容版本。它会把确切版本记录到 `uv.lock` 中，从而可以稍后在另一台计算机上或部署应用程序时安装相同的包版本。

创建或更新此文件称为[**锁定**项目依赖项](https://docs.astral.sh/uv/concepts/projects/sync/)。当你添加包时，`uv` 会自动执行此操作。

**FastAPI 安装选项：**

当你使用 `uv add "fastapi[standard]"` 安装时，它会附带一些默认的可选标准依赖项，其中包括 `fastapi-cloud-cli`，它可以让你部署到 [FastAPI Cloud](https://fastapicloud.com)。

如果你不想安装这些可选依赖，可以选择安装 `uv add fastapi`。

如果你想安装标准依赖但不包含 `fastapi-cloud-cli`，可以使用 `uv add "fastapi[standard-no-fastapi-cloud-cli]"` 安装。

**改用 `pip`：**

如果你更喜欢手动管理虚拟环境和包，请创建并激活一个虚拟环境，然后使用 `pip install "fastapi[standard]"` 安装 FastAPI。

阅读[虚拟环境指南](https://tiangolo.com/guides/virtual-environments/)了解详细步骤。

## AI Agent 技能

FastAPI 为 AI coding agent 提供了一个官方 skill。它随包一起提供，因此它的指导会与你项目中安装的 FastAPI 版本保持一致，并在你更新 FastAPI 时随之更新。

在你的项目中安装 FastAPI 后，你可以使用 [Library Skills](https://library-skills.io) 安装该 skill：

```bash
uvx library-skills
```

**注意：**

`uvx` 是 `uv tool run` 的别名。它会在一个临时、隔离的环境中运行 Library Skills，同时 Library Skills 会扫描你项目中安装的包。

该 skill 与 Codex、Claude Code、Cursor、GitHub Copilot、Gemini CLI、Pi、OpenCode 以及大多数其他 coding agent 兼容。对于 Claude Code，当被询问要将该 skill 安装到哪里时，选择 `.claude/skills`。

更多详情请参阅使用 AI 开发。

## 进阶用户指南

在本**教程-用户指南**之后，你可以阅读**进阶用户指南**。

**进阶用户指南**以本教程为基础，使用相同的概念，并教授一些额外的特性。

但是你应该先阅读**教程-用户指南**（即你现在正在阅读的内容）。

教程经过精心设计，使你可以仅通过**教程-用户指南**来开发一个完整的应用程序，然后根据你的需要，使用**进阶用户指南**中的一些其他概念，以不同的方式来扩展它。
