"""词面一路的融合工具：RRF 融合与候选池构建。

词面检索本身由 Milvus 全文检索承担，带中文 analyzer 和 BM25 稀疏字段，与稠密
检索同集合同版本，见 `milvus_store.py`。本模块保留两路融合共用的工具。RRF 只
按排名融合，不需要两路分数可比；候选池只合并不选择，供重排挑选。`rrf_k` 使用
文献常规值，不是调优结果。
"""


def rrf_fuse(ranked_lists: list[list[int]], top_k: int, k: int = 60,
             weights: list[float] | None = None) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：score(key) = Σ w_i/(k + rank)，多路排名融合。

    每路是一个按相关性排好的 key 列表，排名从 1 起；并列时先到者稳定。只用排名
    不用原始分数，稠密分与 BM25 分无需可比。`weights` 与各路一一对应，缺省全为 1
    表示对称融合。对称 RRF 下单路中后段项可能被另一路的前段项挤出，不对称权重
    可以调整两路的相对影响。
    """
    if weights is not None and len(weights) != len(ranked_lists):
        raise ValueError("weights 必须与 ranked_lists 一一对应")
    scores: dict[int, float] = {}
    for list_index, ranked in enumerate(ranked_lists):
        weight = weights[list_index] if weights else 1.0
        for rank, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return fused[:top_k]


def build_candidate_pool(dense_ranked: list[int], bm25_ranked: list[int],
                         pool_size: int) -> list[dict]:
    """两路候选的去重并集（R2 重排前的候选池）：只合并，不选择。

    与 rrf_fuse 的分工：RRF 融合即截断（合并与选择一体，单路中后段项会被
    挤出）；这里保留两路全部命中项，选择交给看过内容的重排。返回按"两路
    最好名次"升序的池，每项记录各路名次（未入该路为 None），供快照与诊断。
    """
    pool: dict[int, dict] = {}
    for rank, key in enumerate(dense_ranked, start=1):
        pool[key] = {"chunk_id": key, "dense_rank": rank, "bm25_rank": None,
                     "subquery": 0}
    for rank, key in enumerate(bm25_ranked, start=1):
        entry = pool.get(key)
        if entry is None:
            pool[key] = {"chunk_id": key, "dense_rank": None, "bm25_rank": rank,
                         "subquery": 0}
        else:
            entry["bm25_rank"] = rank

    def _best(entry: dict) -> tuple:
        ranks = [r for r in (entry["dense_rank"], entry["bm25_rank"]) if r is not None]
        return (min(ranks), entry["chunk_id"])

    return sorted(pool.values(), key=_best)[:pool_size]


def merge_candidate_pools(pools: list[list[dict]], pool_size: int) -> list[dict]:
    """多查询候选池合并（查询分解用）：池 0（原问题）全保留，其余池补位。

    每个池来自一个查询（下标 0＝原问题）的两路去重并集，条目格式与
    build_candidate_pool 相同。各池名次不可互相比较，合并只做去重与封顶，
    选择仍交给按原问题重排。两条不变式：
    - 池 0 的条目一条不丢（碰撞以池 0 为准）：分解只能往候选里加块，
      不能把原问题本可召回的块挤出上限——否则分解可能降低召回；
    - 其余池按"任一池中最好名次"排序补到 pool_size，来源记入 subquery
      （0＝原问题），供快照回答"这块是哪条查询召回的"。
    """
    if not pools:
        return []

    def _best(entry: dict) -> tuple:
        ranks = [r for r in (entry["dense_rank"], entry["bm25_rank"]) if r is not None]
        return (min(ranks), entry["chunk_id"])

    primary = {entry["chunk_id"]: {**entry, "subquery": 0} for entry in pools[0]}
    extra: dict[int, dict] = {}
    for subquery_index, pool in enumerate(pools[1:], start=1):
        for entry in pool:
            key = entry["chunk_id"]
            if key in primary:
                continue
            current = extra.get(key)
            if current is None or _best(entry) < _best(current):
                extra[key] = {**entry, "subquery": subquery_index}
    budget = max(0, pool_size - len(primary))
    ranked = sorted(extra.values(), key=_best)[:budget]
    return sorted([*primary.values(), *ranked], key=_best)
