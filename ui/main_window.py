"""主窗口：左侧导航 + 四个功能模块 + 底部状态栏。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import config
from core import notifier
from core.models import Item
from core.repository import stats as stats_repo
from core.scheduler import ReminderService
from core.watcher import WatchService
from ui import theme
from ui.collect_panel import CollectPanel
from ui.manage_panel import ManagePanel
from ui.render_panel import RenderPanel
from ui.settings_dialog import SettingsDialog
from ui.stats_panel import StatsPanel
from ui.watch_panel import WatchPanel

_NAV = (
    ("collect", "信息采集台"),
    ("manage", "内容管理台"),
    ("render", "分发生成器"),
    ("stats", "数据看板"),
    ("watch", "自动盯梢"),
)


class Bridge(QObject):
    """把后台线程的回调转成 Qt 信号，安全地回到主线程。"""

    due_soon = Signal(object)
    status_changed = Signal(int)
    # v1.5.0：除「临近截止」外，成团预警与结算逾期也要提醒
    formation = Signal(object)     # [(Item, 提示语), ...]
    settlement = Signal(object)    # [(Item, 未结清人数), ...]
    watch_found = Signal(object)


class MainWindow(QMainWindow):
    """应用外壳。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            f"{config.APP_NAME} · {config.APP_TAGLINE}  v{config.APP_VERSION}"
        )
        cfg = config.load_settings()
        self.resize(int(cfg.get("window_width", 1280)), int(cfg.get("window_height", 820)))

        self.bridge = Bridge()
        self.bridge.due_soon.connect(self._on_due_soon)
        self.bridge.status_changed.connect(self._on_status_changed)
        self.bridge.watch_found.connect(self._on_watch_found)
        self.bridge.formation.connect(self._on_formation)
        self.bridge.settlement.connect(self._on_settlement)

        self.reminder = ReminderService(
            on_due_soon=lambda items_: self.bridge.due_soon.emit(items_),
            on_status_changed=lambda n: self.bridge.status_changed.emit(n),
            on_formation=lambda pairs: self.bridge.formation.emit(pairs),
            on_settlement=lambda pairs: self.bridge.settlement.emit(pairs),
        )
        self.watcher = WatchService(
            on_updates=lambda changes: self.bridge.watch_found.emit(changes)
        )
        self._force_quit = False          # 托盘「退出」置 True，绕开最小化逻辑
        self._build_panels()
        self._build_ui()
        self._build_tray()
        self._start_timer()
        self._refresh_summary()

    # ============================ 组装 ============================

    def _build_panels(self) -> None:
        self.collect = CollectPanel()
        self.manage = ManagePanel()
        self.render = RenderPanel()
        self.stats = StatsPanel()
        self.watch = WatchPanel()

        self.collect.items_imported.connect(lambda n: self._goto("manage"))
        self.collect.items_imported.connect(lambda _n: self.manage.refresh())
        self.manage.data_changed.connect(self._refresh_summary)
        self.render.published.connect(self._refresh_summary)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # —— 侧边导航 ——
        side = QFrame()
        side.setObjectName("surface")
        side.setFixedWidth(178)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(12, 18, 12, 14)
        side_layout.setSpacing(6)

        brand = QLabel("邻里圈")
        brand.setProperty("role", "title")
        side_layout.addWidget(brand)
        sub = QLabel("社群运营工作台")
        sub.setProperty("role", "hint")
        side_layout.addWidget(sub)
        side_layout.addSpacing(14)

        self.nav = QListWidget()
        for key, label in _NAV:
            item = QListWidgetItemShim(label)
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._switch_page)
        side_layout.addWidget(self.nav, 1)

        self.check_btn = QPushButton("立即检查")
        self.check_btn.setProperty("variant", "ghost")
        self.check_btn.clicked.connect(self._scan_now)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.setProperty("variant", "ghost")
        self.settings_btn.clicked.connect(self._open_settings)
        side_layout.addWidget(self.check_btn)
        side_layout.addWidget(self.settings_btn)

        ver = QLabel(f"v{config.APP_VERSION}")
        ver.setProperty("role", "muted")
        side_layout.addWidget(ver)
        root.addWidget(side)

        # —— 内容区 ——
        self.stack = QStackedWidget()
        for panel in (self.collect, self.manage, self.render, self.stats, self.watch):
            self.stack.addWidget(panel)
        root.addWidget(self.stack, 1)

        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.status.addPermanentWidget(self.status_label)

    # ============================ 行为 ============================

    def _switch_page(self, row: int) -> None:
        if row < 0 or row >= self.stack.count():
            return
        self.stack.setCurrentIndex(row)
        panel = self.stack.widget(row)
        if isinstance(panel, ManagePanel):
            panel.refresh()
        elif isinstance(panel, RenderPanel):
            panel.refresh_all()
        elif isinstance(panel, (StatsPanel, WatchPanel)):
            panel.refresh()

    def _goto(self, key: str) -> None:
        for i, (k, _label) in enumerate(_NAV):
            if k == key:
                self.nav.setCurrentRow(i)
                break

    def _build_tray(self) -> None:
        """托盘图标 + 右键菜单。右键菜单用 self 当 owner，保证 quitting 信号可用。"""
        cfg = config.load_settings()
        if not cfg.get("tray_enabled", True) or not notifier.available():
            self.tray = None
            return
        menu = QMenu(self)
        act_show = QAction("打开主界面", self)
        act_show.triggered.connect(self._restore_from_tray)
        act_scan = QAction("立即检查", self)
        act_scan.triggered.connect(self._scan_now)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit_from_tray)
        menu.addAction(act_show)
        menu.addAction(act_scan)
        menu.addSeparator()
        menu.addAction(act_quit)

        ok = notifier.show_tray(self, menu)
        self.tray = notifier.icon(self)
        if ok and self.tray is not None:
            self.tray.messageClicked.connect(self._on_tray_clicked)
            self.tray.activated.connect(self._on_tray_activated)

    def _restore_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._force_quit = True
        self.close()

    @Slot()
    def _on_tray_clicked(self) -> None:
        """点气泡通知 → 打开主界面并跳到内容管理台。"""
        self._goto("manage")
        self._restore_from_tray()

    @Slot(object)
    def _on_tray_activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._restore_from_tray()

    def _start_timer(self) -> None:
        started = self.reminder.start()
        self._timer_state = started
        self.watcher.start()

    def _restart_timer(self) -> None:
        self.reminder.stop()
        self._timer_state = self.reminder.start()
        self.watcher.stop()
        self.watcher.start()

    def _scan_now(self) -> None:
        res = self.reminder.scan_now()
        due = res["due"]            # type: ignore[index]
        formation = res["formation"]  # type: ignore[index]
        settle = res["settle"]      # type: ignore[index]
        self.manage.refresh()
        self._refresh_summary()

        total = len(due) + len(formation) + len(settle)
        if not total:
            self._set_status("检查完成：没有需要处理的提醒")
            self._maybe_notify("检查完成", "没有需要处理的提醒")
            return
        # 手动点「立即检查」时给一条汇总通知，三类一起说清楚
        parts = []
        if due:
            parts.append(f"{len(due)} 条即将截止")
        if formation:
            parts.append(f"{len(formation)} 条成团预警")
        if settle:
            parts.append(f"{len(settle)} 条结算待清")
        summary = "、".join(parts)
        names = [it.title for it in due] + [it.title for it, _ in formation]
        detail = summary
        if names:
            detail += "：" + "、".join(names[:5]) + ("…" if len(names) > 5 else "")
        self._maybe_notify(f"发现 {total} 处需要处理", detail)
        self._set_status(f"{summary}，已在通知栏提醒")

    def _maybe_notify(self, title: str, message: str) -> bool:
        """按设置决定是否弹桌面通知（无 Qt/无托盘时静默失败）。"""
        cfg = config.load_settings()
        if not cfg.get("notify_enabled", True) or not notifier.available():
            return False
        return notifier.notify(title, message)

    @Slot(object)
    def _on_due_soon(self, items_: list[Item]) -> None:
        """后台定时扫到即将截止：发桌面气泡 + 更新状态栏，不打断当前操作。"""
        self._set_status(f"{len(items_)} 条内容即将截止")
        self._maybe_notify(
            f"{len(items_)} 条内容快到截止了",
            "、".join(it.title for it in items_[:5]) + ("…" if len(items_) > 5 else ""),
        )
        self._refresh_summary()

    @Slot(object)
    def _on_formation(self, pairs) -> None:
        """成团预警：差几份就成团 / 时间过半还没动静。"""
        if not pairs:
            return
        head = f"{len(pairs)} 条拼单需要关注成团进度"
        body = "\n".join(f"· {it.title} —— {msg}" for it, msg in pairs[:5])
        if len(pairs) > 5:
            body += f"\n…还有 {len(pairs) - 5} 条"
        self._maybe_notify(head, body)
        self._set_status(head)
        self._refresh_summary()

    @Slot(object)
    def _on_settlement(self, pairs) -> None:
        """结算逾期：团结束了还有人没结清。数据来自 v1.3 的结算闭环。"""
        if not pairs:
            return
        unpaid = sum(n for _, n in pairs)
        head = f"{len(pairs)} 条内容还有 {unpaid} 人没结清"
        body = "\n".join(f"· {it.title} —— {n} 人待结清" for it, n in pairs[:5])
        if len(pairs) > 5:
            body += f"\n…还有 {len(pairs) - 5} 条"
        self._maybe_notify(head, body)
        self._set_status(head)
        self._refresh_summary()

    @Slot(object)
    def _on_watch_found(self, changes) -> None:
        """监控源有更新：通知 + 让盯梢面板刷新。"""
        lines = [c.summary() for c in changes[:5]]
        self.watch.refresh()
        self._maybe_notify(
            f"{len(changes)} 个监控内容有更新",
            "\n".join(lines) + ("\n…" if len(changes) > 5 else ""),
        )
        self._set_status(f"{len(changes)} 个监控内容有更新")

    @Slot(int)
    def _on_status_changed(self, count: int) -> None:
        self._refresh_summary()
        if count:
            self._set_status(f"有 {count} 条内容状态自动更新")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self)
        if dlg.exec():
            self._restart_timer()
            self.render.refresh_all()
            self._refresh_summary()
            self._set_status("设置已保存")

    def _refresh_summary(self) -> None:
        try:
            ov = stats_repo.overview()
        except Exception:
            return
        self._ov = ov
        timer_text = "定时提醒：开" if getattr(self, "_timer_state", False) else "定时提醒：关"
        self.status_label.setText(
            f"内容 {ov['items']} · 进行中 {ov['active'] + ov['ending']} · "
            f"即将截止 {ov['ending']} · 参与 {ov['people']} 人　|　{timer_text}"
        )

    def _set_status(self, text: str) -> None:
        self.status.showMessage(text, 6000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        cfg = config.load_settings()
        # 开了「缩到托盘」且用户是点关闭按钮（不是托盘菜单退出）→ 只隐藏，程序继续跑
        if cfg.get("minimize_to_tray", True) and not self._force_quit:
            event.ignore()
            self.hide()
            self._maybe_notify("邻里圈在后台运行", "已缩到系统托盘，点图标可随时唤出")
            return
        if cfg.get("window_width") != self.width() or cfg.get("window_height") != self.height():
            config.save_settings({"window_width": self.width(),
                                  "window_height": self.height()})
        self.reminder.stop()
        self.watcher.stop()
        notifier.hide_tray()
        super().closeEvent(event)


# 让 QListWidgetItem 的构造集中在一处，方便统一调整行高
from PySide6.QtWidgets import QListWidgetItem as _QListWidgetItem  # noqa: E402


class QListWidgetItemShim(_QListWidgetItem):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setSizeHint(type(self)._hint())

    @staticmethod
    def _hint():
        from PySide6.QtCore import QSize

        return QSize(0, 42)
