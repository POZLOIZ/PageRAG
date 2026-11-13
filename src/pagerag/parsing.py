"""原文解析：统一输出正文与带定位的分段，供分块使用。

Markdown／TXT 整篇一个分段；PDF 每页一个分段，保证块的 page 定位准确。
扫描件（无文本层）显式报错，不把空白当作解析成功。
"""

from dataclasses import dataclass

import fitz


@dataclass
class Segment:
    text: str
    # 该分段正文在整篇文本中的起始字符位置（Unicode 字符索引）。
    char_start: int
    page: int | None = None  # PDF：1-based 页码


@dataclass
class ParsedDocument:
    text: str
    segments: list[Segment]
    parser_version: str
    source_kind: str


def parse_bytes(data: bytes, filename: str) -> ParsedDocument:
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        return _parse_pdf(data)
    if lowered.endswith((".md", ".markdown")):
        text = data.decode("utf-8")
        _require_text(text)
        return ParsedDocument(text, [Segment(text, 0)], parser_version=_parser_version("markdown"),
                              source_kind="md")
    if lowered.endswith(".txt"):
        text = data.decode("utf-8")
        _require_text(text)
        return ParsedDocument(text, [Segment(text, 0)], parser_version=_parser_version("txt"),
                              source_kind="txt")
    raise ValueError(f"不支持的文件类型：{filename}（当前支持 Markdown、TXT、文本层 PDF）")


def _require_text(text: str) -> None:
    # 空文件若被当成正常文档入库，会生成一个永远无法嵌入的空块，并把整个
    # 集合的索引卡在 failed；必须在解析阶段就拒绝。
    if not text.strip():
        raise ValueError("文件没有可提取的文本内容，请检查文件是否为空")


def _parser_version(kind: str) -> str:
    from pagerag.settings import PARSER_VERSION

    return f"{PARSER_VERSION}:{kind}"


def _parse_pdf(data: bytes) -> ParsedDocument:
    pages: list[Segment] = []
    parts: list[str] = []
    offset = 0
    with fitz.open(stream=data, filetype="pdf") as doc:
        for number, page in enumerate(doc, start=1):
            page_text = page.get_text("text")
            parts.append(page_text)
            pages.append(Segment(page_text, offset, number))
            offset += len(page_text)
    full = "".join(parts)
    if not full.strip():
        raise ValueError("PDF 没有可提取的文本层，疑似扫描件；OCR（MinerU）尚未接入")
    return ParsedDocument(full, pages, parser_version=_parser_version("pdf"), source_kind="pdf")
