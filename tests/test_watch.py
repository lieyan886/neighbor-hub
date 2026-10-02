"""v1.2.0 新增能力的测试：桌面通知、监控源盯梢、一键推群的数据副作用。

设计原则跟 selftest 一致：不联网。抓取器用函数替换（monkeypatch 模块属性），
只验证「变化判定」和「快照回写」这些自己的逻辑。
"""
from __future__ import annotations

from core.models import Item, WatchSource, stamp
from core.repository import watch_sources as repo
from core import watcher


class _FakeResult:
    """模仿 link_parser.ScrapeResult 的最小集合。"""

    def __init__(self, title="团购页面", summary="", price=168.0,
                 deadline="2026-10-05 18:00", url="https://example.com/p/1",
                 error=""):
        self.url = url
        self.title = title
        self.summary = summary
        self.source = "example"
        self.local_cover = ""
        self.price = price
        self.deadline = deadline
        self.tags = []
        self.error = error


def _install_fake_parser(monkeypatch, title="团购页面", price=168.0,
                         deadline="2026-10-05 18:00"):
    """把 link_parser 的两个函数换成假实现，返回用来改返回值的 setter。"""
    from collectors import link_parser

    box = {"title": title, "price": price, "deadline": deadline}

    def fake_scrape(url, download_cover=True, allow_browser=None, progress=None):
        return _FakeResult(title=box["title"], price=box["price"],
                           deadline=box["deadline"])

    def fake_convert(res, kind=None):
        return Item(title=res.title, summary=res.summary, url=res.url,
                    price=res.price, deadline=res.deadline, kind="deal")

    monkeypatch.setattr(link_parser, "scrape_url", fake_scrape)
    monkeypatch.setattr(link_parser, "convert_to_item", fake_convert)
    return box


# ===========================================================================
# 桌面通知
# ===========================================================================


def test_notify_never_raises(qapp):
    """无论环境支不支持，notify 都不能抛异常 —— UI 层要靠它做静默降级。"""
    from core import notifier

    result = notifier.notify("标题", "内容")
    assert isinstance(result, bool)
    assert notifier.available() in (True, False)


def test_icon_and_tray_do_not_crash(qapp):
    """托盘图标可以重复取，重复 hide 也不应抛异常。"""
    from core import notifier

    notifier.icon()
    notifier.icon()
    notifier.hide_tray()
    notifier.hide_tray()


# ===========================================================================
# 监控源
# ===========================================================================


def test_watch_source_crud(isolated_data):
    src = WatchSource(url="https://example.com/deal/9", note="测试源",
                      enabled=True, created_at=stamp())
    sid = repo.create(src)
    assert repo.get(sid) is not None
    assert repo.get(sid).note == "测试源"
    assert repo.get_by_url("https://example.com/deal/9") is not None

    repo.set_enabled(sid, False)
    assert repo.get(sid).enabled is False
    # 关掉之后就不该出现在「只扫启用」的结果里
    assert all(s.id != sid for s in repo.all(only_enabled=True))

    repo.set_enabled(sid, True)
    assert any(s.id == sid for s in repo.all(only_enabled=True))

    repo.delete(sid)
    assert repo.get(sid) is None


def test_first_seen_then_price_change(monkeypatch, isolated_data):
    box = _install_fake_parser(monkeypatch, price=168.0)
    src = WatchSource(url="https://example.com/deal/77", created_at=stamp())
    sid = repo.create(src)

    first = watcher.check_source(repo.get(sid))
    assert first.first_seen, "第一次抓要算首次发现"
    assert first.error == ""

    # 落一次快照，让后续能比出差异
    repo.save_snapshot(sid, title=first.title, price=first.price,
                       deadline=first.deadline,
                       content_hash=watcher.fingerprint_of(
                           first.title, "", first.price, first.deadline))

    box["price"] = 149.0
    second = watcher.check_source(repo.get(sid))
    assert not second.first_seen
    assert "price" in second.change_fields, "降价必须被识别"

    # 真实流程里每轮扫完都会写快照，写完再抓同一份内容就不该再提醒
    repo.save_snapshot(sid, title=second.title, price=second.price,
                       deadline=second.deadline,
                       content_hash=watcher.fingerprint_of(
                           second.title, "", second.price, second.deadline))
    third = watcher.check_source(repo.get(sid))
    assert third.change_fields == [], "没变化却不该重复提醒"

    repo.delete(sid)


def test_price_change_ignores_missing_price(monkeypatch, isolated_data):
    """有价变无价（下架占位）不该报「降价到 0 元」，那是误报。"""
    assert watcher._price_changed(168.0, None) is False
    assert watcher._price_changed(None, 168.0) is False
    assert watcher._price_changed(168.0, 168.0) is False
    assert watcher._price_changed(168.0, 149.0) is True


def test_check_source_reports_error(monkeypatch, isolated_data):
    """站点抓不动时要返回带 error 的 change，而不是抛异常。"""
    from collectors import link_parser

    monkeypatch.setattr(
        link_parser, "scrape_url",
        lambda url, download_cover=True, allow_browser=None, progress=None:
            _FakeResult(error="请求失败：超时"))
    src = WatchSource(url="https://example.com/bad", created_at=stamp())
    sid = repo.create(src)
    try:
        change = watcher.check_source(repo.get(sid))
        assert change.error
        assert not change.has_change
    finally:
        repo.delete(sid)


def test_service_apply_writes_snapshot(monkeypatch, isolated_data):
    """跑完一轮扫描，last_price / last_hash 必须真的更新，否则会天天重复提醒。"""
    _install_fake_parser(monkeypatch, price=199.0)
    src = WatchSource(url="https://example.com/deal/88", created_at=stamp())
    sid = repo.create(src)

    svc = watcher.WatchService()
    changes = svc.scan()
    hit = [c for c in changes if c.source.id == sid]
    assert hit, "新加的源应该被识别为首次发现"

    after = repo.get(sid)
    assert after is not None
    assert after.last_price == 199.0
    assert after.last_checked, "扫完要写 last_checked"
    repo.delete(sid)


def test_watch_change_summary():
    src = WatchSource(url="https://example.com/x", note="牛肉卷")
    change = watcher.WatchChange(source=src, price=158.0, change_fields=["price"])
    assert "牛肉卷" in change.summary()
    err = watcher.WatchChange(source=src, error="超时")
    assert "抓取失败" in err.summary()
