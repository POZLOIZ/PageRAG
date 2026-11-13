"""把 FastAPI 官方中文文档整理成可入库的 Markdown 语料。

语料全部来自 fastapi/fastapi 仓库的 docs/zh/docs 目录，固定到某个提交，
再把 MkDocs 的 include 指令替换成真实代码文件。脚本只做下载、内联和格式清理，
不生成或改写任何事实性内容。MIT 许可与来源地址写入语料目录的 README。

用法：
    python scripts/fetch_fastapi_docs.py
    python scripts/fetch_fastapi_docs.py --out fixtures/demo/fastapi-docs
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_COMMIT = "993400812ce9f21a2a424e8366984b49b11f25f5"
RAW_ROOT = "https://raw.githubusercontent.com/fastapi/fastapi"
BLOB_ROOT = "https://github.com/fastapi/fastapi/blob"
LICENSE_URL = f"{RAW_ROOT}/{DEFAULT_COMMIT}/LICENSE"

# 覆盖入门、请求参数、响应、依赖、安全、表单文件、数据库、工程结构与运行机制。
SELECTED = [
    "index.md",
    "tutorial/index.md",
    "tutorial/first-steps.md",
    "tutorial/path-params.md",
    "tutorial/query-params.md",
    "tutorial/body.md",
    "tutorial/query-params-str-validations.md",
    "tutorial/path-params-numeric-validations.md",
    "tutorial/body-multiple-params.md",
    "tutorial/body-fields.md",
    "tutorial/body-nested-models.md",
    "tutorial/response-model.md",
    "tutorial/response-status-code.md",
    "tutorial/handling-errors.md",
    "tutorial/dependencies/index.md",
    "tutorial/dependencies/classes-as-dependencies.md",
    "tutorial/dependencies/sub-dependencies.md",
    "tutorial/security/index.md",
    "tutorial/security/first-steps.md",
    "tutorial/security/simple-oauth2.md",
    "tutorial/request-files.md",
    "tutorial/request-forms.md",
    "tutorial/request-forms-and-files.md",
    "tutorial/sql-databases.md",
    "tutorial/bigger-applications.md",
    "tutorial/background-tasks.md",
    "tutorial/middleware.md",
    "tutorial/cors.md",
    "tutorial/testing.md",
    "tutorial/debugging.md",
    "tutorial/static-files.md",
    "advanced/settings.md",
    "advanced/events.md",
]

INCLUDE_RE = re.compile(r"^\{\*\s*(?P<path>\S+?)(?:\s+(?P<attrs>.*?))?\s*\*\}\s*$")
LN_RE = re.compile(r"\bln\[(\d+)(?::(\d+))?\]")
TITLE_RE = re.compile(r'\btitle\["([^"]+)"\]')
ADMONITION_RE = re.compile(r"^///\s+([A-Za-z]+)(?:\s*\|\s*(.*?))?\s*$")
HEADING_ANCHOR_RE = re.compile(r"^(#{1,6}\s+.*?)\s*\{[^}]*\}\s*$")
LINK_RE = re.compile(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
IMAGE_RE = re.compile(r"<img\s[^>]*>", re.I)
TAG_RE = re.compile(
    r"</?(?:font|span|div|p|b|strong|i|em|u|a|dfn|abbr|code|small|br|button|form|"
    r"details|summary|blockquote|style|script|table|thead|tbody|tr|td|th)\b[^>]*>",
    re.I,
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
STYLE_RE = re.compile(r"<style\b.*?</style>", re.S | re.I)
SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
TEMPLATE_LINE_RE = re.compile(r"^\s*\{%\s*.*?%\}\s*$")
TEMPLATE_INLINE_RE = re.compile(r"\{\{.*?\}\}")
EMPTY_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*\)")

LANGUAGES = {
    ".py": "python",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "javascript",
    ".ts": "typescript",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".txt": "text",
}

ADMONITION_LABELS = {
    "tip": "提示",
    "note": "注意",
    "warning": "警告",
    "danger": "危险",
    "details": "展开查看",
    "info": "说明",
    "example": "示例",
}


def fetch(url: str, cache: Path) -> str:
    """按 URL 缓存下载文本，避免同一代码文件反复请求 GitHub。"""
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cached = cache / key
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    request = urllib.request.Request(url, headers={"User-Agent": "PageRAG-corpus-builder"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"下载失败 {url}: HTTP {exc.code}") from exc
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text


def strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


MARKETING_SECTIONS = ("赞助商", "评价", "迷你纪录片")


def drop_marketing(text: str) -> str:
    """首页的赞助商、用户评价和宣传视频不是技术内容，入库只会制造噪声。"""
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if re.match(r"^##\s+", line):
            heading = line.lstrip("#").replace("*", "").strip()
            skipping = any(marker in heading for marker in MARKETING_SECTIONS) or (
                heading.startswith("Typer")
            )
            if skipping:
                continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def resolve_include_path(doc_rel: str, include_path: str, commit: str, cache: Path) -> tuple[str, str]:
    """返回代码文件在仓库里的相对路径与文本。

    MkDocs 里的 ../../docs_src 是相对 docs/zh/docs 写的，但仓库实际把
    docs_src 放在根目录，因此按 docs_src/ 后缀回退到根目录路径。
    """
    candidates: list[str] = []
    marker = "docs_src/"
    if marker in include_path and not include_path.startswith(marker):
        # 官方文档里的路径是相对 docs/zh/docs 写的，仓库实际把 docs_src
        # 放在根目录。优先走真实路径，避免每次都先发一个必然 404 的请求。
        candidates.append(marker + include_path.split(marker, 1)[1])
    doc_dir = str(Path("docs/zh/docs") / Path(doc_rel).parent)
    literal = str((Path(doc_dir) / include_path).as_posix())
    if not literal.startswith("docs/zh/docs_src/"):
        candidates.append(literal)
    last_error: Exception | None = None
    for candidate in candidates:
        normalized = Path(candidate).as_posix()
        if normalized.startswith("docs/zh/docs_src/"):
            normalized = normalized[len("docs/zh/") :]
        try:
            text = fetch(f"{RAW_ROOT}/{commit}/{normalized}", cache)
        except RuntimeError as exc:
            last_error = exc
            continue
        return normalized, text
    raise RuntimeError(f"{doc_rel} 中的代码文件无法定位：{include_path} ({last_error})")


def expand_includes(text: str, doc_rel: str, commit: str, cache: Path) -> tuple[str, int, int]:
    lines = text.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    inlined = 0
    deduped = 0
    for line in lines:
        match = INCLUDE_RE.match(line)
        if not match:
            out.append(line)
            continue
        include_path = match.group("path")
        attrs = match.group("attrs") or ""
        code_rel, code = resolve_include_path(doc_rel, include_path, commit, cache)
        code_lines = code.splitlines()
        line_match = LN_RE.search(attrs)
        if line_match:
            start = int(line_match.group(1))
            end = int(line_match.group(2) or start)
            code_lines = code_lines[max(start - 1, 0) : end]
        body = "\n".join(code_lines).rstrip()
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if digest in seen:
            deduped += 1
            continue
        seen.add(digest)
        inlined += 1
        title_match = TITLE_RE.search(attrs)
        suffix = Path(code_rel).suffix.lower()
        language = LANGUAGES.get(suffix, "")
        if title_match:
            out.append(f"代码文件：`{title_match.group(1)}`")
            out.append("")
        out.append(f"```{language}")
        out.append(body)
        out.append("```")
    return "\n".join(out), inlined, deduped


def convert_admonitions(text: str) -> str:
    """把 MkDocs 的 /// 提示 转成普通 Markdown 段落，避免入库后出现语法噪声。"""
    out: list[str] = []
    label: str | None = None
    for line in text.splitlines():
        start = ADMONITION_RE.match(line)
        if start:
            kind = start.group(1).lower()
            title = (start.group(2) or "").strip()
            label = title or ADMONITION_LABELS.get(kind, kind)
            out.append(f"**{label}：**")
            out.append("")
            continue
        if line.strip() == "///":
            label = None
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def clean_markdown(text: str) -> str:
    text = STYLE_RE.sub("", text)
    text = SCRIPT_RE.sub("", text)
    text = COMMENT_RE.sub("", text)
    text = drop_marketing(text)
    text = "\n".join(
        line for line in text.splitlines()
        if not TEMPLATE_LINE_RE.match(line) and "fastapi-sponsors" not in line
    )
    text = TEMPLATE_INLINE_RE.sub("", text)
    text = LINK_RE.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", text)
    text = IMAGE_RE.sub("", text)
    text = convert_admonitions(text)
    text = "\n".join(
        HEADING_ANCHOR_RE.sub(r"\1", line) if line.startswith("#") else line
        for line in text.splitlines()
    )
    text = clean_fences_and_prose(text)
    # 徽章图片去掉后只剩空的 Markdown 链接，跨行残留也要清掉。
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)
    text = EMPTY_LINK_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def clean_fences_and_prose(text: str) -> str:
    """代码块里只去掉展示用 HTML，块外去掉所有标签。"""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(html.unescape(TAG_RE.sub("", line)))
        else:
            line = TAG_RE.sub("", line)
            line = html.unescape(line)
            out.append(line)
    return "\n".join(out)


def title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def neutralize_relative_links(text: str) -> str:
    """站内相对链接在语料里没有目标页面，保留文字去掉链接。"""
    def replace(match: re.Match[str]) -> str:
        target = match.group(2).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        return match.group(1)

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default=DEFAULT_COMMIT)
    parser.add_argument("--out", type=Path, default=Path("fixtures/demo/fastapi-docs"))
    parser.add_argument("--cache", type=Path, default=Path("/tmp/pagerag-fastapi-cache"))
    args = parser.parse_args()

    docs_dir = args.out / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    total_includes = 0
    total_deduped = 0

    for rel in SELECTED:
        url = f"{RAW_ROOT}/{args.commit}/docs/zh/docs/{rel}"
        raw = strip_frontmatter(fetch(url, args.cache))
        expanded, inlined, deduped = expand_includes(raw, rel, args.commit, args.cache)
        total_includes += inlined
        total_deduped += deduped
        cleaned = clean_markdown(expanded)
        cleaned = neutralize_relative_links(cleaned)
        title = title_of(cleaned, rel)
        source_url = f"{BLOB_ROOT}/{args.commit}/docs/zh/docs/{rel}"
        header = (
            "> 来源：FastAPI 官方中文文档，MIT License。\n"
            f"> 原文：{source_url}\n"
            "> 说明：代码示例已从仓库 docs_src 内联到正文。\n\n"
        )
        content = header + cleaned
        target = docs_dir / rel.replace("/", "__")
        target.write_text(content, encoding="utf-8")
        entries.append({
            "title": title,
            "path": f"docs/{target.name}",
            "source_path": f"docs/zh/docs/{rel}",
            "source_url": source_url,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "characters": len(content),
            "code_blocks": content.count("```") // 2,
            "includes_inlined": inlined,
            "includes_deduped": deduped,
        })
        print(f"  {rel} -> {target.name} ({len(content)} 字符)")

    license_text = fetch(LICENSE_URL, args.cache)
    (args.out / "LICENSE").write_text(license_text, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "name": "FastAPI 官方中文文档",
        "description": "FastAPI 官方中文文档摘编，用于 PageRAG 演示知识库。",
        "source": {
            "repository": "https://github.com/fastapi/fastapi",
            "commit": args.commit,
            "docs_root": "docs/zh/docs",
            "license": "MIT",
            "license_file": "LICENSE",
        },
        "document_count": len(entries),
        "total_characters": sum(int(entry["characters"]) for entry in entries),
        "includes_inlined": total_includes,
        "includes_deduplicated": total_deduped,
        "documents": entries,
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_readme(args.out, manifest)
    print(
        f"完成：{len(entries)} 篇，{manifest['total_characters']} 字符，"
        f"内联代码 {total_includes} 处，去重 {total_deduped} 处"
    )
    return 0


def write_readme(out: Path, manifest: dict[str, object]) -> None:
    source = manifest["source"]
    lines = [
        "# FastAPI 官方中文文档语料",
        "",
        "PageRAG 演示知识库使用这个目录里的 Markdown。内容全部来自 FastAPI 官方仓库的",
        "中文文档，没有生成或改写的业务事实。脚本只把 MkDocs 的代码 include 内联，",
        "并清理页面装饰性 HTML。",
        "",
        "## 来源",
        "",
        f"- 仓库：{source['repository']}",
        f"- 提交：`{source['commit']}`",
        f"- 原文目录：`{source['docs_root']}`",
        "- 许可：MIT License，全文见同目录 `LICENSE`",
        "",
        "## 语料规模",
        "",
        f"- 文档数：{manifest['document_count']}",
        f"- 正文字符数：{manifest['total_characters']}",
        f"- 内联代码文件：{manifest['includes_inlined']} 处",
        f"- 因重复而省略：{manifest['includes_deduplicated']} 处",
        "",
        "## 重新生成",
        "",
        "```bash",
        "cd PageRAG",
        "python scripts/fetch_fastapi_docs.py",
        "```",
        "",
        "每篇文档开头保留原文地址，正文代码来自官方 `docs_src`。派生语料继续使用 MIT",
        "许可，转载或再分发时请保留 `LICENSE` 与本说明。",
        "",
        "## 文件清单",
        "",
        "| 文档 | 字符数 | 原文 |",
        "| --- | --- | --- |",
    ]
    for entry in manifest["documents"]:
        lines.append(
            f"| {entry['title']} | {entry['characters']} | `{entry['source_path']}` |"
        )
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
