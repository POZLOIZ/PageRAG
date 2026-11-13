> 来源：FastAPI 官方中文文档，MIT License。
> 原文：https://github.com/fastapi/fastapi/blob/993400812ce9f21a2a424e8366984b49b11f25f5/docs/zh/docs/tutorial/testing.md
> 说明：代码示例已从仓库 docs_src 内联到正文。

# 测试

感谢 [Starlette](https://starlette.dev/testclient/)，测试**FastAPI** 应用轻松又愉快。

它基于 [HTTPX](https://www.python-httpx.org)，而HTTPX又是基于Requests设计的，所以很相似且易懂。

有了它，你可以直接与**FastAPI**一起使用 [pytest](https://docs.pytest.org/)。

## 使用 `TestClient`

**注意：**

要使用 `TestClient`，先要安装 [`httpx`](https://www.python-httpx.org)。

将它添加到你的项目中：

```console
$ uv add httpx
```

导入 `TestClient`。

通过传入你的**FastAPI**应用创建一个 `TestClient` 。

创建名字以 `test_` 开头的函数（这是标准的 `pytest` 约定）。

像使用 `httpx` 那样使用 `TestClient` 对象。

为你需要检查的地方用标准的Python表达式写个简单的 `assert` 语句（重申，标准的`pytest`）。

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()

@app.get("/")
async def read_main():
    return {"msg": "Hello World"}

client = TestClient(app)

def test_read_main():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"msg": "Hello World"}
```

**提示：**

注意测试函数是普通的 `def`，不是 `async def`。

还有client的调用也是普通的调用，不是用 `await`。

这让你可以直接使用 `pytest` 而不会遇到麻烦。

**技术细节：**

你也可以用 `from starlette.testclient import TestClient`。

**FastAPI** 提供了和 `starlette.testclient` 一样的 `fastapi.testclient`，只是为了方便开发者。但它直接来自Starlette。

**提示：**

除了发送请求之外，如果你还想测试时在FastAPI应用中调用 `async` 函数（例如异步数据库函数）， 可以在高级教程中看下异步测试。

## 分离测试

在实际应用中，你可能会把你的测试放在另一个文件里。

你的**FastAPI**应用程序也可能由一些文件/模块组成等等。

### **FastAPI** app 文件

假设你有一个像更大的应用中所描述的文件结构:

```
.
├── app
│   ├── __init__.py
│   └── main.py
```

在 `main.py` 文件中你有一个 **FastAPI** app:

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
async def read_main():
    return {"msg": "Hello World"}
```

### 测试文件

然后你会有一个包含测试的文件 `test_main.py` 。它可以位于同一个 Python 包中（一样是目录，但有个 `__init__.py` 文件）：

``` hl_lines="5"
.
├── app
│   ├── __init__.py
│   ├── main.py
│   └── test_main.py
```

因为这文件在同一个包中，所以你可以通过相对导入从 `main` 模块（`main.py`）导入`app`对象：

```python
from fastapi.testclient import TestClient

from .main import app

client = TestClient(app)

def test_read_main():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"msg": "Hello World"}
```

...然后测试代码和之前一样的。

## 测试：扩展示例

现在让我们扩展这个例子，并添加更多细节，看下如何测试不同部分。

### 扩展后的 **FastAPI** app 文件

让我们继续之前的文件结构：

```
.
├── app
│   ├── __init__.py
│   ├── main.py
│   └── test_main.py
```

假设现在包含**FastAPI** app的文件 `main.py` 有些其他**路径操作**。

有个 `GET` 操作可能返回一个错误。

有个 `POST` 操作可能返回多个错误。

两个*路径操作* 都需要一个`X-Token` 头。

```python
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

fake_secret_token = "coneofsilence"

fake_db = {
    "foo": {"id": "foo", "title": "Foo", "description": "There goes my hero"},
    "bar": {"id": "bar", "title": "Bar", "description": "The bartenders"},
}

app = FastAPI()

class Item(BaseModel):
    id: str
    title: str
    description: str | None = None

@app.get("/items/{item_id}", response_model=Item)
async def read_main(item_id: str, x_token: Annotated[str, Header()]):
    if x_token != fake_secret_token:
        raise HTTPException(status_code=400, detail="Invalid X-Token header")
    if item_id not in fake_db:
        raise HTTPException(status_code=404, detail="Item not found")
    return fake_db[item_id]

@app.post("/items/")
async def create_item(item: Item, x_token: Annotated[str, Header()]) -> Item:
    if x_token != fake_secret_token:
        raise HTTPException(status_code=400, detail="Invalid X-Token header")
    if item.id in fake_db:
        raise HTTPException(status_code=409, detail="Item already exists")
    fake_db[item.id] = item.model_dump()
    return item
```

### 扩展后的测试文件

然后你可以使用扩展后的测试更新`test_main.py`：

```python
from fastapi.testclient import TestClient

from .main import app

client = TestClient(app)

def test_read_item():
    response = client.get("/items/foo", headers={"X-Token": "coneofsilence"})
    assert response.status_code == 200
    assert response.json() == {
        "id": "foo",
        "title": "Foo",
        "description": "There goes my hero",
    }

def test_read_item_bad_token():
    response = client.get("/items/foo", headers={"X-Token": "hailhydra"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid X-Token header"}

def test_read_nonexistent_item():
    response = client.get("/items/baz", headers={"X-Token": "coneofsilence"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found"}

def test_create_item():
    response = client.post(
        "/items/",
        headers={"X-Token": "coneofsilence"},
        json={"id": "foobar", "title": "Foo Bar", "description": "The Foo Barters"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": "foobar",
        "title": "Foo Bar",
        "description": "The Foo Barters",
    }

def test_create_item_bad_token():
    response = client.post(
        "/items/",
        headers={"X-Token": "hailhydra"},
        json={"id": "bazz", "title": "Bazz", "description": "Drop the bazz"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid X-Token header"}

def test_create_existing_item():
    response = client.post(
        "/items/",
        headers={"X-Token": "coneofsilence"},
        json={
            "id": "foo",
            "title": "The Foo ID Stealers",
            "description": "There goes my stealer",
        },
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Item already exists"}
```

每当你需要客户端在请求中传递信息，但你不知道如何传递时，你可以通过搜索（谷歌）如何用 `httpx` 做，或者是用 `requests` 做，毕竟HTTPX的设计是基于Requests的设计的。

接着只需在测试中同样操作。

示例：

* 传一个*路径* 或*查询* 参数，添加到URL上。
* 传一个JSON体，传一个Python对象(例如一个`dict`)到参数 `json`。
* 如果你需要发送 *Form Data* 而不是 JSON，使用 `data` 参数。
* 要发送 *headers*，传 `dict` 给 `headers` 参数。
* 对于 *cookies*，传 `dict` 给 `cookies` 参数。

关于如何传数据给后端的更多信息（使用 `httpx` 或 `TestClient`），请查阅 [HTTPX 文档](https://www.python-httpx.org)。

**注意：**

注意 `TestClient` 接收可以被转化为JSON的数据，而不是Pydantic模型。

如果你在测试中有一个Pydantic模型，并且你想在测试时发送它的数据给应用，你可以使用在JSON 兼容编码器介绍的`jsonable_encoder` 。

## 运行起来

之后，你只需要安装 `pytest`。

将它添加到你的项目中：

```console
$ uv add pytest

---> 100%
```

它会自动检测文件和测试，执行测试，然后向你报告结果。

执行测试：

```console
$ uv run pytest

================ test session starts ================
platform linux -- Python 3.6.9, pytest-5.3.5, py-1.8.1, pluggy-0.13.1
rootdir: /home/user/code/superawesome-cli/app
plugins: forked-1.1.3, xdist-1.31.0, cov-2.8.1
collected 6 items

---> 100%

test_main.py ......                            [100%]

================= 1 passed in 0.03s =================
```
