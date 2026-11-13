"""把 fixtures/demo/fastapi-docs 上传成一个可问答的演示知识库。

脚本通过公开 HTTP API 操作，不直接写数据库，因此本地、隧道或远程容器里都能用。
重复执行时会跳过已存在的同名文档，适合中断后续传。

用法：
    python scripts/seed_demo_kb.py --base-url http://127.0.0.1:8000
服务开启登录时再提供 --username 和 --password。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

DEFAULT_CORPUS = Path("fixtures/demo/fastapi-docs")
DEFAULT_COLLECTION = "FastAPI 官方中文文档"


class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("detail") or parsed.get("error") or detail
            except json.JSONDecodeError:
                pass
            raise ApiError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"无法连接 {self.base_url}：{exc.reason}") from exc
        return json.loads(text) if text else {}

    def login(self, username: str, password: str) -> None:
        status = self.request("GET", "/auth/status")
        if not status.get("enabled"):
            print("服务未开启登录，跳过认证。")
            return
        self.request("POST", "/auth/login", {"username": username, "password": password})
        print(f"已登录：{username}")

    def find_collection(self, name: str) -> dict | None:
        for item in self.request("GET", "/api/collections").get("collections", []):
            if item.get("name") == name:
                return item
        return None

    def create_collection(self, name: str) -> dict:
        return self.request("POST", "/collections", {"name": name})

    def existing_titles(self, collection_id: str) -> set[str]:
        data = self.request("GET", f"/api/collections/{collection_id}/documents")
        return {item["title"] for item in data.get("documents", [])}

    def upload(self, collection_id: str, entry: dict, content: bytes,
               commit: str) -> None:
        payload = {
            "title": entry["title"],
            "filename": Path(entry["path"]).name,
            "content_b64": base64.b64encode(content).decode("ascii"),
            "revision_id": "r1",
            "source_kind": "markdown",
            "meta": {
                "source_url": entry["source_url"],
                "source_path": entry["source_path"],
                "source_commit": commit,
                "license": "MIT",
            },
        }
        self.request("POST", f"/collections/{collection_id}/documents", payload)

    def rebuild(self, collection_id: str) -> dict:
        # 同步入口会等索引真正写完再返回，脚本用它保证上传后立刻可问答。
        return self.request("POST", f"/collections/{collection_id}/rebuild")


def load_corpus(corpus: Path) -> tuple[dict, list[tuple[dict, bytes]]]:
    manifest_path = corpus / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(
            f"找不到 {manifest_path}，先运行 python scripts/fetch_fastapi_docs.py"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = []
    for entry in manifest["documents"]:
        path = corpus / entry["path"]
        documents.append((entry, path.read_bytes()))
    return manifest, documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--no-rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest, documents = load_corpus(args.corpus)
    commit = manifest["source"]["commit"]
    print(
        f"语料：{len(documents)} 篇，{manifest['total_characters']} 字符，"
        f"提交 {commit[:12]}"
    )
    if args.dry_run:
        for entry, content in documents:
            print(f"  {entry['title']}  {len(content)} 字节")
        return 0

    client = Client(args.base_url, args.timeout)
    status = client.request("GET", "/auth/status")
    if status.get("enabled") and not args.username:
        raise SystemExit("服务已开启登录，请提供 --username 和 --password")
    if args.username:
        client.login(args.username, args.password)

    collection = client.find_collection(args.collection)
    if collection is None:
        created = client.create_collection(args.collection)
        collection = {"collection_id": created["collection_id"], "name": created["name"],
                      "active_index_version": None, "documents_total": 0}
        print(f"已创建知识库：{args.collection}")
    else:
        print(f"复用知识库：{args.collection}")
    collection_id = collection["collection_id"]

    existing = client.existing_titles(collection_id)
    uploaded = 0
    skipped = 0
    failures: list[str] = []
    for entry, content in documents:
        if entry["title"] in existing:
            skipped += 1
            continue
        try:
            client.upload(collection_id, entry, content, commit)
        except ApiError as exc:
            failures.append(f"{entry['title']}: {exc}")
            print(f"  上传失败：{entry['title']}: {exc}")
            continue
        uploaded += 1
        print(f"  已上传：{entry['title']}")

    print(f"上传 {uploaded} 篇，跳过 {skipped} 篇，失败 {len(failures)} 篇。")
    if args.no_rebuild:
        print("按参数要求未重建索引。")
    elif failures:
        print("存在上传失败，暂不重建索引；修复后重跑本脚本即可续传。")
    elif uploaded or not collection.get("active_index_version"):
        print("正在重建索引，这一步会调用嵌入模型，可能需要几分钟……")
        result = client.rebuild(collection_id)
        print(f"索引完成：版本 {result.get('index_version')}，状态 {result.get('status')}")
    else:
        print("文档没有变化且已有可用索引，跳过重建。")

    detail = client.request("GET", f"/api/collections/{collection_id}")
    print(
        f"知识库 {detail['name']}：{detail['documents_total']} 篇文档，"
        f"索引版本 {detail.get('active_index_version')}。"
    )
    for failure in failures:
        print(f"失败：{failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
