# FastAPI 官方中文文档语料

PageRAG 演示知识库使用这个目录里的 Markdown。内容全部来自 FastAPI 官方仓库的
中文文档，没有生成或改写的业务事实。脚本只把 MkDocs 的代码 include 内联，
并清理页面装饰性 HTML。

## 来源

- 仓库：https://github.com/fastapi/fastapi
- 提交：`993400812ce9f21a2a424e8366984b49b11f25f5`
- 原文目录：`docs/zh/docs`
- 许可：MIT License，全文见同目录 `LICENSE`

## 语料规模

- 文档数：33
- 正文字符数：183288
- 内联代码文件：142 处
- 因重复而省略：56 处

## 重新生成

```bash
cd PageRAG
python scripts/fetch_fastapi_docs.py
```

每篇文档开头保留原文地址，正文代码来自官方 `docs_src`。派生语料继续使用 MIT
许可，转载或再分发时请保留 `LICENSE` 与本说明。

## 文件清单

| 文档 | 字符数 | 原文 |
| --- | --- | --- |
| FastAPI | 10054 | `docs/zh/docs/index.md` |
| 教程 - 用户指南 | 3554 | `docs/zh/docs/tutorial/index.md` |
| 第一步 | 7414 | `docs/zh/docs/tutorial/first-steps.md` |
| 路径参数 | 5234 | `docs/zh/docs/tutorial/path-params.md` |
| 查询参数 | 4212 | `docs/zh/docs/tutorial/query-params.md` |
| 请求体 | 4263 | `docs/zh/docs/tutorial/body.md` |
| 查询参数和字符串校验 | 13695 | `docs/zh/docs/tutorial/query-params-str-validations.md` |
| 路径参数和数值校验 | 5122 | `docs/zh/docs/tutorial/path-params-numeric-validations.md` |
| 请求体 - 多个参数 | 5337 | `docs/zh/docs/tutorial/body-multiple-params.md` |
| 请求体 - 字段 | 1831 | `docs/zh/docs/tutorial/body-fields.md` |
| 请求体 - 嵌套模型 | 6750 | `docs/zh/docs/tutorial/body-nested-models.md` |
| 响应模型 - 返回类型 | 12288 | `docs/zh/docs/tutorial/response-model.md` |
| 响应状态码 | 2082 | `docs/zh/docs/tutorial/response-status-code.md` |
| 处理错误 | 8431 | `docs/zh/docs/tutorial/handling-errors.md` |
| 依赖项 | 5126 | `docs/zh/docs/tutorial/dependencies/index.md` |
| 类作为依赖项 | 6456 | `docs/zh/docs/tutorial/dependencies/classes-as-dependencies.md` |
| 子依赖项 | 2567 | `docs/zh/docs/tutorial/dependencies/sub-dependencies.md` |
| 安全性 | 2275 | `docs/zh/docs/tutorial/security/index.md` |
| 安全 - 第一步 | 4119 | `docs/zh/docs/tutorial/security/first-steps.md` |
| OAuth2 实现简单的 Password 和 Bearer 验证 | 7054 | `docs/zh/docs/tutorial/security/simple-oauth2.md` |
| 请求文件 | 6341 | `docs/zh/docs/tutorial/request-files.md` |
| 表单数据 | 2063 | `docs/zh/docs/tutorial/request-forms.md` |
| 请求表单与文件 | 1168 | `docs/zh/docs/tutorial/request-forms-and-files.md` |
| SQL（关系型）数据库 | 11600 | `docs/zh/docs/tutorial/sql-databases.md` |
| 更大的应用 - 多个文件 | 12762 | `docs/zh/docs/tutorial/bigger-applications.md` |
| 后台任务 | 3255 | `docs/zh/docs/tutorial/background-tasks.md` |
| 中间件 | 2269 | `docs/zh/docs/tutorial/middleware.md` |
| CORS（跨域资源共享） | 3340 | `docs/zh/docs/tutorial/cors.md` |
| 测试 | 6709 | `docs/zh/docs/tutorial/testing.md` |
| 调试 | 1678 | `docs/zh/docs/tutorial/debugging.md` |
| 静态文件 | 1207 | `docs/zh/docs/tutorial/static-files.md` |
| 设置和环境变量 | 8649 | `docs/zh/docs/advanced/settings.md` |
| 生命周期事件 | 4383 | `docs/zh/docs/advanced/events.md` |
