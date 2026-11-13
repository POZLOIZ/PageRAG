"""跨集合候选合并：轮流取、去重、受总上限约束。"""

from pagerag.retrieval import _merge_pools_round_robin


def test_merge_round_robin_preserves_collection_order():
    pools = [
        [{"chunk_id": 1}, {"chunk_id": 2}, {"chunk_id": 3}],
        [{"chunk_id": 4}, {"chunk_id": 5}],
    ]
    merged = _merge_pools_round_robin(pools, 4)
    assert [item["chunk_id"] for item in merged] == [1, 4, 2, 5]


def test_merge_round_robin_deduplicates_and_stops_at_limit():
    pools = [
        [{"chunk_id": 1}, {"chunk_id": 1}, {"chunk_id": 2}],
        [{"chunk_id": 3}],
    ]
    merged = _merge_pools_round_robin(pools, 10)
    assert [item["chunk_id"] for item in merged] == [1, 3, 2]
    assert len(_merge_pools_round_robin(pools, 2)) == 2
