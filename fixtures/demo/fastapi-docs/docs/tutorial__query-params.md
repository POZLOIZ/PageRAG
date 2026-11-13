> 来源：FastAPI 官方中文文档，MIT License。
> 原文：https://github.com/fastapi/fastapi/blob/993400812ce9f21a2a424e8366984b49b11f25f5/docs/zh/docs/tutorial/query-params.md
> 说明：代码示例已从仓库 docs_src 内联到正文。

# 查询参数

声明的参数不是路径参数时，路径操作函数会把该参数自动解释为“查询”参数。

```python
from fastapi import FastAPI

app = FastAPI()

fake_items_db = [{"item_name": "Foo"}, {"item_name": "Bar"}, {"item_name": "Baz"}]

@app.get("/items/")
async def read_item(skip: int = 0, limit: int = 10):
    return fake_items_db[skip : skip + limit]
```

查询字符串是键值对的集合，这些键值对位于 URL 的 `?` 之后，以 `&` 分隔。

例如，以下 URL 中：

```
http://127.0.0.1:8000/items/?skip=0&limit=10
```

...查询参数为：

* `skip`：值为 `0`
* `limit`：值为 `10`

这些值都是 URL 的组成部分，因此，它们的类型“本应”是字符串。

但声明 Python 类型（上例中为 `int`）之后，这些值就会转换为声明的类型，并进行类型校验。

所有应用于路径参数的流程也适用于查询参数：

* （显而易见的）编辑器支持
* 数据"解析"
* 数据校验
* 自动文档

## 默认值

查询参数不是路径的固定内容，它可以是可选的，还支持默认值。

上例用 `skip=0` 和 `limit=10` 设定默认值。

访问 URL：

```
http://127.0.0.1:8000/items/
```

与访问以下地址相同：

```
http://127.0.0.1:8000/items/?skip=0&limit=10
```

但如果访问：

```
http://127.0.0.1:8000/items/?skip=20
```

查询参数的值就是：

* `skip=20`：在 URL 中设定的值
* `limit=10`：使用默认值

## 可选参数

同理，把默认值设为 `None` 即可声明可选的查询参数：

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/items/{item_id}")
async def read_item(item_id: str, q: str | None = None):
    if q:
        return {"item_id": item_id, "q": q}
    return {"item_id": item_id}
```

本例中，查询参数 `q` 是可选的，默认值为 `None`。

**提示：**

注意，**FastAPI** 可以识别出 `item_id` 是路径参数，`q` 不是路径参数，而是查询参数。

## 查询参数类型转换

参数还可以声明为 `bool` 类型，FastAPI 会自动转换参数类型：

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/items/{item_id}")
async def read_item(item_id: str, q: str | None = None, short: bool = False):
    item = {"item_id": item_id}
    if q:
        item.update({"q": q})
    if not short:
        item.update(
            {"description": "This is an amazing item that has a long description"}
        )
    return item
```

本例中，访问：

```
http://127.0.0.1:8000/items/foo?short=1
```

或

```
http://127.0.0.1:8000/items/foo?short=True
```

或

```
http://127.0.0.1:8000/items/foo?short=true
```

或

```
http://127.0.0.1:8000/items/foo?short=on
```

或

```
http://127.0.0.1:8000/items/foo?short=yes
```

或其它任意大小写形式（大写、首字母大写等），函数接收的 `short` 参数都是布尔值 `True`。

`0`、`false`、`off` 和 `no` 等值（包括大小写变体）会转换为 `False`。

如果省略 `short`，**FastAPI** 会使用它的默认值，本例中为 `False`。

空值（`?short=`）或无法识别的值（例如 `?short=maybe`）会导致 `422` 校验错误。

## 多个路径和查询参数

**FastAPI** 可以识别同时声明的多个路径参数和查询参数。

而且声明这些参数的顺序并不重要。

FastAPI 通过参数名进行检测：

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/users/{user_id}/items/{item_id}")
async def read_user_item(
    user_id: int, item_id: str, q: str | None = None, short: bool = False
):
    item = {"item_id": item_id, "owner_id": user_id}
    if q:
        item.update({"q": q})
    if not short:
        item.update(
            {"description": "This is an amazing item that has a long description"}
        )
    return item
```

## 必选查询参数

为不是路径参数的参数声明默认值（至此，仅有查询参数），该参数就不是必选的了。

如果只想把参数设为可选，但又不想指定参数的值，则要把默认值设为 `None`。

如果要把查询参数设置为必选，就不要声明默认值：

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/items/{item_id}")
async def read_user_item(item_id: str, needy: str):
    item = {"item_id": item_id, "needy": needy}
    return item
```

这里的查询参数 `needy` 是类型为 `str` 的必选查询参数。

在浏览器中打开如下 URL：

```
http://127.0.0.1:8000/items/foo-item
```

...因为没有添加必选参数 `needy`，返回的响应中会显示如下错误信息：

```JSON
{
  "detail": [
    {
      "type": "missing",
      "loc": [
        "query",
        "needy"
      ],
      "msg": "Field required",
      "input": null
    }
  ]
}
```

`needy` 是必选参数，因此要在 URL 中设置值：

```
http://127.0.0.1:8000/items/foo-item?needy=sooooneedy
```

...这样就正常了：

```JSON
{
    "item_id": "foo-item",
    "needy": "sooooneedy"
}
```

当然，把一些参数定义为必选，为另一些参数设置默认值，再把其它参数定义为可选，这些操作都是可以的：

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/items/{item_id}")
async def read_user_item(
    item_id: str, needy: str, skip: int = 0, limit: int | None = None
):
    item = {"item_id": item_id, "needy": needy, "skip": skip, "limit": limit}
    return item
```

本例中有 3 个查询参数：

* `needy`，必选的 `str` 类型参数
* `skip`，默认值为 `0` 的 `int` 类型参数
* `limit`，可选的 `int` 类型参数

**提示：**

还可以像在路径参数中那样使用 `Enum`。
