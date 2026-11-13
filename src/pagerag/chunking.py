"""结构分块：按文章实际结构切段，语义边界优先于块长。

行为规则：

- 先识别结构，不制造结构。Markdown 的标题、代码围栏、表格自成块元，其余按空行
  分段落；TXT 和 PDF 只有空行分段落，不给无标题文本强行编造章节。
- 段落是最小完整单位。相邻块元合并到软目标 `chunk_chars`；标题起新块并带上其后
  第一个块元，不留光杆标题。
- 单个块元超过硬上限 `chunk_max_chars` 才切开：散文按句末标点切，围栏和表格按行
  切，单句超过硬上限才允许句中切。
- 英文点号后接字母或数字不算句末，保护 URL 与小数；中文句末符号照常。

定位不变量：`text[char_start:char_end] == chunk.text`、首块起于 0、末块终于分段末、
行号从 1 开始、PDF 带页码、哈希稳定。切块时另记录真实边界与结构上下文。真实边界
说明该端是否落在句子、围栏行或表格行中间，断句补全用它而不是从末尾标点反推。
结构上下文保存章节路径和表头，供渲染与展示使用，引用仍指向原文区间。
`evidence.py` 的单元组装按字符区间工作，对切块方式不敏感。
"""

import hashlib
from dataclasses import dataclass

from pagerag.parsing import ParsedDocument
from pagerag.settings import CHUNKER_VERSION, Settings

_SENTENCE_END = set("。！？!?.…")
_HEADING_PREFIXES = tuple(f"{'#' * level} " for level in range(1, 7))


@dataclass
class ChunkRecord:
    chunk_index: int
    text: str
    content_hash: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    page: int | None
    # 真实边界：补全按真实边界决定，不再只看末尾标点。
    # True＝该端落在句子/围栏行/表格行中间；False＝结构边界（段落、标题、句末）。
    start_cut: bool = False
    end_cut: bool = False
    # 结构上下文（章节路径、表头），与原文位置分开保存；引用仍指原文区间。
    context: dict | None = None


@dataclass
class _Block:
    """分段内的一个块元：标题、围栏、表格或空行分隔的段落（相对分段坐标）。

    label/level：标题的文本与层级（# 数），表格首行文本——供块组装时记录
    章节路径与表头上下文；段落与围栏不标。
    """

    start: int
    end: int
    is_heading: bool = False
    is_lines: bool = False  # 围栏/表格：超上限时按行切而不是按句切
    label: str = ""
    level: int = 0


def _line_of(text: str, char_index: int) -> int:
    """字符位置对应的 1-based 行号。"""
    return text.count("\n", 0, char_index) + 1


def _blocks_of(segment_text: str, is_markdown: bool) -> list[_Block]:
    """空行分段落；Markdown 另识别标题、代码围栏（``` 起至 ``` 止）与表格
    （| 开头的连续行）。围栏内的 # 与空行不产生边界。"""
    blocks: list[_Block] = []
    para_start: int | None = None
    para_end = 0
    in_fence = False
    fence_start = 0
    table_start: int | None = None
    table_end = 0
    table_first_line = ""
    offset = 0
    for line in segment_text.splitlines(keepends=True):
        stripped = line.strip()
        if in_fence:
            offset += len(line)
            if stripped.startswith("```"):
                blocks.append(_Block(fence_start, offset, is_lines=True))
                in_fence = False
            continue
        starts_table = is_markdown and stripped.startswith("|")
        if in_fence is False and (is_markdown and stripped.startswith("```")):
            _close_paragraph(blocks, para_start, para_end)
            _close_table(blocks, table_start, table_end, table_first_line)
            para_start, table_start = None, None
            in_fence = True
            fence_start = offset
            offset += len(line)
            continue
        if not stripped:
            _close_paragraph(blocks, para_start, para_end)
            _close_table(blocks, table_start, table_end, table_first_line)
            para_start, table_start = None, None
            offset += len(line)
            continue
        if starts_table:
            _close_paragraph(blocks, para_start, para_end)
            para_start = None
            if table_start is None:
                table_start = offset
                table_first_line = stripped
            offset += len(line)
            table_end = offset
            continue
        if table_start is not None:
            _close_table(blocks, table_start, table_end, table_first_line)
            table_start = None
        if is_markdown and stripped.startswith(_HEADING_PREFIXES):
            _close_paragraph(blocks, para_start, para_end)
            para_start = None
            level = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(_Block(offset, offset + len(line), is_heading=True,
                                 label=stripped, level=level))
            offset += len(line)
            continue
        if para_start is None:
            para_start = offset
        offset += len(line)
        para_end = offset
    if in_fence:  # 未闭合围栏：整体成块
        blocks.append(_Block(fence_start, len(segment_text), is_lines=True))
    _close_paragraph(blocks, para_start, para_end)
    _close_table(blocks, table_start, table_end, table_first_line)
    return blocks


def _close_paragraph(blocks: list[_Block], start: int | None, end: int) -> None:
    if start is not None:
        blocks.append(_Block(start, end))


def _close_table(blocks: list[_Block], start: int | None, end: int,
                 first_line: str = "") -> None:
    if start is not None:
        blocks.append(_Block(start, end, is_lines=True, label=first_line))


def _pack_blocks(blocks: list[_Block], target: int) -> list[list[_Block]]:
    """相邻块元贪心合并到软目标；标题起新组并强制带其后第一个块元。"""
    groups: list[list[_Block]] = []
    current: list[_Block] = []
    for block in blocks:
        if not current:
            current = [block]
            continue
        if block.is_heading:
            groups.append(current)
            current = [block]
            continue
        if current[-1].is_heading:
            current.append(block)  # 不留光杆标题
            continue
        span = current[-1].end - current[0].start
        if span + (block.end - block.start) <= target:
            current.append(block)
        else:
            groups.append(current)
            current = [block]
    if current:
        groups.append(current)
    return groups


def _is_sentence_end(text: str, index: int) -> bool:
    char = text[index]
    if char not in _SENTENCE_END:
        return False
    # 英文点号后跟字母/数字不是句末：URL、小数、版本号。
    if char == "." and index + 1 < len(text) and (text[index + 1].isalnum()):
        return False
    return True


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """句区间（相对传入文本）：终点是句末标点；无标点的尾巴也算一句。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for i in range(len(text)):
        if _is_sentence_end(text, i):
            spans.append((start, i + 1))
            start = i + 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _line_spans(text: str, cap: int) -> list[tuple[int, int]]:
    """按行累积成不超过 cap 的区间（围栏/表格超上限时用）：加下一行会超就
    先封组，多行组永远不超上限且行保持完整；单行本身超上限的组交给
    上层句中硬切兜底。"""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if start is None:
            start = offset
        if offset > start and offset + len(line) - start > cap:
            spans.append((start, offset))
            start = offset
        offset += len(line)
    if start is not None and offset > start:
        spans.append((start, offset))
    return spans or [(0, len(text))]


def _split_oversized(segment_text: str, start: int, end: int, cap: int,
                     by_lines: bool) -> list[tuple[int, int, bool, bool]]:
    """超上限块元的切分（相对分段坐标）：围栏/表格按行，散文按句；
    单行/单句超过上限才句中切（模型输入的最终兜底）。

    每段返回 (相对起点, 相对终点, 该端起点是否被切, 该端终点是否被切)：
    行组边界落在围栏/表格行与行之间（块在真实结构中被切断）→ 开；
    散文按句切的边界落在句末 → 闭；硬切兜底的边界 → 开。补全决策用这份
    记录的事实，不再从末尾标点反推。
    """
    body = segment_text[start:end]
    raw: list[tuple[int, int, bool]] = []  # (相对起, 相对止, 该段终点是否开边界)
    if by_lines:
        for s, e in _line_spans(body, cap):
            raw.append((s, e, True))  # 行组之间都是行中断开
    else:
        piece_start: int | None = None
        piece_end = 0
        for s, e in _sentence_spans(body):
            if piece_start is None:
                piece_start, piece_end = s, e
            elif e - piece_start <= cap:
                piece_end = e
            else:
                raw.append((piece_start, piece_end, False))
                piece_start, piece_end = s, e
        if piece_start is not None:
            raw.append((piece_start, piece_end, False))
    result: list[tuple[int, int, bool]] = []
    for s, e, _ in raw:  # 单行/单句兜底：硬切，保证任何块都不超模型输入上限
        while e - s > cap:
            result.append((start + s, start + s + cap, True))
            s += cap
        result.append((start + s, start + e, _))
    spans: list[tuple[int, int, bool, bool]] = []
    total = len(result)
    for i, (s, e, cut) in enumerate(result):
        # 组首/组尾是块元边界（结构边界），不因切分而变开；
        # 段内边界：起点开 ⇔ 前一段终点开。
        start_open = i > 0 and result[i - 1][2]
        end_open = i < total - 1 and cut
        spans.append((s, e, start_open, end_open))
    return spans


def chunk_parsed(parsed: ParsedDocument, settings: Settings) -> list[ChunkRecord]:
    # 空正文不产空块：空块无法嵌入，会让整个索引永久停在 failed。
    # 正常上传在解析阶段已拒绝空文件，这里只做最后一道防线。
    if not parsed.text.strip():
        raise ValueError("文档没有可切分的正文")
    is_markdown = parsed.source_kind == "md"
    target = settings.chunk_chars
    cap = settings.chunk_max_chars
    # (绝对起, 绝对止, 页码, 起点被切, 终点被切, 结构上下文)
    spans: list[tuple[int, int, int | None, bool, bool, dict | None]] = []
    sections: dict[int, str] = {}  # 标题层级 -> 标题文本；跨分段连续（页断章不清）
    for segment in parsed.segments:
        seg_text = segment.text
        if not seg_text:
            continue  # 空白页不产块（fixed-char-v1 会产空块，属污染）
        blocks = _blocks_of(seg_text, is_markdown) or [_Block(0, len(seg_text))]
        rel_spans: list[tuple[int, int, bool, bool]] = []
        contexts: list[dict | None] = []
        for group in _pack_blocks(blocks, target):
            g_start, g_end = group[0].start, group[-1].end
            # 章节路径：标题总在组首（打包规则保证），先截掉本级及以下的旧路径
            # 再登记本级标题；本组之后的所有组都携带更新后的路径。
            head = group[0]
            if head.is_heading and head.level:
                for level in [key for key in sections if key >= head.level]:
                    del sections[level]
                sections[head.level] = head.label
            context: dict = {}
            path = [sections[level] for level in sorted(sections) if sections[level]]
            if path:
                context["sections"] = path
            table = next((b for b in group if b.is_lines and b.label), None)
            if table is not None:
                context["table_header"] = table.label
            if g_end - g_start > cap:
                pieces = _split_oversized(seg_text, g_start, g_end, cap,
                                          by_lines=group[-1].is_lines)
            else:
                pieces = [(g_start, g_end, False, False)]
            rel_spans.extend(pieces)
            contexts.extend([context or None] * len(pieces))
        if not rel_spans:
            rel_spans = [((0, len(seg_text)) + (False, False))]
            contexts = [None]
        # 覆盖不变量：首块起于分段头、末块终于分段尾（块间空白不计入任何块）。
        rel_spans[0] = ((0, rel_spans[0][1]) + rel_spans[0][2:])
        rel_spans[-1] = ((rel_spans[-1][0], len(seg_text)) + rel_spans[-1][2:])
        for (s, e, start_cut, end_cut), context in zip(rel_spans, contexts):
            spans.append((segment.char_start + s, segment.char_start + e,
                          segment.page, start_cut, end_cut, context))
    records: list[ChunkRecord] = []
    for index, (abs_start, abs_end, page, start_cut, end_cut, context) in enumerate(spans):
        chunk_text = parsed.text[abs_start:abs_end]
        records.append(
            ChunkRecord(
                chunk_index=index,
                text=chunk_text,
                content_hash=_hash_text(chunk_text),
                char_start=abs_start,
                char_end=abs_end,
                line_start=_line_of(parsed.text, abs_start),
                line_end=_line_of(parsed.text, max(abs_end - 1, abs_start)),
                page=page,
                start_cut=start_cut,
                end_cut=end_cut,
                context=context,
            )
        )
    return records


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunker_version() -> str:
    return CHUNKER_VERSION
