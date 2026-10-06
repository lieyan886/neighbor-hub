"""v1.6.0：全量审计修复的回归用例。

每一条都对应审计里**真实复现过**的问题，不是凭空造的断言：
盯梢误报、目标值被 JOIN 扇出、粘贴接龙不解析、旧快照催款、
「再来一团」复制出已过期团、确认框文案、去重落库、逾期重提醒等。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from core import utils
from core.models import (
    KIND_GROUPBUY,
    Item,
    Signup,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_DRAFT,
)
from core.repository import items as item_repo
from core.repository import signups as signup_repo
from core.repository import stats
from core.utils import humanize, normalize_name, unique_path
from render import copywriter


def _mk(title: str, **kw) -> Item:
    kw.setdefault("status", STATUS_ACTIVE)
    it = Item(title=title, kind=KIND_GROUPBUY, unit="份",
              source_hash=f"h-{title}-{time.time_ns()}", **kw)
    it.id = item_repo.create(it)
    return it


# ===========================================================================
# A 档：数据错 / 流程走不通
# ===========================================================================

def test_kind_progress_target_not_fanout(solo_data):
    """bug #2：目标份数被 JOIN 扇出放大。

    一条 quota=10 的团挂 3 条报名，目标曾经显示成 30。
    """
    it = _mk("拼单", quota=10)
    for n in ("甲", "乙", "丙"):
        signup_repo.add(Signup(item_id=it.id, name=n, qty=1))

    prog = {k: (d, t) for k, d, t in stats.kind_progress()}
    _, target = prog[KIND_GROUPBUY]
    assert target == 10, f"目标不该被报名条数放大，实际 {target}"


def test_kind_progress_done_is_sum_of_qty(solo_data):
    """已完成份数按 qty 求和，不是报名记录条数。"""
    it = _mk("拼单", quota=10)
    signup_repo.add(Signup(item_id=it.id, name="甲", qty=5))

    prog = {k: d for k, d, _ in stats.kind_progress()}
    assert prog[KIND_GROUPBUY] == 5


def test_duplicate_clone_from_today_not_old_deadline(solo_data):
    """bug #5：「再来一团」要按今天顺延，不能把旧截止再加 7 天。"""
    old = _mk("团购", deadline="2026-09-01 18:00", status=STATUS_EXPIRED)
    old.deadline = "2026-09-01 18:00"
    item_repo.update(old)

    new = item_repo.duplicate(old.id)
    assert new is not None
    assert new.deadline, "新一期必须有截止时间"
    d = utils.parse_datetime(new.deadline)
    assert d is not None
    # 9/1 起算会得到 9/8（早已过期），正确行为是从「今天」往后推
    assert d.date() > datetime.now().date()
    assert d.date() - datetime.now().date() <= timedelta(days=10)


def test_duplicate_signups_not_carried_over(solo_data):
    """复制团时报名不跟随。"""
    it = _mk("团购")
    signup_repo.add(Signup(item_id=it.id, name="甲", qty=2))
    new = item_repo.duplicate(it.id)
    assert new is not None
    rows = signup_repo.list_for(new.id)
    assert rows == [], "上一期的报名不该跟到新一期"


# ===========================================================================
# B 档：真实需求补齐
# ===========================================================================

def test_bulk_insert_dedupe_actually_skips(solo_data):
    """bug #7：采集台勾了「去重」却没传 dedupe，同一条链接每周重复入库。"""
    it = Item(title="同一条优惠", kind="deal", url="https://x.test/a",
              source_hash="same-hash")
    inserted, skipped = item_repo.bulk_insert([it, it], dedupe=True)
    assert inserted == 1
    assert skipped == 1
    assert len(item_repo.list_items()) == 1, "同一条链接只该留一条"


def test_settle_reminder_repeats_when_still_unpaid(solo_data):
    """bug #8：结算逾期不处理就一直成立，只提醒一次等于放弃催收。"""
    from core.db import get_database
    from core.scheduler import ReminderService

    it = _mk("已结束未结清", quota=5, status=STATUS_EXPIRED)
    signup_repo.add(Signup(item_id=it.id, name="甲", qty=2, settled=False))

    seen: list = []
    svc = ReminderService(on_settlement=lambda pairs: seen.extend(pairs))
    svc.scan()
    assert len(seen) == 1, "结算逾期没被提醒"
    svc.scan()
    assert len(seen) == 1, "刚提醒过不该立刻重复"

    # 把上次提醒时间往前挪，超过设定间隔 → 该再催一次
    get_database().execute(
        "UPDATE notified SET notified_at = '2020-01-01 00:00:00' WHERE item_id = ?",
        (it.id,))
    svc._notified.clear()
    svc.scan()
    assert len(seen) == 2, "过了一个间隔还没结清，应该再催一次"


def test_humanize_drops_midnight_for_date_only():
    """bug #14：只填了日期，就别显示「今天 00:00」。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    text = today.strftime("%Y-%m-%d %H:%M:%S")
    out = humanize(text)
    assert "00:00" not in out
    assert out.startswith("今天")


def test_humanize_keeps_real_time():
    """填了具体时刻，时间要照常显示。"""
    when = datetime.now().replace(hour=18, minute=30, second=0, microsecond=0)
    out = humanize(when.strftime("%Y-%m-%d %H:%M:%S"))
    assert out.endswith("18:30")


def test_normalize_name_merges_people():
    """看板人数要跟报名归并用同一套口径（bug #12）。"""
    assert normalize_name("3栋张三") == normalize_name("张三")
    assert normalize_name("张三妈妈") == normalize_name("张三")
    assert normalize_name("王姐") == "王姐"


# ===========================================================================
# C 档：口径与打磨
# ===========================================================================

def test_overview_people_uses_normalized_name(solo_data):
    """看板参与人数按核心名去重，「3栋张三」和「张三」是同一个人。"""
    it = _mk("团购")
    signup_repo.add(Signup(item_id=it.id, name="张三", qty=1))
    signup_repo.add(Signup(item_id=it.id, name="3栋张三", qty=1))

    ov = stats.overview()
    assert ov["people"] == 1
    assert ov["units"] == 2


def test_unique_path_never_overwrites(tmp_path):
    """封面同名不再互相顶掉。"""
    first = tmp_path / "IMG_001.jpg"
    first.write_bytes(b"a")
    second = unique_path(tmp_path / "IMG_001.jpg")
    assert second != first
    assert second.exists() is False
    second.write_bytes(b"b")
    assert first.read_bytes() == b"a"


def test_solitaire_ignores_self_pronoun():
    """「我要2份」不能记成一条叫「我」的报名。"""
    rows = copywriter.parse_solitaire("我要2份\n李四 3份")
    names = [n for n, _ in rows]
    assert "我" not in names
    assert "李四" in names


def test_solitaire_still_parses_normal_lines():
    rows = copywriter.parse_solitaire("1. 张三 2份\n王五 1")
    assert ("张三", 2.0) in rows
    assert ("王五", 1.0) in rows


# ===========================================================================
# 统计口径：成团率只看已收尾
# ===========================================================================

def test_fulfillment_excludes_ongoing(solo_data):
    """bug #10：进行中的团不该进分母，否则成团率被系统性低估。"""
    done = _mk("已成团", quota=10, status=STATUS_EXPIRED)
    signup_repo.add(Signup(item_id=done.id, name="甲", qty=12))
    half = _mk("没成团", quota=10, status=STATUS_EXPIRED)
    signup_repo.add(Signup(item_id=half.id, name="乙", qty=4))
    _mk("刚开的", quota=10)                       # 还在进行中

    fu = stats.fulfillment()
    assert fu["total"] == 2
    assert fu["formed"] == 1
    assert fu["ongoing"] == 1
    assert fu["rate"] == 50.0


def test_compare_prev_window_not_sum(solo_data):
    """bug #9：环比上期是独立窗口，不是「总累计 - 本期」。"""
    it = _mk("本期团购", status=STATUS_ACTIVE)
    item_repo.update(it)

    c = stats.compare(30)
    assert c["items"]["now"] == 1
    for key in ("items", "people", "units"):
        assert c[key]["prev"] >= 0
