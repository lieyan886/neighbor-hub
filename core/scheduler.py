"""后台定时器：状态自动流转 + 多维度提醒。

v1.5.0 之前只按「截止时间」一个维度扫描，三件团长真正会漏的事没人管：
  1. 快成团了差几份没人知道（错过再推一把的窗口）
  2. 迟迟不成团没人预警（等到截止才发现白忙）
  3. 团结束了还有人没结清 —— v1.3 的结算数据躺在库里没人提醒

现在三类一起扫。去重也不再是内存 set（重启就忘），改用 notified 表落库。

刻意不依赖 Qt —— 只暴露回调，由 UI 层把 Qt 信号接上来，
这样命令行脚本和单元测试也能直接跑。
"""
from __future__ import annotations

from typing import Callable

from . import config
from .models import Item
from .repository import items, notified

# 提醒类型
KIND_DUE = "due"              # 临近截止
KIND_FORMATION = "formation"  # 成团预警
KIND_SETTLE = "settle"        # 结算逾期


class ReminderService:
    """周期性扫描条目：推进生命周期状态，并回调三类提醒。"""

    def __init__(
        self,
        on_due_soon: Callable[[list[Item]], None] | None = None,
        on_status_changed: Callable[[int], None] | None = None,
        on_formation: Callable[[list[tuple[Item, str]]], None] | None = None,
        on_settlement: Callable[[list[tuple[Item, int]]], None] | None = None,
    ) -> None:
        self.on_due_soon = on_due_soon
        self.on_status_changed = on_status_changed
        self.on_formation = on_formation
        self.on_settlement = on_settlement
        self._scheduler = None
        # 仍保留一份内存集合做本次会话内的快速去重，真正的跨会话去重靠 notified 表
        self._notified: set[tuple[int, str]] = set()

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

    # —— 去重 ——

    def _fresh(self, item_id: int | None, kind: str, force: bool = False) -> bool:
        """这条提醒要不要发：内存 + 数据库两层去重。

        force=True 用于「立即检查」—— 手动点的时候用户要看的是**当下**的情况，
        不能因为半小时前已经弹过一次就永远显示「没有需要处理的提醒」。

        结算逾期是个例外：它跟「临近截止」不同，事情不处理就一直成立。
        只提醒一次等于放弃催收，所以隔 settle_remind_hours 小时会再提一次。
        """
        if item_id is None:
            return False
        if force:
            return True
        key = (item_id, kind)
        if key in self._notified:
            return False
        try:
            if notified.has(item_id, kind):
                if kind == KIND_SETTLE:
                    again = int(config.load_settings().get("settle_remind_hours", 24))
                    # 0 = 用户关掉了重复催收，那就老实只提醒一次
                    if again <= 0:
                        self._notified.add(key)
                        return False
                    age = notified.age_hours(item_id, kind)
                    if age is not None and age >= again:
                        return True     # 还没结清，该再催一次
                self._notified.add(key)
                return False
        except Exception:
            return False          # 表出问题时宁可不提醒，也不要刷屏
        return True

    def _mark(self, item_id: int | None, kind: str) -> None:
        if item_id is None:
            return
        self._notified.add((item_id, kind))
        try:
            notified.mark(item_id, kind)
        except Exception:
            pass

    def forget(self, item_id: int, kind: str | None = None) -> None:
        """允许对同一条目再次提醒（处理完后调用）。"""
        if kind:
            self._notified.discard((item_id, kind))
        else:
            self._notified = {k for k in self._notified if k[0] != item_id}
        try:
            notified.forget(item_id, kind)
        except Exception:
            pass

    def clear_notified(self) -> None:
        self._notified.clear()
        try:
            notified.clear()
        except Exception:
            pass

    # —— 业务逻辑 ——

    def scan(self) -> None:
        """一次扫描：推状态 -> 临近截止 -> 成团预警 -> 结算逾期。"""
        settings = config.load_settings()
        remind_hours = int(settings.get("remind_hours", 24))

        try:
            changed = items.refresh_statuses(remind_hours)
            if changed and self.on_status_changed:
                self.on_status_changed(changed)
        except Exception:
            return

        # ① 临近截止
        if self.on_due_soon:
            try:
                due = [it for it in items.due_soon(remind_hours)
                       if self._fresh(it.id, KIND_DUE)]
                if due:
                    for it in due:
                        self._mark(it.id, KIND_DUE)
                    self.on_due_soon(due)
            except Exception:
                pass

        # ② 成团预警
        if self.on_formation:
            try:
                alerts = [(it, msg) for it, msg in items.formation_alerts()
                          if self._fresh(it.id, KIND_FORMATION)]
                if alerts:
                    for it, _ in alerts:
                        self._mark(it.id, KIND_FORMATION)
                    self.on_formation(alerts)
            except Exception:
                pass

        # ③ 结算逾期
        if self.on_settlement:
            try:
                overdue = [(it, n) for it, n in items.settlement_overdue()
                           if self._fresh(it.id, KIND_SETTLE)]
                if overdue:
                    for it, _ in overdue:
                        self._mark(it.id, KIND_SETTLE)
                    self.on_settlement(overdue)
            except Exception:
                pass

        # 顺手清掉两个月前的提醒记录
        try:
            notified.prune(60)
        except Exception:
            pass

    def scan_now(self, force: bool = True) -> dict[str, object]:
        """手动触发一次扫描（UI 上的「立即检查」按钮）。

        默认 force=True：手动点要看的是当下还有多少事没处理，
        不能因为半小时前弹过一次就一直回「没有需要处理的提醒」。
        返回三类提醒的结果，方便 UI 一次性展示。
        """
        settings = config.load_settings()
        remind_hours = int(settings.get("remind_hours", 24))
        try:
            changed = items.refresh_statuses(remind_hours)
            if changed and self.on_status_changed:
                self.on_status_changed(changed)
        except Exception:
            changed = 0

        due = [it for it in items.due_soon(remind_hours)
               if self._fresh(it.id, KIND_DUE, force)]
        for it in due:
            self._mark(it.id, KIND_DUE)

        formation = [(it, m) for it, m in items.formation_alerts()
                     if self._fresh(it.id, KIND_FORMATION, force)]
        for it, _ in formation:
            self._mark(it.id, KIND_FORMATION)

        settle = [(it, n) for it, n in items.settlement_overdue()
                  if self._fresh(it.id, KIND_SETTLE, force)]
        for it, _ in settle:
            self._mark(it.id, KIND_SETTLE)

        if self.on_due_soon and due:
            self.on_due_soon(due)
        if self.on_formation and formation:
            self.on_formation(formation)
        if self.on_settlement and settle:
            self.on_settlement(settle)

        return {"due": due, "formation": formation, "settle": settle,
                "status_changed": changed}
