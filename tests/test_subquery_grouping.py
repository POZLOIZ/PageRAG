"""子问题证据分组测试：多组时限额、单组不限额、回填不丢证据。"""

from pagerag.evidence import UnitChunk, select_units


def _hit(chunk_id: int, group: int) -> UnitChunk:
    return UnitChunk(
        chunk_id=chunk_id, chunk_index=chunk_id, text=f"正文{chunk_id}",
        char_start=chunk_id * 10, char_end=chunk_id * 10 + 4,
        line_start=chunk_id, line_end=chunk_id, is_hit=True,
        revision_row_id=1, start_cut=False, end_cut=False,
        source_subquery=group,
    )


def _header(number, hit, members):
    return f"[{number}]"


def _select(hits, unit_limit, group_limit):
    return select_units(
        hits, {}, unit_limit, 10000, _header,
        token_counter=len, group_limit=group_limit)


def test_multi_group_limit_applies_in_first_round():
    hits = [_hit(index, group) for group in (0, 1) for index in
            (group * 10 + 1, group * 10 + 2, group * 10 + 3)]
    units = _select(hits, unit_limit=4, group_limit=2)
    groups = [unit.chunks[0].source_subquery for unit in units]
    assert groups.count(0) == 2
    assert groups.count(1) == 2


def test_single_group_is_not_capped_by_group_limit():
    hits = [_hit(index, 0) for index in range(1, 6)]
    units = _select(hits, unit_limit=5, group_limit=2)
    assert len(units) == 5


def test_deferred_hits_backfill_when_capacity_remains():
    hits = [_hit(index, group) for group in (0, 1) for index in
            (group * 10 + 1, group * 10 + 2, group * 10 + 3)]
    units = _select(hits, unit_limit=6, group_limit=1)
    # 第一轮每组 1 个，第二轮按原名次补满 6 个，证据没有被配额丢掉。
    assert len(units) == 6
    groups = [unit.chunks[0].source_subquery for unit in units]
    assert groups.count(0) == 3
    assert groups.count(1) == 3


def test_group_limit_zero_disables_grouping():
    hits = [_hit(index, index % 2) for index in range(1, 7)]
    units = _select(hits, unit_limit=6, group_limit=0)
    assert len(units) == 6
