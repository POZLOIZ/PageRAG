> 来源：FastAPI 官方中文文档，MIT License。
> 原文：https://github.com/fastapi/fastapi/blob/993400812ce9f21a2a424e8366984b49b11f25f5/docs/zh/docs/tutorial/request-forms.md
> 说明：代码示例已从仓库 docs_src 内联到正文。

# 表单数据

当你需要接收表单字段而不是 JSON 时，可以使用 `Form`。

**注意：**

要使用表单，首先安装 [`python-multipart`](https://github.com/Kludex/python-multipart)。

将其添加到你的项目中：

```console
$ uv add python-multipart
```

## 导入 `Form`

从 `fastapi` 导入 `Form`：

```python
from typing import Annotated

from fastapi import FastAPI, Form

app = FastAPI()

@app.post("/login/")
async def login(username: Annotated[str, Form()], password: Annotated[str, Form()]):
    return {"username": username}
```

## 定义 `Form` 参数

创建表单参数的方式与 `Body` 或 `Query` 相同：

例如，在 OAuth2 规范的一种使用方式（称为“密码流”）中，要求将 `username` 和 `password` 作为表单字段发送。

规范要求这些字段必须精确命名为 `username` 和 `password`，并且作为表单字段发送，而不是 JSON。

使用 `Form` 可以像使用 `Body`（以及 `Query`、`Path`、`Cookie`）一样声明相同的配置，包括校验、示例、别名（例如将 `username` 写成 `user-name`）等。

**注意：**

`Form` 是直接继承自 `Body` 的类。

**提示：**

要声明表单请求体，必须显式使用 `Form`，否则这些参数会被当作查询参数或请求体（JSON）参数。

## 布尔表单字段

浏览器在提交 HTML 表单时会忽略未选中的复选框。将布尔表单参数的默认值声明为 `False`，即可在复选框未选中时接收到 `False`：

```python
from typing import Annotated

from fastapi import FastAPI, Form

app = FastAPI()

@app.post("/preferences/")
async def update_preferences(subscribe: Annotated[bool, Form()] = False):
    return {"subscribe": subscribe}
```

选中的复选框会发送其值，默认值为 `"on"`。**FastAPI** 会将 `"on"` 解析为 `True`。

## 关于 "表单字段"

HTML 表单（``）向服务器发送数据时通常会对数据使用一种“特殊”的编码方式，这与 JSON 不同。

**FastAPI** 会确保从正确的位置读取这些数据，而不是从 JSON 中读取。

**技术细节：**

表单数据通常使用“媒体类型” `application/x-www-form-urlencoded` 进行编码。

但当表单包含文件时，会编码为 `multipart/form-data`。你将在下一章阅读如何处理文件。

你可以使用 `Form(media_type="multipart/form-data")` 在 OpenAPI 中描述多部分表单。组合使用表单参数或模型时，只需在一个参数上设置即可。

如果你想了解更多关于这些编码和表单字段的信息，请参阅 [MDN Web 文档的 `POST`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Methods/POST)。

**警告：**

你可以在一个*路径操作*中声明多个 `Form` 参数，但不能同时再声明要接收为 JSON 的 `Body` 字段，因为此时请求体会使用 `application/x-www-form-urlencoded` 而不是 `application/json` 进行编码。

这不是 **FastAPI** 的限制，而是 HTTP 协议的一部分。

## 小结

使用 `Form` 来声明表单数据输入参数。
