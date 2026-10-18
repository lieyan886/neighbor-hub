# -*- coding: utf-8 -*-
"""v1.8.0：按真实应用流程审计后的回归。

本版核心：手动「+报名」与粘贴接龙必须同一套归并口径 ——
以前 add() 直接 INSERT，同一个人点两次「+报名」就出现两条记录，
提货清单多印一行、结算多算一个人、催款多发一条。
"""
from __future__ import annotations

from core.db import get_database
from core.models import Signup
from core.repository import items as item_repo
from core.repository import signups as signup_repo


def _mk(title: str, **kw) -> int:
    from core.models import STATUS_ACTIVE, Item

    kw.setdefault("status", STATUS_ACTIVE)
    it = Item(title=title, kind="groupbuy", unit="份", source_hash=f"v18-{title}", **kw)
    return item_repo.create(it)


def test_add_merges_same_person(solo_data):
    """同名再报名：份数累加，不多出一条记录。"""
    item_id = _mk("手动报名归并")
    first = signup_repo.add(Signup(item_id=item_id, name="3栋王姐", qty=2))
    second = signup_repo.add(Signup(item_id=item_id, name="王姐", qty=1))

    rows = signup_repo.list_for(item_id)
    assert len(rows) == 1, f"同一个人被记成 {len(rows)} 条"
    assert rows[0].qty == 3.0
    # 归并时把新写法记进备注，方便事后核对
    assert "王姐" in (rows[0].note or "")
    assert first == second, "归并应返回同一条记录的 id"


def test_add_keeps_distinct_people(solo_data):
    """不同的人还是各报各的，不能误并。"""
    item_id = _mk("不同人报名")
    signup_repo.add(Signup(item_id=item_id, name="王姐", qty=1))
    signup_repo.add(Signup(item_id=item_id, name="王哥", qty=1))
    signup_repo.add(Signup(item_id=item_id, name="李阿姨", qty=1))

    assert len(signup_repo.list_for(item_id)) == 3


def test_add_and_batch_add_same_outcome(solo_data):
    """两条路径（手动 + / 粘贴接龙）对同一个人必须给出相同结果。"""
    item_a = _mk("路径一致性A")
    signup_repo.add(Signup(item_id=item_a, name="8栋张哥", qty=2))
    signup_repo.add(Signup(item_id=item_a, name="8栋张哥", qty=1))

    item_b = _mk("路径一致性B")
    signup_repo.batch_add(item_b, [("8栋张哥", 2.0), ("8栋张哥", 1.0)])

    rows_a = signup_repo.list_for(item_a)
    rows_b = signup_repo.list_for(item_b)
    assert len(rows_a) == len(rows_b) == 1
    assert rows_a[0].qty == rows_b[0].qty == 3.0


def test_duplicate_merge_still_works_on_legacy_rows(solo_data):
    """查重归并功能照旧：处理历史遗留的重复记录（不同写法）。"""
    item_id = _mk("历史重复记录")
    db = get_database()
    cur_a = db.execute(
        "INSERT INTO signups (item_id, name, qty, settled, created_at) "
        "VALUES (?, '张三', 2, 1, '2026-10-01 10:00:00')", (item_id,))
    a = int(cur_a.lastrowid)
    cur_b = db.execute(
        "INSERT INTO signups (item_id, name, qty, settled, created_at) "
        "VALUES (?, '3栋张三', 3, 0, '2026-10-02 10:00:00')", (item_id,))
    b = int(cur_b.lastrowid)

    groups = signup_repo.duplicate_groups(item_id)
    assert len(groups) == 1 and len(groups[0]) == 2

    assert signup_repo.merge_signups(a, [b]) == 5.0
    rows = signup_repo.list_for(item_id)
    assert len(rows) == 1 and rows[0].qty == 5.0
    assert not rows[0].settled, "有一条没结清就不算结清"


def test_stats_not_inflated_by_repeat_signup(solo_data):
    """看板口径：同一个人重复报名不该让份数/人数虚高。"""
    from core.repository import stats as stats_repo

    item_id = _mk("看板不虚高")
    signup_repo.add(Signup(item_id=item_id, name="甲", qty=2))
    signup_repo.add(Signup(item_id=item_id, name="甲", qty=1))
    signup_repo.add(Signup(item_id=item_id, name="乙", qty=3))

    ov = stats_repo.overview()
    assert ov["units"] == 6.0, f"份数虚高：{ov['units']}"
    assert ov["people"] == 2, f"人数虚高：{ov['people']}"
