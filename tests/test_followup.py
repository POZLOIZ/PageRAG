"""连续追问的离线测试：有界历史、指代改写、失败退回原句。

不连数据库与模型：用假会话返回索引三列，假检索记录真正收到的问题，
假模型按顺序返回改写与回答。重点守两件事——检索用的是改写后的独立问题，
改写不可用时行为退回单轮（原问题直接检索），不阻断回答。
"""

from types import SimpleNamespace

import pytest

from pagerag.answering import (
    HISTORY_MAX_TURNS,
    PROMPT_HISTORY_TURNS,
    AnsweringService,
    PreparedAnswer,
)
from pagerag.retrieval import SearchResult


class FakeLLM:
    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.calls: list[list] = []
        self.last_endpoint = "fake"

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply, usage_metadata={})


class FakeRetrieval:
    def __init__(self) -> None:
        self.questions: list[str] = []

    async def search(self, session, index, question, subqueries=None):
        self.questions.append(question)
        # 空结果走拒答分支：不依赖检索证据构造，也能验证检索收到的文本。
        return SearchResult("empty", [], index.version, {"embed_query": 0.01}, [])


class FakeSession:
    async def execute(self, statement):
        return SimpleNamespace(
            one=lambda: SimpleNamespace(id=1, version=1, milvus_collection="c"))


def make_service(replies: list) -> AnsweringService:
    # 绕开构造函数里的真实 LLM 客户端（本测试不需要 API 配置）。
    service = object.__new__(AnsweringService)
    service._settings = SimpleNamespace(
        retrieval_strategy="hybrid-rerank",
        query_decomposition_enabled=False,
        query_decomposition_max_subqueries=3)
    service._retrieval = FakeRetrieval()
    service._llm = FakeLLM(replies)
    return service


def make_run(question: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=1, index_version_id=1, question=question,
        resolved_question="__unset__", history="__unset__",
        status="running", answer=None, insufficient=False,
        failure_reason=None, timings={}, usage={}, evidence_snapshot=None)


@pytest.mark.asyncio
async def test_followup_retrieves_resolved_question_and_keeps_original():
    service = make_service(["贵州茅台 2023 年毛利率是多少？", "资料不足，无法回答。"])
    run = make_run("那它的毛利率呢？")
    history = [{"question": "贵州茅台 2023 年营收是多少？",
                "answer": "营收为 1476 亿元。"}]

    await service.answer(FakeSession(), run, run.question, history)

    assert service._retrieval.questions == ["贵州茅台 2023 年毛利率是多少？"]
    assert run.question == "那它的毛利率呢？"  # 原问题原样保留
    assert run.resolved_question == "贵州茅台 2023 年毛利率是多少？"
    assert run.history == history
    assert "resolve" in run.timings


@pytest.mark.asyncio
async def test_no_history_skips_rewrite():
    service = make_service(["资料不足。"])
    run = make_run("北京有哪些区？")

    await service.answer(FakeSession(), run, run.question, [])

    assert service._retrieval.questions == ["北京有哪些区？"]
    assert len(service._llm.calls) == 1  # 只有回答调用，没有改写调用
    assert run.resolved_question is None
    assert run.history is None
    assert "resolve" not in run.timings


@pytest.mark.asyncio
async def test_blank_rewrite_falls_back_to_original():
    service = make_service(["   ", "资料不足。"])
    run = make_run("那它的毛利率呢？")

    await service.answer(FakeSession(), run, run.question,
                         [{"question": "茅台营收多少？", "answer": "1476 亿元。"}])

    assert service._retrieval.questions == ["那它的毛利率呢？"]
    assert run.resolved_question is None
    assert len(service._llm.calls) == 2


@pytest.mark.asyncio
async def test_rewrite_error_falls_back_to_original():
    service = make_service([RuntimeError("boom"), "资料不足。"])
    run = make_run("它去年呢？")

    await service.answer(FakeSession(), run, run.question,
                         [{"question": "今年的营收？", "answer": "增长 12%。"}])

    assert service._retrieval.questions == ["它去年呢？"]
    assert run.resolved_question is None


def test_history_digest_keeps_recent_turns_and_clips_answers():
    service = make_service([])
    history = [{"question": f"问题{i}", "answer": "答" * 800} for i in range(10)]

    digest = service._history_digest(history)

    assert len(digest) == HISTORY_MAX_TURNS
    assert digest[0]["question"] == "问题4"  # 只保留最近 6 轮
    assert len(digest[0]["answer"]) == 500


def make_prepared(question: str, resolved: str, digest: list[dict]) -> PreparedAnswer:
    return PreparedAnswer(
        question=question, resolved=resolved, digest=digest, timings={},
        result=SearchResult("ok", [], 1, {}, []), subqueries=[],
        context="[1] 依赖项\n你的代码声明它需要的东西，FastAPI 负责提供。")


def test_prompt_answers_original_question_and_keeps_prior_answer():
    prepared = make_prepared(
        "啥意思，解释过于复杂",
        "依赖注入 解释过于复杂 什么意思",
        [{"question": "什么是依赖注入？",
          "answer": "依赖注入是声明依赖并由框架注入的一种机制[7]。"}])

    prompt = prepared.prompt()

    assert "Current question: 啥意思，解释过于复杂" in prompt
    assert "User: 什么是依赖注入？" in prompt
    assert "Assistant: 依赖注入是声明依赖并由框架注入的一种机制。" in prompt
    # 上一轮引用编号会被误当成当前证据编号，生成提示里去掉。
    assert "[7]" not in prompt
    # 检索改写只用于召回，不能作为用户问题再出现在生成提示里。
    assert "依赖注入 解释过于复杂 什么意思" not in prompt


def test_prompt_limits_conversation_to_recent_turns():
    prepared = make_prepared(
        "继续", "继续", [{"question": f"问题{i}", "answer": f"回答{i}"}
                        for i in range(PROMPT_HISTORY_TURNS + 2)])

    prompt = prepared.prompt()

    assert "User: 问题2" in prompt
    assert "User: 问题3" in prompt
    assert "User: 问题1" not in prompt


def test_first_turn_prompt_has_no_conversation_block():
    prepared = make_prepared("北京有哪些区？", "北京有哪些区？", [])

    prompt = prepared.prompt()

    assert "Conversation so far:" not in prompt
    assert "Current question: 北京有哪些区？" in prompt
