"""主窗口：左侧导航 + 四个功能模块 + 底部状态栏。"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import config
from core.models import Item
from core.repository import stats as stats_repo
from core.scheduler import ReminderService
from ui import theme
from ui.collect_panel import CollectPanel
from ui.manage_panel import ManagePanel
from ui.render_panel import RenderPanel
from ui.settings_dialog import SettingsDialog
from ui.stats_panel import StatsPanel

_NAV = (
    ("collect", "信息采集台"),
    ("manage", "内容管理台"),
    ("render", "分发生成器"),
    ("stats", "数据看板"),
)


class Bridge(QObject):
    """把后台线程的回调转成 Qt 信号，安全地回到主线程。"""

    due_soon = Signal(object)
    status_changed = Signal(int)


class MainWindow(QMainWindow):
    """应用外壳。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("邻里圈 · 社群运营工作台")
        cfg = config.load_settings()
        self.resize(int(cfg.get("window_width", 1280)), int(cfg.get("window_height", 820)))

        self.bridge = Bridge()
        self.bridge.due_soon.connect(self._on_due_soon)
        self.bridge.status_changed.connect(self._on_status_changed)

        self.reminder = ReminderService(
            on_due_soon=lambda items_: self.bridge.due_soon.emit(items_),
            on_status_changed=lambda n: self.bridge.status_changed.emit(n),
        )
        self._build_panels()
        self._build_ui()
        self._start_timer()
        self._refresh_summary()

    # ============================ 组装 ============================

    def _build_panels(self) -> None:
        self.collect = CollectPanel()
        self.manage = ManagePanel()
        self.render = RenderPanel()
        self.stats = StatsPanel()

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
        root.addWidget(side)

        # —— 内容区 ——
        self.stack = QStackedWidget()
        for panel in (self.collect, self.manage, self.render, self.stats):
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
        elif isinstance(panel, StatsPanel):
            panel.refresh()

    def _goto(self, key: str) -> None:
        for i, (k, _label) in enumerate(_NAV):
            if k == key:
                self.nav.setCurrentRow(i)
                break

    def _start_timer(self) -> None:
        started = self.reminder.start()
        self._timer_state = started

    def _restart_timer(self) -> None:
        self.reminder.stop()
        self._timer_state = self.reminder.start()

    def _scan_now(self) -> None:
        due = self.reminder.scan_now()
        self.manage.refresh()
        self._refresh_summary()
        if not due:
            self._set_status("检查完成：没有临近截止的内容")
            return
        self._on_due_soon(due)

    @Slot(object)
    def _on_due_soon(self, items_: list[Item]) -> None:
        lines = [f"· {it.title} —— 截止 {it.deadline}" for it in items_[:8]]
        more = f"\n…还有 {len(items_) - 8} 条" if len(items_) > 8 else ""
        self._set_status(f"{len(items_)} 条内容即将截止")
        box = QMessageBox(self)
        box.setWindowTitle("临时提醒")
        box.setText(f"{len(items_)} 条内容快到截止了：\n\n" + "\n".join(lines) + more)
        box.setIcon(QMessageBox.Information)
        box.setStyleSheet(theme.QSS)
        go = box.addButton("去处理", QMessageBox.AcceptRole)
        box.addButton("知道了", QMessageBox.RejectRole)
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is go:
            self._goto("manage")

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
        if cfg.get("window_width") != self.width() or cfg.get("window_height") != self.height():
            config.save_settings({"window_width": self.width(),
                                  "window_height": self.height()})
        self.reminder.stop()
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
