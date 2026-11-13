"""R2 离线单元测试：候选池、重排结果解析、断句补全与证据单元组装。

不依赖 MySQL/Milvus/模型服务；token 计数用确定性假计数器（len），
不触发 tiktoken 编码加载。
"""

import pytest

from pagerag.bm25 import build_candidate_pool
from pagerag.evidence import (
    UnitChunk,
    cut_mid_sentence,
    merge_texts,
    plan_unit,
    plan_window,
    select_units,
)
from pagerag.rerank import RerankClient, parse_rerank_results
from pagerag.retrieval import Evidence, _apply_rerank


def _uc(chunk_id: int, index: int, text: str, start: int = 0,
        end: int | None = None, rev: int = 1, is_hit: bool = True,
        start_cut: bool | None = None, end_cut: bool | None = None,
        context: dict | None = None) -> UnitChunk:
    return UnitChunk(
        chunk_id=chunk_id, chunk_index=index, text=text, char_start=start,
        char_end=end if end is not None else start + len(text),
        line_start=1, line_end=1, is_hit=is_hit, revision_row_id=rev,
        start_cut=start_cut, end_cut=end_cut, context=context)


def _header(number: int, hit: UnitChunk, members: list[UnitChunk]) -> str:
    return f"[{number}]"


class TestFromSource:
    def test_chunk_row_uses_id_as_pk(self):
        # Chunk 行主键叫 id（不叫 chunk_id）：接线检查实测踩过，回归在此守住。
        from types import SimpleNamespace

        row = SimpleNamespace(id=42, chunk_index=3, text="正文", char_start=0,
                              char_end=2, line_start=1, line_end=1,
                              revision_row_id=7)
        unit = UnitChunk.from_source(row, is_hit=False)
        assert unit.chunk_id == 42 and unit.revision_row_id == 7
        assert unit.is_hit is False

    def test_evidence_uses_chunk_id(self):
        item = Evidence(
            chunk_id=99, document_public_id="d", document_title="t",
            revision_id="r", revision_row_id=1, chunk_index=0, text="x",
            char_start=0, char_end=1, line_start=1, line_end=1, page=None,
            score=0.0, rank=0, content_hash="h")
        assert UnitChunk.from_source(item, is_hit=True).chunk_id == 99

    def test_evidence_carries_cuts_and_context(self):
        # 生产命中块必须保留切块时的边界与章节，窗口补全才会与重放一致。
        item = Evidence(
            chunk_id=99, document_public_id="d", document_title="t",
            revision_id="r", revision_row_id=1, chunk_index=0, text="x",
            char_start=0, char_end=1, line_start=1, line_end=1, page=None,
            score=0.0, rank=0, content_hash="h", start_cut=False,
            end_cut=True, context={"sections": ["第一章"]})
        unit = UnitChunk.from_source(item, is_hit=True)
        assert unit.start_cut is False and unit.end_cut is True
        assert unit.context == {"sections": ["第一章"]}


class TestCandidatePool:
    def test_union_without_selection(self):
        # 只合并不选择：两路全部命中项都在，共有键记录两路名次，未入该路为 None。
        pool = build_candidate_pool([5, 3, 1], [3, 9, 5], pool_size=10)
        by_id = {e["chunk_id"]: e for e in pool}
        assert set(by_id) == {1, 3, 5, 9}
        assert by_id[3] == {"chunk_id": 3, "dense_rank": 2, "bm25_rank": 1,
                            "subquery": 0}
        assert by_id[9]["dense_rank"] is None
        assert by_id[1]["bm25_rank"] is None

    def test_sorted_by_best_rank_then_id(self):
        # 5 与 9 的最好名次并列第 1，按块主键稳定：5 在 9 前。
        pool = build_candidate_pool([5, 3, 1], [9, 5], pool_size=10)
        assert [e["chunk_id"] for e in pool] == [5, 9, 3, 1]

    def test_pool_size_caps(self):
        pool = build_candidate_pool(list(range(1, 11)), list(range(11, 21)), pool_size=5)
        assert len(pool) == 5

    def test_no_fuse_truncation_below_cap(self):
        # 两路各 30、无交集：去重并集 60 全保留——与 RRF"融合即截断"的结构差异。
        pool = build_candidate_pool(list(range(1, 31)), list(range(31, 61)),
                                    pool_size=60)
        assert len(pool) == 60


class TestParseRerankResults:
    def test_expands_to_input_length(self):
        payload = {"results": [{"index": 1, "relevance_score": 0.9},
                               {"index": 0, "relevance_score": 0.4}]}
        assert parse_rerank_results(payload, 3) == [0.4, 0.9, -1.0]

    def test_top_n_truncated_marks_missing(self):
        payload = {"results": [{"index": 2, "relevance_score": 0.7}]}
        assert parse_rerank_results(payload, 3) == [-1.0, -1.0, 0.7]

    def test_out_of_range_index_ignored(self):
        payload = {"results": [{"index": 5, "relevance_score": 1.0}]}
        assert parse_rerank_results(payload, 2) == [-1.0, -1.0]

    def test_empty_results(self):
        assert parse_rerank_results({}, 2) == [-1.0, -1.0]


class TestRerankBatching:
    """邻块扩展后的候选可能超过远端单次 64 段的上限：客户端按 60 分批，
    分数拼回原顺序。逐对分数与批次组成无关（服务端逐对 softmax）。"""

    @staticmethod
    def _client(handler):
        from types import SimpleNamespace

        import httpx
        from pydantic import SecretStr

        settings = SimpleNamespace(
            rerank_url="http://test/rerank", rerank_model="m",
            rerank_api_key=SecretStr(""))
        return RerankClient(settings, transport=httpx.MockTransport(handler))

    @pytest.mark.asyncio
    async def test_splits_at_batch_and_keeps_order(self):
        import json

        import httpx

        batch_sizes: list[int] = []

        def handler(request):
            body = json.loads(request.content)
            size = len(body["documents"])
            batch_sizes.append(size)
            return httpx.Response(200, json={"results": [
                {"index": i, "relevance_score": float(i)} for i in range(size)]})

        client = self._client(handler)
        scores = await client.rerank("问题", [f"文档{i}" for i in range(65)])
        assert batch_sizes == [60, 5]
        assert scores == [float(i) for i in range(60)] + [float(i) for i in range(5)]

    @pytest.mark.asyncio
    async def test_empty_input_makes_no_request(self):
        def handler(request):  # pragma: no cover - 不应被调用
            raise AssertionError("空输入不应发请求")

        client = self._client(handler)
        assert await client.rerank("问题", []) == []


class TestSentenceBoundary:
    def test_ends_with_period(self):
        assert not cut_mid_sentence("句子结束。")
        assert not cut_mid_sentence("Done.")
        assert not cut_mid_sentence("trailing.  \n")

    def test_mid_sentence_tail(self):
        assert cut_mid_sentence("句子被切")
        assert cut_mid_sentence("cut in the mid")

    def test_marks_and_ellipsis(self):
        assert not cut_mid_sentence("省略号…")
        assert not cut_mid_sentence("半角!")


class TestMergeTexts:
    def test_overlap_dedup(self):
        # 重叠区间只保留一份：后块起点落在前块区间内。
        a = _uc(1, 0, "ABCDEFGHIJ", 0, 10)
        b = _uc(2, 1, "EFGHIJKLMNO", 4, 14)
        assert merge_texts([a, b]) == "ABCDEFGHIJKLMNO"

    def test_fully_covered_skipped(self):
        a = _uc(1, 0, "0123456789", 0, 10)
        b = _uc(2, 1, "234567", 2, 8)
        assert merge_texts([a, b]) == "0123456789"

    def test_disjoint_concat(self):
        assert merge_texts([_uc(1, 0, "abc", 0, 3), _uc(2, 1, "def", 3, 6)]) == "abcdef"

    def test_gap_restored_as_newline(self):
        # 区间断档＝块间被切块排除的空白：以换行恢复分隔，不把相邻段落粘死。
        a = _uc(1, 0, "段落一", 0, 3)
        b = _uc(2, 1, "段落二", 5, 8)
        assert merge_texts([a, b]) == "段落一\n段落二"

    def test_order_by_chunk_index_not_input_order(self):
        a = _uc(1, 1, "def", 3, 6)
        b = _uc(2, 0, "abc", 0, 3)
        assert merge_texts([a, b]) == "abcdef"


class TestBoundaryOpen:
    """_boundary_open：记录的边界优先，旧块（None）才回退末尾标点启发式。"""

    def test_recorded_open_wins_over_punctuation(self):
        # 记录为开（围栏/表格行中切）：即使左块恰好以句末符收尾也判开。
        left = _uc(1, 0, "行内容。", end_cut=True)
        right = _uc(2, 1, "下一行")
        from pagerag.evidence import _boundary_open
        assert _boundary_open(left, right) is True

    def test_recorded_closed_wins_over_punctuation(self):
        # 记录为闭（结构边界）：即使左块末尾没有标点也不补邻块。
        left = _uc(1, 0, "段落结束无标点", end_cut=False)
        right = _uc(2, 1, "下一段")
        from pagerag.evidence import _boundary_open
        assert _boundary_open(left, right) is False

    def test_left_none_uses_right_start(self):
        # 旧块（左端 None）＋新块：用右块的 start_cut 记录。
        left = _uc(1, 0, "旧块无记录")
        right = _uc(2, 1, "新块有记录", start_cut=True)
        from pagerag.evidence import _boundary_open
        assert _boundary_open(left, right) is True

    def test_both_none_fallback_punctuation(self):
        # 两块都无记录（旧行）：回退旧行为——看左块末尾标点。
        from pagerag.evidence import _boundary_open
        assert _boundary_open(_uc(1, 0, "句子结束。"), _uc(2, 1, "x")) is False
        assert _boundary_open(_uc(1, 0, "句子被切"), _uc(2, 1, "x")) is True


class TestPlanUnit:
    def _neighbors(self, *chunks: UnitChunk) -> dict:
        return {(c.revision_row_id, c.chunk_index): c for c in chunks}

    def test_prev_added_when_prev_cut(self):
        prev = _uc(1, 0, "前块被切断", is_hit=False)
        hit = _uc(2, 1, "句子结束。")
        members = plan_unit(hit, self._neighbors(prev))
        assert [c.chunk_id for c in members] == [1, 2]
        assert members[0].is_hit is False and members[1].is_hit is True

    def test_next_added_when_hit_cut(self):
        hit = _uc(2, 1, "句子被切断")
        nxt = _uc(3, 2, "后续内容。")
        members = plan_unit(hit, self._neighbors(nxt))
        assert [c.chunk_id for c in members] == [2, 3]
        assert members[1].is_hit is False

    def test_no_completion_when_complete(self):
        prev = _uc(1, 0, "完整句。", is_hit=False)
        hit = _uc(2, 1, "也是完整句。")
        nxt = _uc(3, 2, "下一句。")
        members = plan_unit(hit, self._neighbors(prev, nxt))
        assert [c.chunk_id for c in members] == [2]

    def test_recorded_closed_suppresses_completion(self):
        # 旧启发式会补（左块无标点），但记录为闭（段落边界）→ 不补。
        prev = _uc(1, 0, "段落结束无标点", end_cut=False, is_hit=False)
        hit = _uc(2, 1, "下一段开头。")
        members = plan_unit(hit, self._neighbors(prev))
        assert [c.chunk_id for c in members] == [2]

    def test_recorded_open_forces_completion(self):
        # 旧启发式不补（左块以句末符收尾），但记录为开（表格行中切）→ 补。
        prev = _uc(1, 0, "表格行内容。", end_cut=True, is_hit=False)
        hit = _uc(2, 1, "表格下一行。")
        members = plan_unit(hit, self._neighbors(prev))
        assert [c.chunk_id for c in members] == [1, 2]


class TestPlanWindow:
    def _neighbors(self, *chunks: UnitChunk) -> dict:
        return {(c.revision_row_id, c.chunk_index): c for c in chunks}

    def test_always_adds_adjacent_even_when_boundaries_closed(self):
        prev = _uc(1, 0, "前一段完整。", end_cut=False, is_hit=False)
        hit = _uc(2, 1, "命中段完整。", start_cut=False, end_cut=False)
        nxt = _uc(3, 2, "后一段完整。", start_cut=False, is_hit=False)
        members = plan_window(hit, self._neighbors(prev, nxt))
        assert [c.chunk_id for c in members] == [1, 2, 3]
        assert [c.is_hit for c in members] == [False, True, False]

    def test_same_section_restriction(self):
        hit = _uc(2, 1, "命中。", context={"sections": ["第一章"]})
        prev = _uc(1, 0, "上一节。", is_hit=False,
                   context={"sections": ["第二章"]})
        nxt = _uc(3, 2, "同一节。", is_hit=False,
                  context={"sections": ["第一章"]})
        members = plan_window(hit, self._neighbors(prev, nxt))
        assert [c.chunk_id for c in members] == [2, 3]

    def test_code_block_in_sibling_subsection_is_added(self):
        # 代码示例常单独成块，挂在同一顶层章节的下一个小节；命中代码前的
        # 说明文字时也要补进来，否则模型会误判资料中没有示例。
        hit = _uc(2, 1, "你可以导入它并创建实例。", context={
            "sections": ["# 更大的应用", "## APIRouter", "### 导入 APIRouter"]})
        prev = _uc(1, 0, "```python\nrouter = APIRouter()\n```", is_hit=False,
                   context={"sections": ["# 更大的应用", "## APIRouter",
                                         "### 准备"]})
        members = plan_window(hit, self._neighbors(prev))
        assert [c.chunk_id for c in members] == [1, 2]

    def test_code_block_in_other_top_section_is_not_added(self):
        hit = _uc(2, 1, "命中。", context={"sections": ["# 第一章", "## 小节"]})
        prev = _uc(1, 0, "```python\nprint(1)\n```", is_hit=False,
                   context={"sections": ["# 第二章", "## 小节"]})
        members = plan_window(hit, self._neighbors(prev))
        assert [c.chunk_id for c in members] == [2]

    def test_prose_in_sibling_subsection_is_not_added(self):
        hit = _uc(2, 1, "命中。", context={"sections": ["# 第一章", "## 甲"]})
        nxt = _uc(3, 2, "相邻小节正文。", is_hit=False,
                  context={"sections": ["# 第一章", "## 乙"]})
        members = plan_window(hit, self._neighbors(nxt))
        assert [c.chunk_id for c in members] == [2]

    def test_child_subsection_after_parent_intro_is_added(self):
        # 章节导语只交代主题，定义在紧接的子小节里；两块是父子关系。
        hit = _uc(2, 1, "FastAPI 提供依赖注入系统。",
                  context={"sections": ["## 依赖项"]})
        nxt = _uc(3, 2, "依赖注入是声明依赖并由系统注入。", is_hit=False,
                  context={"sections": ["## 依赖项", "### 什么是依赖注入"]})
        members = plan_window(hit, self._neighbors(nxt))
        assert [c.chunk_id for c in members] == [2, 3]

    def test_parent_intro_before_child_subsection_is_added(self):
        hit = _uc(3, 2, "依赖注入是声明依赖并由系统注入。",
                  context={"sections": ["## 依赖项", "### 什么是依赖注入"]})
        prev = _uc(2, 1, "FastAPI 提供依赖注入系统。", is_hit=False,
                   context={"sections": ["## 依赖项"]})
        members = plan_window(hit, self._neighbors(prev))
        assert [c.chunk_id for c in members] == [2, 3]

    def test_no_section_info_falls_back_to_adjacency(self):
        hit = _uc(2, 1, "命中。", context={"sections": ["第一章"]})
        nxt = _uc(3, 2, "无章节记录。", is_hit=False, context=None)
        members = plan_window(hit, self._neighbors(nxt))
        assert [c.chunk_id for c in members] == [2, 3]

    def test_one_sided_section_info_does_not_block(self):
        hit = _uc(2, 1, "命中。", context=None)
        prev = _uc(1, 0, "有章节。", is_hit=False,
                   context={"sections": ["第一章"]})
        members = plan_window(hit, self._neighbors(prev))
        assert [c.chunk_id for c in members] == [1, 2]

    def test_missing_neighbor_is_skipped(self):
        hit = _uc(2, 1, "命中。")
        assert plan_window(hit, {}) == [hit]


class TestSelectUnits:
    def test_limit_keeps_rank_order(self):
        hits = [_uc(i, i, f"第{i}句。") for i in range(1, 11)]
        units = select_units(hits, {}, unit_limit=3, token_budget=1000,
                             header_builder=_header, token_counter=len)
        assert [u.hit_chunk_ids[0] for u in units] == [1, 2, 3]

    def test_budget_skip_and_continue(self):
        # 大单元放不下 → 跳过并继续试下一个候选（可能更小），不是就此终止。
        big = _uc(1, 0, "x" * 50 + "。")
        small = _uc(2, 5, "小句。")
        units = select_units([big, small], {}, unit_limit=8, token_budget=20,
                             header_builder=_header, token_counter=len)
        assert [u.hit_chunk_ids[0] for u in units] == [2]

    def test_covered_hit_not_reexpanded(self):
        # 已作为补全进过前面单元的命中块，不再重复展开。
        hit1 = _uc(1, 0, "句子被切断", 0, 5)
        hit2 = _uc(2, 1, "继续并结束。", 5, 11)
        neighbors = {(1, 1): hit2}
        units = select_units([hit1, hit2], neighbors, unit_limit=8,
                             token_budget=1000, header_builder=_header,
                             token_counter=len)
        assert len(units) == 1
        assert units[0].hit_chunk_ids == [1]
        assert units[0].completion_chunk_ids == [2]

    def test_header_numbered_by_final_order(self):
        numbers: list[int] = []

        def builder(number, hit, members):
            numbers.append(number)
            return f"[{number}]"

        hits = [_uc(1, 0, "一。"), _uc(2, 2, "二。")]
        units = select_units(hits, {}, unit_limit=8, token_budget=1000,
                             header_builder=builder, token_counter=len)
        assert numbers == [1, 2]
        assert units[1].header == "[2]"

    def test_document_limit_defers_overrepresented_doc(self):
        # 单篇占满名额时，先给别的文档一个单元；被挡下的命中不丢。
        hits = [_uc(1, 0, "一。", rev=1), _uc(2, 1, "二。", rev=1),
                _uc(3, 2, "三。", rev=1), _uc(4, 3, "四。", rev=1),
                _uc(5, 0, "另一篇。", rev=2)]
        units = select_units(hits, {}, unit_limit=4, token_budget=1000,
                             header_builder=_header, token_counter=len,
                             document_limit=3)
        assert [u.hit_chunk_ids[0] for u in units] == [1, 2, 3, 5]
        unlimited = select_units(hits, {}, unit_limit=4, token_budget=1000,
                                 header_builder=_header, token_counter=len)
        assert [u.hit_chunk_ids[0] for u in unlimited] == [1, 2, 3, 4]

    def test_document_limit_second_pass_fills_slots(self):
        # 只有一篇文档时，被挡下的单元在第二轮补回：配额不减少证据数量。
        hits = [_uc(i, i, f"第{i}句。", rev=1) for i in range(1, 6)]
        units = select_units(hits, {}, unit_limit=6, token_budget=1000,
                             header_builder=_header, token_counter=len,
                             document_limit=3)
        assert [u.hit_chunk_ids[0] for u in units] == [1, 2, 3, 4, 5]

    def test_document_limit_zero_is_unlimited(self):
        hits = [_uc(i, i, f"第{i}句。", rev=1) for i in range(1, 6)]
        units = select_units(hits, {}, unit_limit=6, token_budget=1000,
                             header_builder=_header, token_counter=len,
                             document_limit=0)
        assert [u.hit_chunk_ids[0] for u in units] == [1, 2, 3, 4, 5]

    def test_budget_includes_header(self):
        # 预算含渲染头：正文能放下、加头超预算 → 整个单元跳过。
        def builder(number, hit, members):
            return "[1]-header"

        hit = _uc(1, 0, "x" * 20 + "。")  # 正文 21 字符，头 10 字符，共 31
        units = select_units([hit], {}, unit_limit=8, token_budget=30,
                             header_builder=builder, token_counter=len)
        assert units == []
        units = select_units([hit], {}, unit_limit=8, token_budget=31,
                             header_builder=builder, token_counter=len)
        assert len(units) == 1

    def test_window_keeps_hit_and_both_supplements_in_one_unit(self):
        prev = _uc(1, 0, "前段。", 0, 3, is_hit=False)
        hit = _uc(2, 1, "命中段。", 3, 7)
        nxt = _uc(3, 2, "后段。", 7, 10, is_hit=False)
        neighbors = {(1, 0): prev, (1, 2): nxt}
        units = select_units([hit], neighbors, unit_limit=8, token_budget=1000,
                             header_builder=_header, token_counter=len,
                             window=True)
        assert len(units) == 1
        assert units[0].hit_chunk_ids == [2]
        assert units[0].completion_chunk_ids == [1, 3]
        assert [c.chunk_id for c in units[0].chunks] == [1, 2, 3]

    def test_window_shrinks_supplements_before_dropping_hit(self):
        prev = _uc(1, 0, "前" * 20 + "。", 0, 21, is_hit=False)
        hit = _uc(2, 1, "命中正文。", 21, 26)
        nxt = _uc(3, 2, "后" * 20 + "。", 26, 47, is_hit=False)
        neighbors = {(1, 0): prev, (1, 2): nxt}
        units = select_units([hit], neighbors, unit_limit=8, token_budget=40,
                             header_builder=_header, token_counter=len,
                             window=True)
        assert len(units) == 1
        assert units[0].hit_chunk_ids == [2]
        assert units[0].completion_chunk_ids == []
        assert units[0].text == "命中正文。"

    def test_window_does_not_reuse_chunk_across_units(self):
        first = _uc(1, 0, "第一。", 0, 3)
        second = _uc(2, 1, "第二。", 3, 6)
        neighbors = {(1, 1): second, (1, 0): first}
        units = select_units([first, second], neighbors, unit_limit=8,
                             token_budget=1000, header_builder=_header,
                             token_counter=len, window=True)
        assert len(units) == 1
        assert units[0].hit_chunk_ids == [1]
        assert units[0].completion_chunk_ids == [2]


class TestApplyRerank:
    def _evidence(self, chunk_id: int) -> Evidence:
        return Evidence(
            chunk_id=chunk_id, document_public_id="d", document_title="t",
            revision_id="r", revision_row_id=1, chunk_index=0, text="x",
            char_start=0, char_end=1, line_start=1, line_end=1, page=None,
            score=0.0, rank=0, content_hash="h")

    def test_sorts_by_score_and_reassigns_rank(self):
        items = [self._evidence(1), self._evidence(2), self._evidence(3)]
        ordered = _apply_rerank(items, [0.2, 0.9, 0.5])
        assert [e.chunk_id for e in ordered] == [2, 3, 1]
        assert [e.rank for e in ordered] == [1, 2, 3]
        assert ordered[0].score == 0.9

    def test_tie_breaks_by_chunk_id(self):
        items = [self._evidence(9), self._evidence(4)]
        ordered = _apply_rerank(items, [0.5, 0.5])
        assert [e.chunk_id for e in ordered] == [4, 9]


class TestRenderHeaderV3:
    """渲染头 v3：结构上下文（章节路径/表头）只进头部，不进正文与引用区间。"""

    def test_sections_and_table_header_rendered(self):
        from pagerag.answering import _header

        line = _header(
            1, "文档标题", "来源机构", "2026-11-01T00:00:00", 3, 9, 2,
            {"sections": ["# 一级章", "## 二级节"], "table_header": "| 指标 | 数值 |"})
        assert line == ("[1] | 文档标题 | 一级章 > 二级节 | 指标 / 数值 | 来源机构"
                        " | 2026-11-01 | L3-9 | P2")

    def test_legacy_context_identical_to_v2(self):
        # 旧块 context 为 NULL/空：渲染头与 v2 完全一致（快照可比性）。
        from pagerag.answering import _header

        for context in (None, {}, {"sections": [], "table_header": ""}):
            line = _header(2, "标题", "来源", "", 1, 2, None, context)
            assert line == "[2] | 标题 | 来源 | L1-2"

    def test_render_version_bumped(self):
        # 渲染规则变了必须升版本号：评分对照按 render_version 分组。
        from pagerag.answering import EVIDENCE_RENDER_VERSION

        assert EVIDENCE_RENDER_VERSION == "evidence-head-v3"


class TestLoadNeighbors:
    """邻块映射（含版本隔离）：命中块本身进映射；
    版本隔离按修订整体判定——修订在本索引有标记块就只用本版本行，NULL 历史
    行只服务整个修订都没有标记块的旧索引。"""

    @staticmethod
    def _row(chunk_id: int, revision: int, index: int, version: int | None):
        from types import SimpleNamespace

        return SimpleNamespace(
            id=chunk_id, revision_row_id=revision, chunk_index=index,
            index_version_id=version, text=f"块{chunk_id}",
            char_start=0, char_end=3, line_start=1, line_end=1)

    @staticmethod
    def _fake_session(results):
        """execute 依次弹出 results 里的一批行（每次 execute 对应一个 list）。"""

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return list(self._rows)

        class _Session:
            def __init__(self):
                self._pending = list(results)

            async def execute(self, _statement):
                return _Result(self._pending.pop(0) if self._pending else [])

        return _Session()

    @pytest.mark.asyncio
    async def test_hits_included_in_map(self):
        # 相邻两块都被检索到：映射必须包含它们自己，切断的句子才能互相补全。
        from pagerag.answering import _load_neighbors

        hit_a = UnitChunk(chunk_id=11, chunk_index=1, text="句子被切断",
                          char_start=0, char_end=5, line_start=1, line_end=1,
                          is_hit=True, revision_row_id=7)
        hit_b = UnitChunk(chunk_id=12, chunk_index=2, text="的后半段。",
                          char_start=5, char_end=10, line_start=1, line_end=1,
                          is_hit=True, revision_row_id=7)
        session = self._fake_session([])  # 无缺口，不发查询
        neighbors = await _load_neighbors(session, [hit_a, hit_b], index_id=5)
        assert set(neighbors) == {(7, 1), (7, 2)}
        assert neighbors[(7, 1)].is_hit and neighbors[(7, 2)].is_hit

    @pytest.mark.asyncio
    async def test_fetched_neighbors_marked_not_hit(self):
        from pagerag.answering import _load_neighbors

        hit = UnitChunk(chunk_id=11, chunk_index=1, text="句子结束。",
                        char_start=0, char_end=5, line_start=1, line_end=1,
                        is_hit=True, revision_row_id=7)
        # 第一个查询（本索引有标记块的修订）返回空：修订 7 未标记，走 NULL 路径
        session = self._fake_session([[], [self._row(10, 7, 0, None),
                                           self._row(12, 7, 2, None)]])
        neighbors = await _load_neighbors(session, [hit], index_id=5)
        assert set(neighbors) == {(7, 0), (7, 1), (7, 2)}
        assert neighbors[(7, 0)].chunk_id == 10 and not neighbors[(7, 0)].is_hit
        assert neighbors[(7, 2)].chunk_id == 12 and not neighbors[(7, 2)].is_hit
        assert neighbors[(7, 1)].is_hit

    @pytest.mark.asyncio
    async def test_legacy_row_excluded_once_revision_tagged(self):
        # 修订在本索引有标记块：同序号的 NULL 旧行不得进映射（已知的
        # 混入路径之一）——即便 SQL 没挡住，Python 侧同规则双保险也要挡。
        from pagerag.answering import _load_neighbors

        hit = UnitChunk(chunk_id=21, chunk_index=1, text="句子结束。",
                        char_start=0, char_end=5, line_start=1, line_end=1,
                        is_hit=True, revision_row_id=7)
        session = self._fake_session([
            [(7,)],                                  # 修订 7 在索引 5 有标记块
            [self._row(20, 7, 0, 5), self._row(9, 7, 0, None)],
        ])
        neighbors = await _load_neighbors(session, [hit], index_id=5)
        assert neighbors[(7, 0)].chunk_id == 20

    @pytest.mark.asyncio
    async def test_legacy_chunk_beyond_new_count_excluded(self):
        # 审阅的正例：旧版 20 块、新版 8 块——序号 8 只在旧版存在。修订已标记
        # 时，越界的旧块（idx 2 而新版只有 0~1）绝不能当邻块拼进证据。
        from pagerag.answering import _load_neighbors

        hit = UnitChunk(chunk_id=21, chunk_index=1, text="句子结束。",
                        char_start=0, char_end=5, line_start=1, line_end=1,
                        is_hit=True, revision_row_id=7)
        session = self._fake_session([
            [(7,)],
            [self._row(19, 7, 0, 5), self._row(9, 7, 2, None)],
        ])
        neighbors = await _load_neighbors(session, [hit], index_id=5)
        assert set(neighbors) == {(7, 0), (7, 1)}
        assert neighbors[(7, 0)].chunk_id == 19
