"""v1.4.0：全周期文案、接龙脏文本解析、周期性开团。"""
from __future__ import annotations

from datetime import datetime

from core.models import KIND_GROUPBUY, Item, Signup
from core.repository import items as item_repo
from core.repository import signups as signup_repo
from core.utils import parse_datetime
from render.copywriter import parse_solitaire_detail


def _item(title: str = "山姆牛肉卷拼单 第2期", deadline: str = "2026-10-08 18:00",
          event_at: str = "") -> Item:
    it = Item(title=title, kind=KIND_GROUPBUY, price=168.0, unit="份", quota=20,
              merchant="山姆", location="3栋1502自提", deadline=deadline,
              event_at=event_at, source_hash=f"hash-{title}-{deadline}")
    it.id = item_repo.create(it)
    return it


def _signup(item_id: int, name: str, qty: float = 1.0, settled: bool = False) -> Signup:
    row = Signup(item_id=item_id, name=name, qty=qty, contact="13800001111",
                 settled=settled)
    signup_repo.add(row)
    return row


# ===========================================================================
# 接龙解析：真实群聊里的脏文本
# ===========================================================================

def test_parse_dirty_text_forms():
    """行尾数量、句中数量、+1、x2 都要能认出来。"""
    res = parse_solitaire_detail(
        "1. 张三 2份\n"
        "2、李四 1\n"
        "王五要3份\n"
        "赵六+1\n"
        "孙七x2\n")
    names = dict(res.rows)
    assert names["张三"] == 2.0
    assert names["李四"] == 1.0
    assert names["王五"] == 3.0
    assert names["赵六"] == 1.0
    assert names["孙七"] == 2.0
    assert res.adjustments == []


def test_parse_keeps_digits_in_house_number():
    """「3栋王姐」不能被当成数量 3 —— 房间里带数字的名字太常见了。"""
    res = parse_solitaire_detail("2、3栋王姐 2份\n3栋1701 李姐 1份")
    assert ("3栋王姐", 2.0) in res.rows
    assert any(name.startswith("3栋1701") for name, _ in res.rows)


def test_parse_modify_and_cancel_are_separate():
    """改单和取消必须单独归类，不能当成新增一个人。"""
    res = parse_solitaire_detail("张三改成3份\n李四不要了\n王五 2份")
    assert dict(res.rows) == {"王五": 2.0}, res.rows
    assert ("张三", 3.0) in res.adjustments
    assert ("李四", None) in res.adjustments


def test_parse_filters_chat_and_stamps():
    """闲聊、表情、微信时间戳不能混进名单，但也不能静默丢掉（要能给用户看）。"""
    res = parse_solitaire_detail(
        "2026年10月3日 22:14 1. 张三 2份\n22:15 收到\n👌\n好的谢谢\n李四 1份")
    assert dict(res.rows) == {"张三": 2.0, "李四": 1.0}, res.rows
    assert len(res.skipped) >= 2, "闲聊行应该被单独列出而不是消失"


# ===========================================================================
# 改单落库
# ===========================================================================

def test_apply_adjustments_updates_and_removes():
    it = _item("调整测试团")
    _signup(it.id, "张三", 2.0)
    _signup(it.id, "李四", 1.0)

    updated, removed = signup_repo.apply_adjustments(
        it.id, [("张三", 5.0), ("李四", None), ("查无此人", 3.0)])
    assert (updated, removed) == (1, 1), "陌生人不能被凭空造出来"

    rows = {s.name: s.qty for s in signup_repo.list_for(it.id)}
    assert rows["张三"] == 5.0
    assert "李四" not in rows


# ===========================================================================
# 全周期文案
# ===========================================================================

def test_chase_only_names_unsettled():
    """催收文案只能点还没结清的人，已结清的不该出现在群里。"""
    from render import copywriter

    it = _item("催收文案团")
    _signup(it.id, "张三", 2.0, settled=True)
    _signup(it.id, "李雷", 1.0, settled=False)

    text = copywriter.build_settlement_chase(it, signup_repo.list_for(it.id))
    assert "李雷" in text and "张三" not in text


def test_arrival_notice_masks_pickup_address():
    """到货通知要贴到群里，自提点门牌必须遮掉。"""
    from render import copywriter

    it = _item("到货文案团")
    _signup(it.id, "张三", 2.0)

    text = copywriter.build_arrival_notice(it, signup_repo.list_for(it.id))
    assert "1502" not in text, "自提点房号泄露了"
    assert "3栋" in text and "张三" in text


def test_reminder_shows_gap_and_fail_notice_shows_target():
    from render import copywriter

    it = _item("催办文案团")
    remind = copywriter.build_reminder(it, signed_qty=3.0)
    assert "3/20" in remind and "还差" in remind

    fail = copywriter.build_fail_notice(it, signed_qty=3.0)
    assert "20" in fail and "3" in fail


# ===========================================================================
# 周期性开团
# ===========================================================================

def test_duplicate_bumps_round_and_shifts_deadline():
    it = _item("周团测试 第2期", deadline="2026-10-08 18:00")
    clone = item_repo.duplicate(it.id, days_shift=7)
    assert clone is not None
    assert clone.id is None, "新一期不能有旧 id"
    assert clone.status == "draft", "复制出来应该是草稿"
    assert clone.source_hash != it.source_hash, "指纹不同才会被去重放过"
    assert "第3期" in clone.title, f"期数没递增：{clone.title}"

    old = parse_datetime(it.deadline)
    new = parse_datetime(clone.deadline)
    assert old is not None and new is not None
    assert (new - old).days == 7, "截止日期没顺延 7 天"


def test_duplicate_does_not_carry_signups():
    it = _item("开团带人测试")
    _signup(it.id, "张三", 2.0)

    clone = item_repo.duplicate(it.id)
    assert clone is not None
    clone.id = item_repo.create(clone)
    assert signup_repo.list_for(clone.id) == [], "新一期不该带着上期的报名"


def test_duplicate_plain_title_unchanged():
    """标题里没有「第N期」的不要乱加后缀。"""
    it = _item("山姆牛肉卷团购")
    clone = item_repo.duplicate(it.id)
    assert clone is not None and clone.title == "山姆牛肉卷团购"
