> 来源：FastAPI 官方中文文档，MIT License。
> 原文：https://github.com/fastapi/fastapi/blob/993400812ce9f21a2a424e8366984b49b11f25f5/docs/zh/docs/tutorial/debugging.md
> 说明：代码示例已从仓库 docs_src 内联到正文。

# 调试

你可以在编辑器中连接调试器，例如使用 Visual Studio Code 或 PyCharm。

## 调用 `uvicorn`

在你的 FastAPI 应用中直接导入 `uvicorn` 并运行：

```python
import uvicorn
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    a = "a"
    b = "b" + a
    return {"hello world": b}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### 关于 `__name__ == "__main__"`

`__name__ == "__main__"` 的主要目的是使用以下代码调用文件时执行一些代码：

```console
$ uv run python myapp.py
```

而当其它文件导入它时并不会被调用，像这样：

```Python
from myapp import app
```

#### 更多细节

假设你的文件命名为 `myapp.py`。

如果你这样运行：

```console
$ uv run python myapp.py
```

那么文件中由 Python 自动创建的内部变量 `__name__`，会将字符串 `"__main__"` 作为值。

所以，这一段：

```Python
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

会运行。

---

如果你是导入这个模块（文件）就不会这样。

因此，如果你的另一个文件 `importer.py` 像这样：

```Python
from myapp import app

# 其他一些代码
```

在这种情况下，`myapp.py` 内部自动创建的变量 `__name__` 不会有值 `"__main__"`。

所以，这一行：

```Python
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

不会被执行。

**注意：**

更多信息请检查 [Python 官方文档](https://docs.python.org/3/library/__main__.html).

## 使用你的调试器运行代码

由于是从代码直接运行的 Uvicorn 服务器，所以你可以从调试器直接调用 Python 程序（你的 FastAPI 应用）。

---

例如，你可以在 Visual Studio Code 中：

* 进入到「调试」面板。
* 「添加配置...」。
* 选中「Python」
* 使用选项 "`Python: Current File (Integrated Terminal)`" 运行调试器。

然后它会使用你的 **FastAPI** 代码开启服务器，停在断点处，等等。

看起来可能是这样：

---

如果使用 PyCharm，你可以：

* 打开「运行」菜单。
* 选中「调试...」。
* 然后出现一个上下文菜单。
* 选择要调试的文件（本例中的 `main.py`）。

然后它会使用你的 **FastAPI** 代码开启服务器，停在断点处，等等。

看起来可能是这样：
