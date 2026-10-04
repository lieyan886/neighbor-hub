"""v1.5.0：看板深化 / 提醒扩维 / 报名同名归并。

其中几条是**防回归**用例：v1.4 的看板有三个「看着对、实际错」的 bug，
这里逐条钉死，避免以后又改回去。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import utils
from core.db import get_database
from core.models import KIND_GROUPBUY, Item, Signup, STATUS_ACTIVE, STATUS_EXPIRED
from core.repository import _month_keys, items as item_repo
from core.repository import notified, signups as signup_repo, stats


def _new_item(title: str, **kw) -> Item:
    """建一条拼单。默认 status=active —— draft 不会被提醒扫描命中。"""
    kw.setdefault("status", STATUS_ACTIVE)
    it = Item(title=title, kind=KIND_GROUPBUY, unit="份",
              source_hash=f"h-{title}", **kw)
    it.id = item_repo.create(it)
    return it


def _shift_created(item_id: int, when: datetime) -> None:
    get_database().execute(
        "UPDATE items SET created_at = ? WHERE id = ?",
        (when.strftime("%Y-%m-%d %H:%M:%S"), item_id),
    )


# ===========================================================================
# 看板 bug 防回归
# ===========================================================================

def test_units_is_sum_of_qty_not_rowcount(isolated_data):
    """bug #1：看板「累计份数」曾经显示的是报名记录条数。

    一个人报 5 份只算 1，跟卡片标题完全不符。
    """
    it = _new_item("份数统计拼单", quota=100)
    # 用增量断言：这是 session 级共享库，别的用例也在往里写数据
    before = stats.overview()
    signup_repo.add(Signup(item_id=it.id, name="张三", qty=5))
    signup_repo.add(Signup(item_id=it.id, name="李四", qty=3))

    after = stats.overview()
    assert after["signups"] == before["signups"] + 2, "多出 2 条报名记录"
    assert after["units"] == before["units"] + 8.0, (
        "累计份数应该多 5+3=8 份；若这里等于 +2，说明又把记录条数当成了份数")


def test_month_keys_no_duplicate_no_gap(monkeypatch):
    """bug #2：月份序列曾经用 30 天步进反推，跨小月会重复并漏月。

    模拟 3 月 31 日：老算法会算出 3/31→03月、3/1→又是03月，2 月直接消失。
    """
    import core.repository as R

    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 31, 10, 0, 0)

    monkeypatch.setattr(R, "datetime", FakeNow)
    keys = _month_keys(6)
    assert len(set(keys)) == 6, f"月份不能重复：{keys}"
    assert keys == ["2025-10", "2025-11", "2025-12",
                    "2026-01", "2026-02", "2026-03"]


def test_kind_progress_returns_target(isolated_data):
    """bug #3：目标值算出来了却没人用，卡片标题却写着「完成进度」。"""
    _new_item("目标进度拼单", quota=50)
    prog = {k: (d, t) for k, d, t in stats.kind_progress()}
    assert KIND_GROUPBUY in prog
    _, target = prog[KIND_GROUPBUY]
    assert target >= 50, "目标份数必须参与统计，不能是 0"


# ===========================================================================
# 看板新增能力
# ===========================================================================

def test_fulfillment_rate(isolated_data):
    """成团率：凑够目标份数的才算成团。"""
    done = _new_item("已成团", quota=10)
    signup_repo.add(Signup(item_id=done.id, name="甲", qty=12))
    half = _new_item("没成团", quota=10)
    signup_repo.add(Signup(item_id=half.id, name="乙", qty=4))

    fu = stats.fulfillment()
    assert fu["total"] >= 2
    assert fu["formed"] >= 1
    assert 0 < fu["rate"] <= 100


def test_compare_has_now_prev_delta(isolated_data):
    """环比要给出本期 / 上期 / 变化率三件套。"""
    cmp_ = stats.compare(30)
    for key in ("items", "people", "units"):
        assert "now" in cmp_[key] and "prev" in cmp_[key] and "delta" in cmp_[key]
    assert cmp_["items"]["prev"] >= 0


def test_overview_respects_days(isolated_data):
    """时间范围筛选必须真的收窄结果。"""
    old = _new_item("很早以前的内容", quota=1)
    _shift_created(old.id, datetime.now() - timedelta(days=400))

    assert int(stats.overview(30)["items"]) < int(stats.overview(None)["items"])


# ===========================================================================
# 提醒扩维
# ===========================================================================

def test_formation_alerts_almost_there(isolated_data):
    """差一点点就成团时，应该提醒「还差 N 份」。"""
    it = _new_item("快成团了", quota=10, deadline=(
        datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M"))
    signup_repo.add(Signup(item_id=it.id, name="甲", qty=9))

    alerts = {a.id: msg for a, msg in item_repo.formation_alerts()}
    assert it.id in alerts
    assert "还差 1 份" in alerts[it.id], f"实际提示：{alerts[it.id]}"


def test_formation_alerts_slow(isolated_data):
    """时间过半还不到一半，应该提醒该催了。"""
    it = _new_item("迟迟不成团", quota=20, deadline=(
        datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M"))
    _shift_created(it.id, datetime.now() - timedelta(days=8))
    signup_repo.add(Signup(item_id=it.id, name="甲", qty=2))

    alerts = {a.id: msg for a, msg in item_repo.formation_alerts()}
    assert it.id in alerts
    assert "%" in alerts[it.id], f"应给出百分比：{alerts[it.id]}"


def test_settlement_overdue(isolated_data):
    """团结束了还有人没结清 —— v1.3 的结算数据终于有人提醒了。"""
    it = _new_item("结束但没结清", quota=5, status=STATUS_EXPIRED)
    signup_repo.add(Signup(item_id=it.id, name="甲", qty=2, settled=True))
    signup_repo.add(Signup(item_id=it.id, name="乙", qty=1, settled=False))

    overdue = {a.id: n for a, n in item_repo.settlement_overdue()}
    assert it.id in overdue
    assert overdue[it.id] == 1, "只有乙没结清"


def test_notified_dedupe_persists(isolated_data):
    """去重落库后跨查询都能查到（以前是内存 set，重启就忘）。"""
    it = _new_item("去重测试", quota=1)
    notified.forget(it.id)
    assert not notified.has(it.id, "due")
    notified.mark(it.id, "due")
    assert notified.has(it.id, "due")
    notified.forget(it.id, "due")
    assert not notified.has(it.id, "due")


def test_reminder_scan_does_not_repeat(isolated_data):
    """扫过一次的提醒，第二次扫描不该再报一遍。"""
    from core.scheduler import ReminderService

    seen: list = []
    svc = ReminderService(on_settlement=lambda pairs: seen.extend(pairs))
    svc.scan()
    first = len(seen)
    svc.scan()
    assert len(seen) == first, "第二次扫描重复提醒了同一批"


# ===========================================================================
# 报名同名归并
# ===========================================================================

@pytest.mark.parametrize("raw,expect", [
    ("张三", "张三"),
    ("3栋张三", "张三"),
    ("12号楼张三", "张三"),
    ("张三妈妈", "张三"),
    ("李雷老婆", "李雷"),
    ("张三 ", "张三"),
    ("王姐", "王姐"),          # 称呼不是亲属后缀，不能被吃掉
])
def test_normalize_name(raw, expect):
    assert utils.normalize_name(raw) == expect


def test_same_person():
    assert utils.same_person("张三", "3栋张三")
    assert utils.same_person("张三", "张三妈妈")
    assert not utils.same_person("张三", "李四")
    assert not utils.same_person("王姐", "李姐")


def test_batch_add_merges_by_normalized_name(isolated_data):
    """接龙里「张三」和「3栋张三」是同一户，份数要累加而不是记成两个人。"""
    it = _new_item("归并接龙", quota=100)
    signup_repo.batch_add(it.id, [("张三", 2.0), ("3栋张三", 3.0), ("李四", 1.0)])
    rows = signup_repo.list_for(it.id)
    assert len(rows) == 2, f"应该合并成 2 个人，实际 {len(rows)}"
    zs = [r for r in rows if utils.normalize_name(r.name) == "张三"][0]
    assert zs.qty == 5.0
    assert "3栋张三" in (zs.note or ""), "别名要记进备注方便核对"


def test_duplicate_groups_and_merge(isolated_data):
    """查重分组 + 合并：份数相加、结清取「都结清才算」。"""
    it = _new_item("查重测试", quota=100)
    a = signup_repo.add(Signup(item_id=it.id, name="张三", qty=2, settled=True))
    b = signup_repo.add(Signup(item_id=it.id, name="3栋张三", qty=3, settled=False))

    groups = signup_repo.duplicate_groups(it.id)
    assert len(groups) == 1 and len(groups[0]) == 2

    assert signup_repo.merge_signups(a, [b]) == 5.0
    rows = signup_repo.list_for(it.id)
    assert len(rows) == 1
    assert rows[0].qty == 5.0
    assert not rows[0].settled, "有一条没结清就不算结清"
