"""PageRAG 命令行入口：init-db 与 eval。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pagerag.db import Database, init_db
from pagerag.embeddings import EmbeddingClient
from pagerag.evalrunner import EvalRunner, TaskResult, write_run
from pagerag.milvus_store import MilvusStore
from pagerag.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PageRAG 命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="按当前模型建表（不改动已有表）")

    serve_parser = sub.add_parser("serve", help="启动 API 与控制台")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--reload", action="store_true")

    eval_parser = sub.add_parser("eval", help="运行评测套件中的任务")
    eval_parser.add_argument("--suite", required=True, help="tasks.json 路径")
    eval_parser.add_argument("--tasks", default="", help="逗号分隔的 task_id；空为全部")
    eval_parser.add_argument("--out", default="evaluation/runs", help="结果输出目录")

    corpus_parser = sub.add_parser(
        "eval-corpus", help="语料级评测：一次建库全题复用（MultiHop）")
    corpus_parser.add_argument("--corpus", required=True, help="corpus.json 路径")
    corpus_parser.add_argument("--questions", required=True, help="MultiHopRAG.json 路径")
    corpus_parser.add_argument("--split", required=True, help="题目清单 jsonl 路径")
    # limit 只是调试小批入口（取清单前 N 行，非代表性抽样），
    # 主结果必须跑全量（--limit 0）。
    corpus_parser.add_argument("--limit", type=int, default=0,
                               help="最多跑多少题；0 = 全部（正式比较用全量）")
    corpus_parser.add_argument("--strategy", default="",
                               choices=["", "dense", "hybrid-rrf", "hybrid-rerank"],
                               help="覆盖检索策略；空 = 用环境配置")
    corpus_parser.add_argument(
        "--decomposition-cache", default="",
        help="子查询缓存 JSON 路径（{问题: [子查询]}；存在则预载、新分解追加；"
             "配合 PAGERAG_QUERY_DECOMPOSITION_ENABLED=true 使用）")
    corpus_parser.add_argument("--out", default="evaluation/runs", help="结果输出目录")

    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "init-db":
        asyncio.run(init_db(settings.database_url))
        print("数据库表已就绪")
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("pagerag.api:app", host=args.host, port=args.port,
                    reload=args.reload)
        return 0

    if args.command == "eval":
        return asyncio.run(_run_eval(settings, args))

    if args.command == "eval-corpus":
        return asyncio.run(_run_corpus_eval(settings, args))

    return 1


async def _run_corpus_eval(settings: Settings, args) -> int:
    from pathlib import Path as _Path

    # 策略覆盖只影响本次运行；索引身份（index_build_config）不含检索策略，
    # 两臂共用同一索引，比较条件一致。子查询缓存同样只在本次运行接上。
    overrides = {}
    if args.strategy:
        overrides["retrieval_strategy"] = args.strategy
    if args.decomposition_cache:
        overrides["decomposition_cache"] = args.decomposition_cache
    if overrides:
        settings = Settings(**{**settings.model_dump(), **overrides})
    from pagerag.corpus_eval import CorpusEvalRunner, QuestionResult, write_corpus_run
    from pagerag.db import Database
    from pagerag.embeddings import EmbeddingClient
    from pagerag.milvus_store import MilvusStore

    corpus_path = _Path(args.corpus)
    questions_path = _Path(args.questions)
    split_path = _Path(args.split)
    from pagerag.corpus_eval import sha256_of
    corpus_sha = sha256_of(corpus_path)
    rows_all = json.loads(questions_path.read_text(encoding="utf-8"))
    split_rows = [json.loads(line) for line in
                  split_path.read_text(encoding="utf-8").splitlines()]
    if args.limit:
        split_rows = split_rows[: args.limit]
    if not split_rows:
        print("题目清单为空", file=sys.stderr)
        return 2

    database = Database(settings)
    store = MilvusStore(settings)
    embedder = EmbeddingClient(settings)
    runner = CorpusEvalRunner(settings, database, store, embedder)
    results: list[QuestionResult] = []
    # 逐题落盘：整批结束才写 results.jsonl 时，超时/中断会丢掉已完成的题。
    # 成功结束后删除，正式结果仍由 write_corpus_run 按运行目录写入。
    partial_path = _Path(args.out) / "results.partial.jsonl"
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_text("", encoding="utf-8")
    try:
        print(f"准备语料（{corpus_path.name}，sha256 {corpus_sha[:8]}…）…", flush=True)
        collection, _name, build_seconds, reused = await runner.ensure_collection(
            corpus_path, corpus_sha)
        print(f"集合 {_name}：{'复用已有索引' if reused else '新建索引完成'}"
              f"（{build_seconds:.0f}s）", flush=True)
        for row_index, split_row in enumerate(split_rows, start=1):
            row = dict(rows_all[split_row["row_index"]])
            row["row_index"] = split_row["row_index"]
            print(f"[{row_index}/{len(split_rows)}] {split_row['question_type']} "
                  f"row={split_row['row_index']} …", flush=True)
            item = await runner.answer_one(collection, row)
            results.append(item)
            with partial_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
                handle.flush()
    finally:
        await database.dispose()
        await embedder.aclose()
        store.close()
    out = write_corpus_run(_Path(args.out), f"multihop-{split_path.stem}", corpus_path,
                           split_path, settings, results, build_seconds, reused,
                           index_version_id=collection.active_index_version_id)
    for item in results:
        status = item.error or ("refused" if item.refused else "answered")
        print(f"row={item.row_index}: {status}")
    partial_path.unlink(missing_ok=True)
    print(f"结果已写入 {out}")
    return 0


async def _run_eval(settings: Settings, args) -> int:
    suite_path = Path(args.suite)
    package_dir = suite_path.parent
    tasks = json.loads(suite_path.read_text(encoding="utf-8"))["tasks"]
    wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
    selected = [t for t in tasks if not wanted or t["task_id"] in wanted]
    if not selected:
        print("没有匹配的任务", file=sys.stderr)
        return 2

    database = Database(settings)
    store = MilvusStore(settings)
    embedder = EmbeddingClient(settings)
    runner = EvalRunner(settings, database, store, embedder)
    results: list[TaskResult] = []
    try:
        for task in selected:
            print(f"运行 {task['task_id']} …", flush=True)
            results.append(await runner.run_task(task, package_dir))
    finally:
        await database.dispose()
        await embedder.aclose()
        store.close()
    out = write_run(Path(args.out), results, settings, suite_path)
    for item in results:
        print(f"{item.task_id}: {item.status}" + (f"（{item.error}）" if item.error else ""))
    print(f"结果已写入 {out}")
    return 0 if all(r.status == "passed" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
