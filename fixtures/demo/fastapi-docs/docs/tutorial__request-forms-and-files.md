> 来源：FastAPI 官方中文文档，MIT License。
> 原文：https://github.com/fastapi/fastapi/blob/993400812ce9f21a2a424e8366984b49b11f25f5/docs/zh/docs/tutorial/request-forms-and-files.md
> 说明：代码示例已从仓库 docs_src 内联到正文。

# 请求表单与文件

FastAPI 支持同时使用 `File` 和 `Form` 定义文件和表单字段。

**注意：**

接收上传的文件和/或表单数据，首先安装 [`python-multipart`](https://github.com/Kludex/python-multipart)。

将它添加到你的项目中：

```console
$ uv add python-multipart
```

## 导入 `File` 与 `Form`

```python
from typing import Annotated

from fastapi import FastAPI, File, Form, UploadFile

app = FastAPI()

@app.post("/files/")
async def create_file(
    file: Annotated[bytes, File()],
    fileb: Annotated[UploadFile, File()],
    token: Annotated[str, Form()],
):
    return {
        "file_size": len(file),
        "token": token,
        "fileb_content_type": fileb.content_type,
    }
```

## 定义 `File` 与 `Form` 参数

创建文件和表单参数的方式与 `Body` 和 `Query` 一样：

文件和表单字段作为表单数据上传与接收。

并且你可以将部分文件声明为 `bytes`，将部分文件声明为 `UploadFile`。

**警告：**

可在一个*路径操作*中声明多个 `File` 与 `Form` 参数，但不能同时声明要接收 JSON 的 `Body` 字段。因为此时请求体的编码为 `multipart/form-data`，不是 `application/json`。

这不是 **FastAPI** 的问题，而是 HTTP 协议的规定。

## 小结

在同一个请求中接收数据和文件时，应同时使用 `File` 和 `Form`。
