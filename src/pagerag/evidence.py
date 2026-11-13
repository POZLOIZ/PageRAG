"""证据单元组装：从重排后的块列表选出少量完整单元（R2 的上下文组织侧）。

流程：按重排名次逐个取块 → 断句处补邻块恢复被切断的
句子 → 跨单元去重（同一块只进一个单元）→ 在 token 预算内填充，预算含
渲染头；放不下的单元跳过，继续试下一个候选。

块以 UnitChunk 描述（字符区间 + 行号 + 所属修订），不绑定 500 字符切块：
断句与区间合并规则对结构分块（段落/父子）同样适用，换分块不需要改这里。
"""

from dataclasses import dataclass, field, replace
from typing import Callable

# 断句判断用窄集合：宁可多补一块（预算兜底），不把被切断的句子漏在外面。
SENTENCE_END = set("。！？!?.…")

_ENCODING = None


def count_tokens(text: str) -> int:
    """tiktoken cl100k 近似计数。生成模型的真实 tokenizer 不同，这里只求
    同一口径内可比（含渲染头计入）；首次调用会拉取编码文件，之后走缓存。"""
    global _ENCODING
    if _ENCODING is None:
        import tiktoken

        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return len(_ENCODING.encode(text))


def cut_mid_sentence(text: str) -> bool:
    """文本结尾不是断句符 → 与下一块之间是被切断的句子。

    启发式兜底：只对没有边界记录的旧块用（_boundary_open 里 None 分支）；
    新块以切块时记录的 start_cut/end_cut 为准。
    """
    tail = text.rstrip()[-1:]
    return tail not in SENTENCE_END


@dataclass
class UnitChunk:
    chunk_id: int
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    is_hit: bool
    # (revision_row_id, chunk_index)：邻块查询键，不参与合并逻辑本身。
    revision_row_id: int = 0
    # 切块时记录的真实边界：None＝旧行/旧对象没有记录，
    # 回退末尾标点启发式；True/False＝记录在案的事实，直接使用。
    start_cut: bool | None = None
    end_cut: bool | None = None
    # 结构上下文（sections / table_header），窗口模式用来限制同章节补全。
    context: dict | None = None
    # 命中块由哪条查询召回：0＝原问题，1..＝子查询；邻块继承触发它的命中块。
    source_subquery: int = 0

    @classmethod
    def from_source(cls, source, is_hit: bool) -> "UnitChunk":
        """Evidence 与 Chunk 行共用同一组字段名，直接拷贝；唯一差异是主键
        名（Evidence.chunk_id / Chunk.id），这里统一成 chunk_id。"""
        chunk_id = getattr(source, "chunk_id", None)
        if chunk_id is None:
            chunk_id = source.id
        return cls(
            chunk_id=chunk_id, chunk_index=source.chunk_index,
            text=source.text, char_start=source.char_start,
            char_end=source.char_end, line_start=source.line_start,
            line_end=source.line_end, is_hit=is_hit,
            revision_row_id=getattr(source, "revision_row_id", 0),
            start_cut=getattr(source, "start_cut", None),
            end_cut=getattr(source, "end_cut", None),
            context=getattr(source, "context", None),
            source_subquery=getattr(source, "source_subquery", 0),
        )


def _boundary_open(left: UnitChunk, right: UnitChunk) -> bool:
    """两块之间的边界是否落在句子/围栏行/表格行中间：任一侧有切块时记录的
    真实边界就用记录（同一边界两侧记录一致），都没有（旧块）才回退到"看
    左块末尾标点"的启发式（旧行为）。"""
    if left.end_cut is not None:
        return left.end_cut
    if right.start_cut is not None:
        return right.start_cut
    return cut_mid_sentence(left.text)


@dataclass
class EvidenceUnit:
    chunks: list[UnitChunk] = field(default_factory=list)  # 按 chunk_index 升序
    text: str = ""
    header: str = ""

    @property
    def hit_chunk_ids(self) -> list[int]:
        return [c.chunk_id for c in self.chunks if c.is_hit]

    @property
    def completion_chunk_ids(self) -> list[int]:
        return [c.chunk_id for c in self.chunks if not c.is_hit]


def merge_texts(chunks: list[UnitChunk]) -> str:
    """按字符区间拼接块文本，重叠区间只保留一份；块间被切块排除的空白以
    换行恢复分隔（拼接不再把相邻段落直接粘死；分隔符是可读性
    恢复，不逐字符还原原文空白）。

    块是原文的字符切片（分块带 overlap 时相邻区间重叠）；直接字符串拼接
    会复制重叠段，这里以区间为准去重后重组。
    """
    text = ""
    covered_to: int | None = None
    for chunk in sorted(chunks, key=lambda c: c.chunk_index):
        if covered_to is not None:
            if chunk.char_end <= covered_to:
                continue  # 完全被已拼接区间覆盖
            if chunk.char_start < covered_to:
                text += chunk.text[covered_to - chunk.char_start:]
            else:
                if chunk.char_start > covered_to:
                    text += "\n"  # 区间断档＝块间被排除的空白，恢复分隔
                text += chunk.text
        else:
            text += chunk.text
        covered_to = chunk.char_end
    return text


def plan_unit(hit: UnitChunk,
              neighbors: dict[tuple[int, int], UnitChunk]) -> list[UnitChunk]:
    """单个命中块的单元规划：边界落在真实结构中间时补上邻块。

    前块与命中块之间被切 → 补前块；命中块与后块之间被切 → 补后块。
    是否"被切"优先用切块时记录的真实边界（start_cut/end_cut），旧块没有
    记录才看前块末尾标点（旧行为兜底）。
    """
    members: list[UnitChunk] = []
    prev = neighbors.get((hit.revision_row_id, hit.chunk_index - 1))
    if prev is not None and _boundary_open(prev, hit):
        members.append(replace(prev, is_hit=False))
    members.append(hit)
    nxt = neighbors.get((hit.revision_row_id, hit.chunk_index + 1))
    if nxt is not None and _boundary_open(hit, nxt):
        members.append(replace(nxt, is_hit=False))
    return members


def _section_path(chunk: UnitChunk) -> tuple[str, ...]:
    """块的结构章节路径；没有记录时返回空元组（旧文档/无标题文本）。"""
    context = chunk.context if isinstance(chunk.context, dict) else {}
    return tuple(context.get("sections") or ())


def _same_section(left: UnitChunk, right: UnitChunk) -> bool:
    """两侧都有章节记录时要求完全同路径；至少一侧没有记录则不拦邻块。

    没有章节信息的文档（TXT/PDF 无标题）本来只能按相邻段落补；只有一侧
    有记录时也无法证明跨章节，保守地交给窗口预算判断。
    """
    left_sections = _section_path(left)
    right_sections = _section_path(right)
    if left_sections and right_sections:
        return left_sections == right_sections
    return True


def _is_code_block(chunk: UnitChunk) -> bool:
    return chunk.text.lstrip().startswith("```")


def _same_top_section(left: UnitChunk, right: UnitChunk) -> bool:
    left_sections = _section_path(left)
    right_sections = _section_path(right)
    return bool(left_sections and right_sections
                and left_sections[0] == right_sections[0])


def _adjacent_section(left: UnitChunk, right: UnitChunk) -> bool:
    """两侧是否正好是父子章节。

    章节开头的导语与紧接的小节正文常被切成两块。例如 `## 依赖项` 的开头
    只说明 FastAPI 提供依赖注入，下一块 `### 什么是「依赖注入」` 才给出
    定义。两块是父子关系而不是兄弟关系，补进窗口不会串到无关小节。
    """
    left_sections = _section_path(left)
    right_sections = _section_path(right)
    if not left_sections or not right_sections:
        return False
    shorter, longer = ((left_sections, right_sections)
                       if len(left_sections) < len(right_sections)
                       else (right_sections, left_sections))
    return (len(longer) == len(shorter) + 1
            and longer[:len(shorter)] == shorter)


def _window_neighbor_allowed(hit: UnitChunk, neighbor: UnitChunk) -> bool:
    """窗口补块是否允许跨小节。

    文档的代码示例常单独成块，紧跟在上一小节的说明文字后面，例如
    `## APIRouter` 下的 `### 导入 APIRouter` 说明与随后的代码块。若严格
    要求完整章节路径相同，命中说明文字时永远补不到代码，答案就会声称
    "没有代码示例"。同一顶层章节内的相邻代码块允许跨小节补入；父子章节
    的导语与正文也允许相邻补入。普通兄弟小节仍按完整路径限制，避免把
    相邻小节的开头拼进证据。
    """
    if _same_section(hit, neighbor) or _adjacent_section(hit, neighbor):
        return True
    return _is_code_block(neighbor) and _same_top_section(hit, neighbor)


def plan_window(hit: UnitChunk,
                neighbors: dict[tuple[int, int], UnitChunk]) -> list[UnitChunk]:
    """窗口规划：命中块前后各补一块，不再由断句标记触发。

    只在同修订、同索引版本的邻块映射里取块（调用方保证）；两侧都有章节
    记录时限制在同一章节，避免把相邻章节的开头拼进证据。保持原文顺序，
    不递归扩展到整篇。
    """
    members: list[UnitChunk] = []
    prev = neighbors.get((hit.revision_row_id, hit.chunk_index - 1))
    if prev is not None and _window_neighbor_allowed(hit, prev):
        members.append(replace(prev, is_hit=False))
    members.append(hit)
    nxt = neighbors.get((hit.revision_row_id, hit.chunk_index + 1))
    if nxt is not None and _window_neighbor_allowed(hit, nxt):
        members.append(replace(nxt, is_hit=False))
    return members


def select_units(
    hits: list[UnitChunk],
    neighbors: dict[tuple[int, int], UnitChunk],
    unit_limit: int,
    token_budget: int,
    header_builder: Callable[[int, UnitChunk, list[UnitChunk]], str],
    token_counter: Callable[[str], int] = count_tokens,
    document_limit: int = 0,
    window: bool = False,
    group_limit: int = 0,
) -> list[EvidenceUnit]:
    """按重排名次填充证据单元：条数与 token 预算（含渲染头）双重约束。

    - window=False：旧行为，只在边界被切断时补邻块（对照）；
    - window=True：命中块前后各一块组成窗口，邻块不单独参与重排竞争；
      窗口放不下时先去掉补充块，命中正文仍放不下才跳过；
    - 命中块已被前面的单元覆盖（作为补全进过上下文）→ 跳过，不重复展开；
    - 单元放不进剩余预算 → 跳过并继续试下一个候选（可能更小）；
    - header_builder 收到（临时编号, 命中块, 成员块），编号即最终上下文序号；
    - document_limit > 0 时先按"同一修订最多 N 个单元"填一轮，修订在本索引
      内即一篇文档，被挡下的命中不丢弃，第一轮结束后按原名次补满名额。
      这一层解决"一篇文档的多个块占满上下文、把别的来源挤出名额"的问题；
      名额没被占满时第二轮回填，不会因为配额少给证据。
    - group_limit > 0 且命中来自多个子查询时，第一轮每个子问题最多占 N 个
      单元，被挡下的命中同样在第二轮回填。只有一个子查询时不生效，避免把
      单查询的证据总量压到 N。
    """
    units: list[EvidenceUnit] = []
    used_chunk_ids: set[int] = set()
    used_tokens = 0
    per_document: dict[int, int] = {}
    per_group: dict[int, int] = {}
    groups = {hit.source_subquery for hit in hits}
    grouped = group_limit > 0 and len(groups) > 1

    def fit(hit: UnitChunk, members: list[UnitChunk]) -> tuple[str, str, int] | None:
        text = merge_texts(members)
        header = header_builder(len(units) + 1, hit, members)
        cost = token_counter(text) + token_counter(header)
        if used_tokens + cost > token_budget:
            return None
        return text, header, cost

    def take(hit: UnitChunk) -> bool:
        nonlocal used_tokens
        if hit.chunk_id in used_chunk_ids:
            return False
        planner = plan_window if window else plan_unit
        members = [m for m in planner(hit, neighbors)
                   if m.chunk_id not in used_chunk_ids]
        if not members:
            return False
        fitted = fit(hit, members)
        if fitted is None and window and len(members) > 1:
            # 预算不足时先缩补充部分：命中正文保留，补充块下次仍可独立命中。
            members = [hit]
            fitted = fit(hit, members)
        if fitted is None:
            return False
        text, header, cost = fitted
        used_chunk_ids.update(m.chunk_id for m in members)
        used_tokens += cost
        units.append(EvidenceUnit(chunks=members, text=text, header=header))
        return True

    deferred: list[UnitChunk] = []
    for hit in hits:
        if len(units) >= unit_limit:
            break
        if document_limit and per_document.get(hit.revision_row_id, 0) >= document_limit:
            deferred.append(hit)
            continue
        if grouped and per_group.get(hit.source_subquery, 0) >= group_limit:
            deferred.append(hit)
            continue
        if take(hit):
            per_document[hit.revision_row_id] = per_document.get(
                hit.revision_row_id, 0) + 1
            per_group[hit.source_subquery] = per_group.get(
                hit.source_subquery, 0) + 1
    for hit in deferred:
        if len(units) >= unit_limit:
            break
        take(hit)
    return units
