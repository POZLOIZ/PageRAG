"""状态接口纯函数测试：百分位、时间格式与摘要清理。"""

from datetime import UTC, datetime
from types import SimpleNamespace

from pagerag.status_api import (
    _elapsed_seconds,
    _iso,
    _percentile,
    _plain_excerpt,
    _query_item,
    _seconds_to_ms,
    total_duration_ms,
)


def test_percentile_handles_empty_and_edges():
    assert _percentile([], 50) is None
    assert _percentile([1, 2, 3, 4], 50) == 2.0
    assert _percentile([1, 2, 3, 4], 95) == 4.0
    assert _percentile([5], 100) == 5.0


def test_seconds_to_ms():
    assert _seconds_to_ms(None) is None
    assert _seconds_to_ms("1.0") is None
    assert _seconds_to_ms(0.5) == 500.0
    assert _seconds_to_ms(1) == 1000.0


def test_iso_handles_none_and_datetime():
    assert _iso(None) is None
    value = datetime(2026, 11, 12, 10, 30, tzinfo=UTC)
    assert _iso(value) == "2026-11-12T10:30:00+00:00"


def test_elapsed_seconds_accepts_naive_mysql_datetime():
    start = datetime.now(UTC).replace(tzinfo=None)
    elapsed = _elapsed_seconds(start)
    assert elapsed is not None and 0 <= elapsed < 5
    assert _elapsed_seconds(None) is None


def test_total_duration_does_not_double_count_verification():
    # verify_total 已经包含 verify_verify；两项都加会把核验算两遍。
    timings = {"generate": 12.6, "verify_total": 6.371,
               "verify_verify": 6.371, "llm_attempts": 1,
               "llm_endpoint": "provider/model"}
    assert total_duration_ms(timings) == 18971.0


def test_total_duration_falls_back_when_verify_total_is_missing():
    timings = {"generate": 2.0, "verify_verify": 0.5, "verify_repair": 0.25}
    assert total_duration_ms(timings) == 2750.0
    assert total_duration_ms({}) is None


def test_query_item_reports_snapshot_counts_and_duration():
    run = SimpleNamespace(
        public_id="q1", question="问题", status="succeeded", insufficient=False,
        evidence_snapshot={"items": [{"rank": 1}, {"rank": 2}]},
        answer="回答正文", failure_reason=None,
        timings={"generate": 2.0, "verify_total": 1.0, "verify_verify": 1.0},
        usage={"total_tokens": 10}, created_at=None,
    )
    check = SimpleNamespace(status="partial", issues={"partial": 2})
    item = _query_item(run, check, "c1", "知识库", 3, "conv1", 4)
    assert item["evidence_count"] == 2
    assert item["citation_count"] == 4
    assert item["duration_ms"] == 3000.0
    assert item["verification_issue_count"] == 2
    assert item["index_version"] == 3


def test_plain_excerpt_strips_markdown_and_clips():
    text = ("## 结论\n\n**依赖注入**就是函数参数，见 `Depends`[1]。\n\n"
            "- 第一项\n- 第二项\n\n```python\nprint('x')\n```")
    assert _plain_excerpt(text) == "结论 依赖注入就是函数参数，见 Depends。 第一项 第二项"
    assert _plain_excerpt("见 `Path`[1] 。") == "见 Path。"
    assert _plain_excerpt("文档中的*路径操作函数*与 _依赖_。") == "文档中的路径操作函数与 依赖。"
    assert _plain_excerpt("字" * 300).endswith("…")
    assert len(_plain_excerpt("字" * 300)) == 201
    assert _plain_excerpt("") == ""
