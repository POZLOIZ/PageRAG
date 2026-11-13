> 来源：FastAPI 官方中文文档，MIT License。
> 原文：https://github.com/fastapi/fastapi/blob/993400812ce9f21a2a424e8366984b49b11f25f5/docs/zh/docs/index.md
> 说明：代码示例已从仓库 docs_src 内联到正文。

# FastAPI

    FastAPI 框架，高性能，易于学习，高效编码，生产可用

---

**文档**： [https://fastapi.tiangolo.com/zh](https://fastapi.tiangolo.com/zh)

**源码**： [https://github.com/fastapi/fastapi](https://github.com/fastapi/fastapi)

---

FastAPI 是一个用于构建 API 的现代、快速（高性能）的 Web 框架，使用 Python 并基于标准的 Python 类型提示。

关键特性：

* **快速**：极高性能，可与 **NodeJS** 和 **Go** 并肩（归功于 Starlette 和 Pydantic）。[最快的 Python 框架之一](#performance)。
* **高效编码**：功能开发速度提升约 200% ～ 300%。*
* **更少 bug**：人为（开发者）错误减少约 40%。*
* **直观**：极佳的编辑器支持。处处皆可自动补全。更少的调试时间。
* **易用**：为易用和易学而设计。更少的文档阅读时间。
* **简短**：最小化代码重复。一次参数声明即可获得多种功能。更少的 bug。
* **健壮**：生产可用级代码。并带有自动生成的交互式文档。
* **标准化**：基于（并完全兼容）API 的开放标准：[OpenAPI](https://github.com/OAI/OpenAPI-Specification)（以前称为 Swagger）和 [JSON Schema](https://json-schema.org/)。

* 基于某内部开发团队在构建生产应用时的测试估算。

## 依赖

FastAPI 站在巨人的肩膀之上：

* [Starlette](https://starlette.dev/) 负责 Web 部分。
* [Pydantic](https://pydantic.dev/docs/) 负责数据部分。

## 安装

首先，[安装 `uv`](https://docs.astral.sh/uv/getting-started/installation/)，然后将 FastAPI 添加到你的项目中：

```console
$ uv add "fastapi[standard]"

---> 100%
```

**注意**: 请确保把 `"fastapi[standard]"` 用引号包起来，以保证在所有终端中都能正常工作。

如果你更喜欢使用 `pip`，请在虚拟环境中安装 `fastapi[standard]`。请参阅安装指南了解替代步骤。

## 示例

### 创建

创建文件 `main.py`，内容如下：

```Python
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}
```

或者使用 async def...

如果你的代码里会用到 `async` / `await`，请使用 `async def`：

```Python hl_lines="7  12"
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
async def read_root():
    return {"Hello": "World"}

@app.get("/items/{item_id}")
async def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}
```

**注意**:

如果你不确定，请查看文档中关于 [`async` 和 `await`](https://fastapi.tiangolo.com/zh/async/#in-a-hurry) 的 _"急用？"_ 章节。

### 运行

用下面的命令运行服务器：

```console
$ uv run fastapi dev

 ╭────────── FastAPI CLI - Development mode ───────────╮
 │                                                     │
 │  Serving at: http://127.0.0.1:8000                  │
 │                                                     │
 │  API docs: http://127.0.0.1:8000/docs               │
 │                                                     │
 │  Running in development mode, for production use:   │
 │                                                     │
 │  fastapi run                                        │
 │                                                     │
 ╰─────────────────────────────────────────────────────╯

INFO:     Will watch for changes in these directories: ['/home/user/code/awesomeapp']
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [2248755] using WatchFiles
INFO:     Started server process [2248757]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

关于命令 fastapi dev...

`fastapi dev` 命令会自动读取你的 `main.py` 文件，检测其中的 **FastAPI** 应用，并使用 [Uvicorn](https://uvicorn.dev) 启动服务器。

默认情况下，`fastapi dev` 会在本地开发时启用自动重载。

你可以在 [FastAPI CLI 文档](https://fastapi.tiangolo.com/zh/fastapi-cli/) 中了解更多。

### 检查

用浏览器打开 [http://127.0.0.1:8000/items/5?q=somequery](http://127.0.0.1:8000/items/5?q=somequery)。

你会看到如下 JSON 响应：

```JSON
{"item_id": 5, "q": "somequery"}
```

你已经创建了一个 API，它可以：

* 在路径 `/` 和 `/items/{item_id}` 接收 HTTP 请求。
* 以上两个路径都接受 `GET` 操作（也称为 HTTP 方法）。
* 路径 `/items/{item_id}` 有一个应为 `int` 的路径参数 `item_id`。
* 路径 `/items/{item_id}` 有一个可选的 `str` 类型查询参数 `q`。

### 交互式 API 文档

现在访问 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。

你会看到自动生成的交互式 API 文档（由 [Swagger UI](https://github.com/swagger-api/swagger-ui) 提供）：

![Swagger UI](https://fastapi.tiangolo.com/img/index/index-01-swagger-ui-simple.png)

### 可选的 API 文档

然后访问 [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)。

你会看到另一个自动生成的文档（由 [ReDoc](https://github.com/Redocly/redoc) 提供）：

![ReDoc](https://fastapi.tiangolo.com/img/index/index-02-redoc-simple.png)

## 示例升级

现在修改 `main.py` 文件来接收来自 `PUT` 请求的请求体。

借助 Pydantic，使用标准的 Python 类型来声明请求体。

```Python hl_lines="2  7-10 23-25"
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class Item(BaseModel):
    name: str
    price: float
    is_offer: bool | None = None

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}

@app.put("/items/{item_id}")
def update_item(item_id: int, item: Item):
    return {"item_name": item.name, "item_id": item_id}
```

`fastapi dev` 服务器会自动重载。

### 交互式 API 文档升级

现在访问 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。

* 交互式 API 文档会自动更新，并包含新的请求体：

![Swagger UI](https://fastapi.tiangolo.com/img/index/index-03-swagger-02.png)

* 点击「Try it out」按钮，它允许你填写参数并直接与 API 交互：

![Swagger UI interaction](https://fastapi.tiangolo.com/img/index/index-04-swagger-03.png)

* 然后点击「Execute」按钮，界面会与你的 API 通信、发送参数、获取结果并在屏幕上展示：

![Swagger UI interaction](https://fastapi.tiangolo.com/img/index/index-05-swagger-04.png)

### 可选文档升级

再访问 [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)。

* 可选文档同样会体现新的查询参数和请求体：

![ReDoc](https://fastapi.tiangolo.com/img/index/index-06-redoc-02.png)

### 总结

总之，你只需要把参数、请求体等的类型作为函数参数**声明一次**。

这些都使用标准的现代 Python 类型即可。

你不需要学习新的语法、某个特定库的方法或类等。

只需要标准的 **Python**。

例如，一个 `int`：

```Python
item_id: int
```

或者更复杂的 `Item` 模型：

```Python
item: Item
```

...通过一次声明，你将获得：

* 编辑器支持，包括：
    * 自动补全。
    * 类型检查。
* 数据校验：
    * 当数据无效时自动生成清晰的错误信息。
    * 即便是多层嵌套的 JSON 对象也会进行校验。
* 转换输入数据：从网络读取到 Python 数据和类型。读取来源：
    * JSON。
    * 路径参数。
    * 查询参数。
    * Cookies。
    * 请求头。
    * 表单。
    * 文件。
* 转换输出数据：从 Python 数据和类型转换为网络数据（JSON）：
    * 转换 Python 类型（`str`、`int`、`float`、`bool`、`list` 等）。
    * `datetime` 对象。
    * `UUID` 对象。
    * 数据库模型。
    * ...以及更多。
* 自动生成的交互式 API 文档，包括两种可选的用户界面：
    * Swagger UI。
    * ReDoc。

---

回到之前的代码示例，**FastAPI** 将会：

* 校验 `GET` 和 `PUT` 请求的路径中是否包含 `item_id`。
* 校验 `GET` 和 `PUT` 请求中的 `item_id` 是否为 `int` 类型。
    * 如果不是，客户端会看到清晰有用的错误信息。
* 对于 `GET` 请求，检查是否存在名为 `q` 的可选查询参数（如 `http://127.0.0.1:8000/items/foo?q=somequery`）。
    * 因为参数 `q` 被声明为 `= None`，所以它是可选的。
    * 如果没有 `None`，它就是必需的（就像 `PUT` 情况下的请求体）。
* 对于发送到 `/items/{item_id}` 的 `PUT` 请求，把请求体作为 JSON 读取：
    * 检查是否存在必需属性 `name`，且为 `str`。
    * 检查是否存在必需属性 `price`，且为 `float`。
    * 检查是否存在可选属性 `is_offer`，如果存在则应为 `bool`。
    * 对于多层嵌套的 JSON 对象，同样适用。
* 自动完成 JSON 的读取与输出转换。
* 使用 OpenAPI 记录所有内容，可用于：
    * 交互式文档系统。
    * 多语言的客户端代码自动生成系统。
* 直接提供 2 种交互式文档 Web 界面。

---

我们只是浅尝辄止，但你已经大致了解其工作方式了。

尝试把这一行：

```Python
    return {"item_name": item.name, "item_id": item_id}
```

...从：

```Python
        ... "item_name": item.name ...
```

...改为：

```Python
        ... "item_price": item.price ...
```

...看看你的编辑器如何自动补全属性并知道它们的类型：

![editor support](https://fastapi.tiangolo.com/img/vscode-completion.png)

更多包含更多特性的完整示例，请参阅 [教程 - 用户指南](https://fastapi.tiangolo.com/zh/tutorial/)。

**剧透警告**：教程 - 用户指南包括：

* 来自不同位置的**参数**声明：**请求头**、**cookies**、**表单字段**和**文件**。
* 如何设置**校验约束**，如 `maximum_length` 或 `regex`。
* 功能强大且易用的 **依赖注入** 系统。
* 安全与认证，包括对结合 **JWT tokens** 的 **OAuth2** 和 **HTTP Basic** 认证的支持。
* 更高级（但同样简单）的 **多层嵌套 JSON 模型** 声明技巧（得益于 Pydantic）。
* 通过 [Strawberry](https://strawberry.rocks) 等库进行 **GraphQL** 集成。
* 许多额外特性（归功于 Starlette），例如：
    * **WebSockets**
    * 基于 HTTPX 和 `pytest` 的极其简单的测试
    * **CORS**
    * **Cookie Sessions**
    * ...以及更多。

### 部署你的应用（可选）

你可以选择用一条命令将 FastAPI 应用部署到 [FastAPI Cloud](https://fastapicloud.com)。🚀

```console
$ uv run fastapi deploy

Deploying to FastAPI Cloud...

✅ Deployment successful!

🐔 Ready the chicken! Your app is ready at https://myapp.fastapicloud.dev
```

CLI 会自动检测你的 FastAPI 应用并将其部署到云端。如果你尚未登录，浏览器会打开以完成认证流程。

就这样！现在你可以通过该 URL 访问你的应用了。✨

#### 关于 FastAPI Cloud

**[FastAPI Cloud](https://fastapicloud.com)** 由 **FastAPI** 的同一位作者和团队打造。

它让你以最小的工作量就能**构建**、**部署**并**访问**一个 API。

它把用 FastAPI 构建应用时的**开发者体验**带到了部署到云上的过程。🎉

FastAPI Cloud 是 *FastAPI and friends* 开源项目的主要赞助方和资金提供者。✨

#### 部署到其他云厂商

FastAPI 是开源且基于标准的。你可以部署 FastAPI 应用到你选择的任意云厂商。

按照你的云厂商的指南部署 FastAPI 应用即可。🤓

## 性能

独立机构 TechEmpower 的基准测试显示，运行在 Uvicorn 下的 **FastAPI** 应用是 [最快的 Python 框架之一](https://www.techempower.com/benchmarks/#section=test&runid=7464e520-0dc2-473d-bd34-dbdfd7e85911&hw=ph&test=query&l=zijzen-7)，仅次于 Starlette 和 Uvicorn 本身（FastAPI 内部使用它们）。(*)

想了解更多，请参阅 [基准测试](https://fastapi.tiangolo.com/zh/benchmarks/) 章节。

## 依赖项

FastAPI 依赖 Pydantic 和 Starlette。

### `standard` 依赖

当你通过 `uv add "fastapi[standard]"` 安装 FastAPI 时，会包含 `standard` 组的一些可选依赖：

Pydantic 使用：

* [`email-validator`](https://github.com/JoshData/python-email-validator) - 用于 email 校验。

Starlette 使用：

* [`httpx2`](https://pydantic.dev/docs/httpx2/)（或 `httpx`）- 供 `TestClient` 使用。
* [`jinja2`](https://jinja.palletsprojects.com) - 使用默认模板配置时需要。
* [`python-multipart`](https://github.com/Kludex/python-multipart) - 使用 `request.form()` 支持表单「解析」时需要。

FastAPI 使用：

* [`uvicorn`](https://uvicorn.dev) - 加载并提供你的应用的服务器。包含 `uvicorn[standard]`，其中包含高性能服务所需的一些依赖（例如 `uvloop`）。
* `fastapi-cli[standard]` - 提供 `fastapi` 命令。
    * 其中包含 `fastapi-cloud-cli`，它允许你将 FastAPI 应用部署到 [FastAPI Cloud](https://fastapicloud.com)。

### 不包含 `standard` 依赖

如果你不想包含这些 `standard` 可选依赖，可以使用 `uv add fastapi`，而不是 `uv add "fastapi[standard]"`。

### 不包含 `fastapi-cloud-cli`

如果你想安装带有 standard 依赖但不包含 `fastapi-cloud-cli` 的 FastAPI，可以使用 `uv add "fastapi[standard-no-fastapi-cloud-cli]"`。

### 其他可选依赖

还有一些你可能想安装的可选依赖。

额外的 Pydantic 可选依赖：

* [`pydantic-settings`](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/) - 用于配置管理。
* [`pydantic-extra-types`](https://github.com/pydantic/pydantic-extra-types) - 用于在 Pydantic 中使用的额外类型。

额外的 FastAPI 可选依赖：

* [`orjson`](https://github.com/ijl/orjson) - 使用 `ORJSONResponse` 时需要。
* [`ujson`](https://github.com/ultrajson/ultrajson) - 使用 `UJSONResponse` 时需要。

## 许可协议

该项目遵循 MIT 许可协议。
