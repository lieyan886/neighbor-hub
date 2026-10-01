"""后台定时器：状态自动流转 + 临近截止提醒。

刻意不依赖 Qt —— 只暴露回调，由 UI 层把 Qt 信号接上来，
这样命令行脚本和单元测试也能直接跑。
"""
from __future__ import annotations

from typing import Callable

from . import config
from .models import Item
from .repository import items


class ReminderService:
    """周期性扫描条目：推进生命周期状态，并回调提醒即将到期的条目。"""

    def __init__(
        self,
        on_due_soon: Callable[[list[Item]], None] | None = None,
        on_status_changed: Callable[[int], None] | None = None,
    ) -> None:
        self.on_due_soon = on_due_soon
        self.on_status_changed = on_status_changed
        self._scheduler = None
        self._notified: set[int] = set()

    # —— 生命周期 ——

    @property
    def running(self) -> bool:
        return self._scheduler is not None

    def start(self, interval_minutes: int | None = None) -> bool:
        """启动定时器，返回是否真的启动了。"""
        settings = config.load_settings()
        if not settings.get("scheduler_enabled", True):
            return False
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError:
            return False

        interval = interval_minutes or int(settings.get("scan_interval_minutes", 30))
        interval = max(1, interval)
        self.stop()
        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self.scan, "interval", minutes=interval, id="scan", replace_existing=True
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

    # —— 业务逻辑 ——

    def scan(self) -> None:
        """一次扫描：先推状态，再收集即将到期的条目做提醒。"""
        settings = config.load_settings()
        remind_hours = int(settings.get("remind_hours", 24))

        try:
            changed = items.refresh_statuses(remind_hours)
            if changed and self.on_status_changed:
                self.on_status_changed(changed)
        except Exception:
            return

        if not self.on_due_soon:
            return
        try:
            due = items.due_soon(remind_hours)
        except Exception:
            return
        fresh = [it for it in due if it.id not in self._notified]
        if fresh:
            for it in fresh:
                if it.id is not None:
                    self._notified.add(it.id)
            self.on_due_soon(fresh)

    def scan_now(self) -> list[Item]:
        """手动触发一次扫描（UI 上的「立即检查」按钮）。"""
        settings = config.load_settings()
        try:
            changed = items.refresh_statuses(int(settings.get("remind_hours", 24)))
            if changed and self.on_status_changed:
                self.on_status_changed(changed)
        except Exception:
            pass
        due = items.due_soon(int(settings.get("remind_hours", 24)))
        return due

    def forget(self, item_id: int) -> None:
        """允许对同一条目再次提醒。"""
        self._notified.discard(item_id)

    def clear_notified(self) -> None:
        self._notified.clear()
