"""v1.2.0：监控源定时盯梢。

做什么：把常用的团购/拼单链接存起来，每隔几小时自动重抓一次，
跟上次抓到的快照对比——标题、价格、截止时间任一变了就产生一条 WatchChange，
由 UI 层决定是弹通知还是自动入库。

为什么自己扫而不依赖外部服务：这类页面大多需要 JS 渲染或微信 UA，
交给平台 SaaS 反而不如本地脚本灵活。也刻意不依赖 Qt，
命令行和单元测试能直接调用 check_source()。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import config, utils
from .models import Item, WatchSource
from .repository import watch_sources as watch_repo


@dataclass
class WatchChange:
    """一次抓到的变化。

    change_fields 里记录到底哪项变了，方便文案写成「价格从 168 变成 158」。
    """

    source: WatchSource
    item: Item | None = None
    title: str = ""
    price: float | None = None
    deadline: str = ""
    change_fields: list[str] = field(default_factory=list)
    first_seen: bool = False       # 首次抓取也算「有情况」，好让人知道盯梢是不是生效了
    error: str = ""

    @property
    def has_change(self) -> bool:
        return bool(self.change_fields) or self.first_seen

    def summary(self) -> str:
        label = self.title or self.source.note or self.source.url
        if self.error:
            return f"{label}：抓取失败（{self.error}）"
        if self.first_seen:
            price = utils.format_price(self.price) if self.price else ""
            return f"{label}：首次抓到{('，' + price) if price else ''}"
        if "price" in self.change_fields:
            return f"{label}：价格变了 → {utils.format_price(self.price)}"
        if "deadline" in self.change_fields:
            return f"{label}：截止时间变了 → {self.deadline or '（空）'}"
        if "title" in self.change_fields:
            return f"{label}：文案/标题更新"
        return f"{label}：有新动静"


def fingerprint_of(title: str, summary: str = "", price: float | None = None,
                   deadline: str = "") -> str:
    """用「判断要不要提醒」的关键字段做指纹，避免页面里无关元素（如广告位）
    每次重排都触发误报。"""
    price_text = f"{price:.2f}" if price is not None else ""
    return utils.fingerprint(title, summary[:120], price_text, deadline)


def check_source(src: WatchSource) -> WatchChange:
    """抓单个监控源并返回变化（不落库，落库交给 WatchService.apply）。"""
    from collectors import link_parser

    # 盯梢追求快：不下载头图，也不用浏览器兜底（后台跑，慢了会被用户察觉）
    res = link_parser.scrape_url(src.url, download_cover=False, allow_browser=False)
    if res is None or res.error:
        why = getattr(res, "error", "") or "没返回数据"
        return WatchChange(source=src, error=why)
    if not res.title and not res.summary:
        return WatchChange(source=src, error="页面没读到内容")

    item = link_parser.convert_to_item(res)
    price = getattr(item, "price", None)
    deadline = getattr(item, "deadline", "") or ""
    title = (getattr(item, "title", "") or "").strip()
    summary = (getattr(item, "summary", "") or "").strip()
    new_hash = fingerprint_of(title, summary, price, deadline)

    change = WatchChange(source=src, item=item, title=title, price=price,
                         deadline=deadline)

    if not src.last_hash:
        change.first_seen = True
        change.change_fields = ["first"]
    else:
        if title and src.title and title != src.title:
            change.change_fields.append("title")
        if _price_changed(src.last_price, price):
            change.change_fields.append("price")
        if deadline and src.last_deadline and deadline != src.last_deadline:
            change.change_fields.append("deadline")
        # 标题/价格/截止都没变但页面整体内容指纹变了 → 也算有动静
        if not change.change_fields and new_hash != src.last_hash:
            change.change_fields.append("content")
    return change


def _price_changed(old: float | None, new: float | None) -> bool:
    """价格比较：0 和 None 都视为「没价格」，避免库存占位值被当成变价。"""
    old_v = old if old else None
    new_v = new if new else None
    if old_v is None or new_v is None:
        return False
    return abs(old_v - new_v) > 0.009


class WatchService:
    """定时扫监控源。刻意不依赖 Qt，回调由 UI 层接。"""

    def __init__(self, on_updates: Callable[[list[WatchChange]], None] | None = None) -> None:
        self.on_updates = on_updates
        self._scheduler = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None

    def start(self, interval_hours: int | None = None) -> bool:
        settings = config.load_settings()
        if not settings.get("watch_enabled", True):
            return False
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            return False
        hours = interval_hours or int(settings.get("watch_interval_hours", 6))
        hours = max(1, hours)
        self.stop()
        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self.scan, "interval", hours=hours, id="watch", replace_existing=True
        )
        self._scheduler.start()
        return True

    def stop(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None

    def scan(self) -> list[WatchChange]:
        """扫一轮所有启用源，把变化落盘后回调给 UI。"""
        changes: list[WatchChange] = []
        for src in watch_repo.all(only_enabled=True):
            change = check_source(src)
            if change.has_change and not change.error:
                self.apply(change)
                changes.append(change)
        if changes and self.on_updates:
            self.on_updates(changes)
        return changes

    def apply(self, change: WatchChange) -> None:
        """把本轮观察写回监控源；开了自动入库就把内容同步进 items。"""
        src = change.source
        if src.id is None:
            return
        item_id = src.item_id
        settings = config.load_settings()
        if settings.get("watch_auto_import", False) and change.item is not None:
            item_id = self._upsert_item(change, src)
        watch_repo.save_snapshot(
            src.id,
            title=change.title or src.title,
            price=change.price if change.price is not None else src.last_price,
            deadline=change.deadline or src.last_deadline,
            content_hash=fingerprint_of(
                change.title, "", change.price, change.deadline),
            item_id=item_id,
        )

    def _upsert_item(self, change: WatchChange, src: WatchSource) -> int | None:
        """首次出现就新建条目；已存在就同步价格/截止。"""
        from .repository import items as item_repo

        item = change.item
        if item is None:
            return src.item_id
        if src.item_id:
            exist = item_repo.get(src.item_id)
            if exist is not None:
                exist.price = item.price if item.price is not None else exist.price
                exist.deadline = item.deadline or exist.deadline
                exist.cover = item.cover or exist.cover
                item_repo.update(exist)
                return exist.id
        item.status = "draft"
        return item_repo.create(item)
