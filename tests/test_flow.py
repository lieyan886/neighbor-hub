"""主链路：入库 → 状态流转 → 接龙登记与累加 → 统计。"""
from __future__ import annotations

from core.repository import items as item_repo
from core.repository import signups as signup_repo
from core.repository import stats as stats_repo
from render import copywriter


def test_seed_inserted(seed):
    assert len(seed) >= 3


def test_status_refresh(seed):
    changed = item_repo.refresh_statuses(24)
    gb = [i for i in item_repo.list_items(include_archived=True)
          if i.source_hash == "test-groupbuy"]
    assert gb, "没找到拼单条目"
    assert gb[0].derive_status()
    assert isinstance(changed, int)


def test_solitaire_parse_and_accumulate(seed):
    gb = [i for i in item_repo.list_items(include_archived=True)
          if i.source_hash == "test-groupbuy"][0]
    rows = copywriter.dedupe_solitaire(
        copywriter.parse_solitaire("1. 张三 2份\n2、3栋王姐 3\n3)李雷 1份", "份")
    )
    assert len(rows) == 3, f"接龙解析条数不对：{len(rows)}"
    signup_repo.batch_add(gb.id, rows)
    signup_repo.batch_add(gb.id, [("张三", 1.0)])   # 同名应累加
    assert abs(signup_repo.total_qty(gb.id) - 7.0) < 1e-6


def test_stats_overview(seed):
    ov = stats_repo.overview()
    assert ov["items"] >= 3
    assert stats_repo.count_by_kind()
    assert isinstance(stats_repo.monthly_trend(), list)
    assert isinstance(stats_repo.top_items(), list)
