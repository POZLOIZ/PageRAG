"""离线单元测试：分块定位、解析与集合/修订模型。不依赖 MySQL/Milvus/模型服务。"""

from pathlib import Path

import pytest

from pagerag.bm25 import rrf_fuse
from pagerag.chunking import chunk_parsed
from pagerag.llm import FallbackChatLLM, endpoint_label, is_rate_limited
from pagerag.parsing import ParsedDocument, Segment, parse_bytes
from pagerag.settings import Settings, index_build_config


def make_settings(**overrides) -> Settings:
    values = {"chunk_chars": 100, "chunk_max_chars": 200,
              "data_dir": Path("/tmp/pagerag-test"),
              # 显式给出重排配置，避免离线测试依赖本机 .env。
              "rerank_url": "http://127.0.0.1:18080/rerank",
              "rerank_model": "Qwen/Qwen3-Reranker-0.6B"}
    values.update(overrides)
    return Settings(_env_file=None, **values,
                    database_url="", llm_base_url="", llm_model="")


class TestChunking:
    """结构分块（structural-v1）：切在哪由结构决定，长度只是软目标/硬兜底。"""

    def _parse(self, text: str, kind: str = "md") -> ParsedDocument:
        return ParsedDocument(text, [Segment(text, 0)],
                              parser_version="test", source_kind=kind)

    def test_empty_document_is_rejected(self):
        # 空正文不能产出一个永远无法嵌入的空块，否则整个索引会卡在 failed。
        with pytest.raises(ValueError, match="正文"):
            chunk_parsed(self._parse(""), make_settings())

    def test_short_paragraphs_merge_to_soft_target(self):
        # 三个 30 字符短段落（目标 100）：贪心合并成一个块，区间重建原文。
        text = "甲" * 30 + "\n\n" + "乙" * 30 + "\n\n" + "丙" * 30
        records = chunk_parsed(self._parse(text, "txt"), make_settings())
        assert len(records) == 1
        assert records[0].text == text

    def test_offsets_reconstruct_text(self):
        # 无空行单段落超过硬上限 200 → 按句切；每块可重建、不超上限、覆盖完整。
        text = "第一段内容。第二段内容。" * 20
        records = chunk_parsed(self._parse(text, "txt"), make_settings())
        assert len(records) > 1
        for record in records:
            assert text[record.char_start:record.char_end] == record.text
            assert record.char_end - record.char_start <= 200
        assert records[0].char_start == 0
        assert records[-1].char_end == len(text)

    def test_oversized_paragraph_cuts_at_sentence_end(self):
        # 超上限的散文按句末切：每块以句末符结束；小数与 URL 的点号不是句末。
        text = "价格是3.14元。访问 https://a.example.com/site 查询。" * 10
        records = chunk_parsed(self._parse(text, "txt"), make_settings())
        assert len(records) > 1
        for record in records:
            assert record.text.endswith("。")
        assert any("3.14" in r.text for r in records)
        assert any("a.example.com" in r.text for r in records)

    def test_single_sentence_over_cap_hard_cut(self):
        # 无标点单句超过硬上限：句中硬切是最终兜底，块长恰为上限。
        text = "无" * 500
        records = chunk_parsed(self._parse(text, "txt"), make_settings())
        assert [r.char_end - r.char_start for r in records] == [200, 200, 100]

    def test_heading_carries_next_block(self):
        # 标题不带光杆：标题与其后第一个块元同块；再放不下才开新块。
        text = "# 标题\n\n" + "长" * 120 + "。\n\n" + "再" * 120 + "。"
        records = chunk_parsed(self._parse(text), make_settings())
        assert len(records) == 2
        assert "# 标题" in records[0].text
        assert "长" * 120 in records[0].text

    def test_fence_atomic(self):
        # 围栏是完整块元：小于硬上限时绝不从中间切开。
        fence = "```python\n" + "x" * 130 + "\n```"
        text = "前" * 90 + "。\n\n" + fence
        records = chunk_parsed(self._parse(text), make_settings())
        assert len(records) == 2
        assert records[1].text == fence

    def test_oversized_fence_splits_between_lines(self):
        # 围栏超上限按行切：行保持完整（除末块外都以换行收尾），组不超上限。
        fence = "```\n" + "\n".join(f"row{i} " + "x" * 30 for i in range(15)) + "\n```"
        records = chunk_parsed(self._parse(fence), make_settings())
        assert "".join(r.text for r in records) == fence
        for record in records:
            assert record.char_end - record.char_start <= 200
            assert record.text.endswith("\n") or record is records[-1]

    def test_line_numbers(self):
        text = "a\nb\nc\nd"
        records = chunk_parsed(self._parse(text, "txt"), make_settings())
        assert records[0].line_start == 1
        assert records[0].line_end == 4

    def test_pdf_pages_not_crossed(self):
        page1 = "p" * 150
        page2 = "q" * 150
        segments = [Segment(page1, 0, 1), Segment(page2, 150, 2)]
        parsed = ParsedDocument(page1 + page2, segments, "pdf", "test")
        records = chunk_parsed(parsed, make_settings())
        assert {r.page for r in records} == {1, 2}
        for record in records:
            page_text = page1 if record.page == 1 else page2
            assert record.text in page_text

    def test_blank_pdf_page_skipped(self):
        # 空白页不产块；末块仍终于文档末尾。
        page1 = "p" * 80
        segments = [Segment(page1, 0, 1), Segment("", 80, 2)]
        parsed = ParsedDocument(page1, segments, "pdf", "test")
        records = chunk_parsed(parsed, make_settings())
        assert len(records) == 1
        assert records[0].page == 1
        assert records[0].char_end == len(page1)

    def test_content_hash_stable(self):
        text = "同样的文本"
        first = chunk_parsed(self._parse(text), make_settings())
        second = chunk_parsed(self._parse(text), make_settings())
        assert [r.content_hash for r in first] == [r.content_hash for r in second]

    def test_boundary_flags_sentence_cut_closed(self):
        # 散文按句切：块间边界落在句末 → 记录为闭（False）；首尾是块元边界也闭。
        text = "第一段内容。第二段内容。" * 20
        records = chunk_parsed(self._parse(text, "txt"), make_settings())
        assert len(records) > 1
        for record in records:
            assert record.start_cut is False
            assert record.end_cut is False

    def test_boundary_flags_hard_cut_open(self):
        # 句中硬切：中间各块两端都记录为开（True）；组首/组尾仍是结构边界（闭）。
        text = "无" * 500
        records = chunk_parsed(self._parse(text, "txt"), make_settings())
        assert len(records) == 3
        assert records[0].start_cut is False and records[0].end_cut is True
        assert records[1].start_cut is True and records[1].end_cut is True
        assert records[2].start_cut is True and records[2].end_cut is False

    def test_boundary_flags_fence_line_cut_open(self):
        # 围栏按行切：行组之间的边界落在围栏行中间 → 记录为开。
        fence = "```\n" + "\n".join(f"row{i} " + "x" * 30 for i in range(15)) + "\n```"
        records = chunk_parsed(self._parse(fence), make_settings())
        assert len(records) > 1
        for record in records[1:]:
            assert record.start_cut is True
        for record in records[:-1]:
            assert record.end_cut is True

    def test_context_sections_path(self):
        # 章节路径随标题层级更新：## 挂在 # 之下；同级新标题截掉旧下级路径。
        # 标题所在的组先登记本级标题再建 context，所以组自己也在路径里。
        text = ("# 一级\n\n" + "甲" * 30 + "\n\n"
                "## 二级\n\n" + "乙" * 30 + "\n\n"
                "# 另一级\n\n" + "丙" * 30)
        records = chunk_parsed(self._parse(text), make_settings())
        assert len(records) == 3
        assert records[0].context == {"sections": ["# 一级"]}
        assert records[1].context == {"sections": ["# 一级", "## 二级"]}
        assert records[2].context == {"sections": ["# 另一级"]}

    def test_context_table_header(self):
        # 表格块携带表头（首行）；同一组里的普通段落不带表头。
        table = "| 指标 | 数值 |\n|---|---|\n| 营收 | 10 |"
        text = "# 报表\n\n" + table
        records = chunk_parsed(self._parse(text), make_settings())
        assert len(records) == 1
        assert records[0].context == {
            "sections": ["# 报表"], "table_header": "| 指标 | 数值 |"}

    def test_context_sections_persist_across_segments(self):
        # 章节路径跨分段连续：分段（页）断章不清路径（sections 字典在分段循环外）。
        # 当前解析器只有 PDF 会多分段且 PDF 无标题，这里用 md 多分段验证构造保证，
        # 为未来多分段解析器守住该行为。
        head = "# 章节\n\n" + "甲" * 80
        body = "乙" * 80
        segments = [Segment(head, 0), Segment(body, len(head))]
        parsed = ParsedDocument(head + body, segments, "test", "md")
        records = chunk_parsed(parsed, make_settings())
        assert len(records) == 2
        assert records[0].context == {"sections": ["# 章节"]}
        assert records[1].context == {"sections": ["# 章节"]}


class TestParsing:
    def test_markdown(self, tmp_path):
        parsed = parse_bytes("# 标题\n正文".encode(), "doc.md")
        assert parsed.source_kind == "md"
        assert parsed.segments[0].page is None

    def test_pdf_text_layer(self, tmp_path):
        import fitz

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "port 13306")
        doc.new_page().insert_text((72, 72), "dim 1024")
        data = doc.tobytes()
        doc.close()
        parsed = parse_bytes(data, "doc.pdf")
        assert parsed.source_kind == "pdf"
        assert [s.page for s in parsed.segments] == [1, 2]
        assert "13306" in parsed.segments[0].text

    def test_scanned_pdf_rejected(self):
        import fitz

        doc = fitz.open()
        doc.new_page()
        data = doc.tobytes()
        doc.close()
        with pytest.raises(ValueError, match="扫描件"):
            parse_bytes(data, "scan.pdf")

    def test_unsupported(self):
        with pytest.raises(ValueError, match="不支持"):
            parse_bytes(b"x", "doc.docx")

    def test_empty_text_file_rejected(self):
        with pytest.raises(ValueError, match="没有可提取"):
            parse_bytes(b"  \n\t\n", "doc.txt")


class _FakeChat:
    def __init__(self, outcome=None, error=None):
        self._outcome = outcome
        self._error = error
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._outcome


class TestFallbackChatLLM:
    def test_is_rate_limited(self):
        assert is_rate_limited(Exception("Error code: 429 - rate limit reached"))
        assert is_rate_limited(Exception("Rate limit exceeded"))
        assert not is_rate_limited(Exception("connection timeout"))
        assert not is_rate_limited(Exception("500 server error"))

    def test_endpoint_label(self):
        assert endpoint_label("https://api.deepseek.com/v1", "m") == "m@api.deepseek.com"
        assert endpoint_label("https://opencode.ai/inference/go/openai/v1", "m") \
            == "m@opencode.ai"

    @pytest.mark.asyncio
    async def test_primary_success_uses_primary(self):
        primary, backup = _FakeChat(outcome="a"), _FakeChat(outcome="b")
        llm = FallbackChatLLM(primary, backup, "go", "official")
        assert await llm.ainvoke([("human", "q")]) == "a"
        assert llm.last_endpoint == "go"
        assert backup.calls == 0

    @pytest.mark.asyncio
    async def test_rate_limit_falls_back(self):
        primary = _FakeChat(error=Exception("Error code: 429 - rate limit"))
        backup = _FakeChat(outcome="b")
        llm = FallbackChatLLM(primary, backup, "go", "official")
        assert await llm.ainvoke([("human", "q")]) == "b"
        assert llm.last_endpoint == "official"

    @pytest.mark.asyncio
    async def test_other_errors_propagate(self):
        primary = _FakeChat(error=ValueError("bad request"))
        llm = FallbackChatLLM(primary, _FakeChat(outcome="b"), "go", "official")
        with pytest.raises(ValueError, match="bad request"):
            await llm.ainvoke([("human", "q")])

    @pytest.mark.asyncio
    async def test_no_backup_reraises_rate_limit(self):
        primary = _FakeChat(error=Exception("429"))
        llm = FallbackChatLLM(primary, None, "go", None)
        with pytest.raises(Exception, match="429"):
            await llm.ainvoke([("human", "q")])


class TestIndexBuildConfig:
    def test_config_reflects_settings(self):
        settings = make_settings(chunk_chars=400, chunk_max_chars=2500)
        config = index_build_config(settings)
        assert config["chunker_version"] == "structural-v1"
        assert config["chunk_chars"] == 400
        assert config["chunk_max_chars"] == 2500
        # 改参数即改索引身份：软目标或硬上限任一不同，配置必须不同。
        assert config != index_build_config(make_settings(chunk_chars=500,
                                                          chunk_max_chars=2500))
        assert config != index_build_config(make_settings(chunk_chars=400,
                                                          chunk_max_chars=3000))


class TestRRFFuse:
    def test_agreement_ranks_first(self):
        # 两路都靠前的键融合分最高；只在一路出现的键排后。
        fused = rrf_fuse([[1, 2, 3], [2, 1, 4]], top_k=4)
        assert [key for key, _ in fused[:2]] == [1, 2]

    def test_rank_only_not_score(self):
        # RRF 只用排名：两路名次互换的键融合分并列。
        fused = dict(rrf_fuse([[1, 2], [2, 1]], top_k=2))
        assert fused[1] == fused[2]

    def test_top_k_truncates(self):
        assert len(rrf_fuse([[1], [2], [3]], top_k=2)) == 2

    def test_single_list_preserves_order(self):
        fused = rrf_fuse([[7, 8, 9]], top_k=3)
        assert [key for key, _ in fused] == [7, 8, 9]

    def test_weight_narrows_displacement_window(self):
        # BM25 第 20 名（1/80 > 榜尾 1/90）在对称融合下能注入；w=0.8 后
        # 0.8/80 < 1/90，进不来——权重收窄的是"可注入的 BM25 深度窗口"。
        dense = list(range(1, 31))
        bm25 = list(range(1, 20)) + [91]   # 前 19 名与 dense 一致，91 排第 20
        assert 91 in [key for key, _ in rrf_fuse([dense, bm25], top_k=30)]
        fused = rrf_fuse([dense, bm25], top_k=30, weights=[1.0, 0.8])
        assert 91 not in [key for key, _ in fused]

    def test_weight_still_injects_bm25_top(self):
        # w=0.8 下 BM25 第 1 名（0.8/61）仍高于榜尾（1/90），仍能注入——
        # 窗口收窄不是关闭；权重压到 ~0.68 以下 BM25 就完全进不来了。
        dense = list(range(1, 31))
        fused = rrf_fuse([dense, [77]], top_k=30, weights=[1.0, 0.8])
        keys = [key for key, _ in fused]
        assert 77 in keys and 30 not in keys

    def test_weight_still_rewards_agreement(self):
        # 两路一致的项即使降权仍获得加成，名次上升。
        dense, bm25 = [5, 1, 2, 3, 4], [1, 6, 7, 8, 9]
        fused = rrf_fuse([dense, bm25], top_k=9, weights=[1.0, 0.8])
        assert fused[0][0] == 1

    def test_weights_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="一一对应"):
            rrf_fuse([[1], [2]], top_k=2, weights=[1.0])


class TestRetrievalStrategySetting:
    def test_default_is_final_scheme(self):
        # 正常启动就应是保留方案（收尾计划第一部分）；dense 只作显式对照。
        assert make_settings().retrieval_strategy == "hybrid-rerank"

    def test_hybrid_allowed(self):
        settings = make_settings(retrieval_strategy="hybrid-rrf")
        assert settings.retrieval_strategy == "hybrid-rrf"
        assert settings.rrf_k == 60 and settings.bm25_top_n == 30

    def test_unknown_strategy_rejected(self):
        # 拼错必须在启动时失败，防止对照实验拿 dense 结果冒充 hybrid。
        with pytest.raises(Exception, match="hybrid-rrf"):
            make_settings(retrieval_strategy="hybrid_rrf")

    def test_hybrid_rerank_allowed_with_service(self):
        settings = make_settings(
            retrieval_strategy="hybrid-rerank",
            rerank_url="http://127.0.0.1:18080/rerank",
            rerank_model="Qwen/Qwen3-Reranker-0.6B")
        assert settings.rerank_pool_size == 60
        assert settings.evidence_unit_limit == 20
        assert settings.context_token_budget == 6000

    def test_hybrid_rerank_without_service_rejected(self):
        # 没配重排服务时启动即失败：否则会拿无重排的结果冒充 R2。
        # 显式传空覆盖 .env（init 参数优先于环境文件，测试不能依赖本机 .env）。
        with pytest.raises(Exception, match="rerank_url"):
            make_settings(retrieval_strategy="hybrid-rerank",
                          rerank_url="", rerank_model="")

    def test_default_evidence_window_config(self):
        settings = make_settings()
        assert settings.evidence_context_mode == "window"
        assert settings.evidence_unit_limit == 20
        assert settings.evidence_document_limit == 3

    def test_neighbor_rerank_control_mode_allowed(self):
        settings = make_settings(evidence_context_mode="neighbor-rerank")
        assert settings.evidence_context_mode == "neighbor-rerank"

    def test_unknown_context_mode_rejected(self):
        with pytest.raises(Exception, match="evidence_context_mode"):
            make_settings(evidence_context_mode="windows")
