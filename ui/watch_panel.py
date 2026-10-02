"""模块五（v1.2.0）：自动盯梢。

常看的团购/拼单链接存进来，工具定时重抓，价格、截止时间、标题一变就通知。
这一页负责「存哪些、多久抓一次、现在抓到什么」这三件事。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import config, utils
from core.models import WatchSource, stamp
from core.repository import watch_sources as repo
from ui import theme
from ui.widgets import confirm, hint_label, info, make_table, section_title, set_row
from ui.workers import WatchWorker


class WatchPanel(QWidget):
    """监控源列表 + 立即检查。"""

    data_changed = Signal()

    HEADERS = ("备注/标题", "链接", "上次价格", "上次截止", "最近检查", "启用")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sources: list[WatchSource] = []
        self._worker: WatchWorker | None = None
        self._build_ui()
        self.refresh()

    # ============================ 组装 ============================

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(section_title(
            "自动盯梢", "加了监控源之后，工具会在后台定时重抓，变了才通知你"))

        bar = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "粘贴要盯的链接，回车添加（常看的团购页、拼单页、活动页）")
        self.url_edit.returnPressed.connect(self._add)
        self.add_btn = QPushButton("添加监控源")
        self.add_btn.clicked.connect(self._add)
        self.check_btn = QPushButton("立即检查")
        self.check_btn.setProperty("variant", "ghost")
        self.check_btn.clicked.connect(self._check_selected)
        self.check_all_btn = QPushButton("全部检查")
        self.check_all_btn.setProperty("variant", "ghost")
        self.check_all_btn.clicked.connect(self._check_all)
        self.toggle_btn = QPushButton("启/停")
        self.toggle_btn.setProperty("variant", "ghost")
        self.toggle_btn.clicked.connect(self._toggle)
        self.del_btn = QPushButton("删除")
        self.del_btn.setProperty("variant", "danger")
        self.del_btn.clicked.connect(self._delete)
        for w in (self.add_btn, self.check_btn, self.check_all_btn,
                  self.toggle_btn, self.del_btn):
            bar.addWidget(w)
        layout.addWidget(self.url_edit)
        layout.addLayout(bar)

        self.table = make_table(self.HEADERS, stretch_col=1)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.table, 1)

        self.status_label = hint_label("")
        layout.addWidget(self.status_label)

        cfg = config.load_settings()
        hours = int(cfg.get("watch_interval_hours", 6))
        enabled = bool(cfg.get("watch_enabled", True))
        layout.addWidget(hint_label(
            f"后台盯梢：{'开启' if enabled else '关闭'} · 每 {hours} 小时扫一轮"
            "（设置里可改）。软件缩到托盘后照样在跑，抓不到内容的站点会被跳过。"
        ))
        self.setStyleSheet(theme.QSS)
        self._sync_buttons()

    # ============================ 行为 ============================

    def refresh(self) -> None:
        try:
            self.sources = repo.all()
        except Exception as e:                      # 库还没建好时不炸界面
            self.status_label.setText(f"读取监控源失败：{e}")
            self.sources = []
        self._fill()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.sources))
        for r, src in enumerate(self.sources):
            color = None if src.enabled else "#9A9994"
            set_row(
                self.table, r,
                (
                    src.note or src.title or "（还没抓过）",
                    src.url,
                    utils.format_price(src.last_price) if src.last_price else "—",
                    utils.humanize(src.last_deadline) if src.last_deadline else "—",
                    src.last_checked or "未检查",
                    "是" if src.enabled else "否",
                ),
                colors={5: color} if color else None,
            )
            self.table.item(r, 0).setData(Qt.UserRole, src.id)
        self._sync_buttons()

    def _selected_ids(self) -> list[int]:
        ids = []
        for row in sorted({i.row() for i in self.table.selectedIndexes()}):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.UserRole) is not None:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def _sync_buttons(self) -> None:
        has = bool(self._selected_ids())
        for b in (self.check_btn, self.toggle_btn, self.del_btn):
            b.setEnabled(has)

    def _add(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            info(self, "链接不对", "要 http:// 或 https:// 开头的完整网址。")
            return
        if repo.get_by_url(url) is not None:
            info(self, "已经有了", "这个链接已经在监控列表里了。")
            return
        note, ok = QInputDialog.getText(
            self, "备注", "给它起个好认的名字（留空就用网页标题）：")
        if not ok:
            return
        src = WatchSource(url=url, note=note.strip(), enabled=True,
                          created_at=stamp())
        try:
            repo.create(src)
        except Exception as e:
            QMessageBox.warning(self, "添加失败", str(e))
            return
        self.url_edit.clear()
        self.refresh()
        self.status_label.setText("已加入监控列表，点「立即检查」先抓一次看看能不能读出来")

    def _delete(self) -> None:
        ids = self._selected_ids()
        if not ids or not confirm(self, "删除监控源",
                                  f"要删掉选中的 {len(ids)} 个监控源吗？\n已经入库的内容不会被删。"):
            return
        for sid in ids:
            repo.delete(sid)
        self.refresh()
        self.status_label.setText(f"已删除 {len(ids)} 个监控源")

    def _toggle(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        for sid in ids:
            src = repo.get(sid)
            if src is not None:
                repo.set_enabled(sid, not src.enabled)
        self.refresh()

    def _check_all(self) -> None:
        self._run_check(repo.all(only_enabled=True))

    def _check_selected(self) -> None:
        ids = set(self._selected_ids())
        picked = [s for s in self.sources if s.id in ids]
        self._run_check(picked)

    def _run_check(self, sources: list[WatchSource]) -> None:
        if not sources:
            self.status_label.setText("没有可检查的监控源")
            return
        if self._worker is not None and self._worker.isRunning():
            self.status_label.setText("上一轮还在跑，稍等…")
            return
        self.check_btn.setEnabled(False)
        self.check_all_btn.setEnabled(False)
        self.status_label.setText(f"正在检查 {len(sources)} 个监控源…")
        self._worker = WatchWorker(sources)
        self._worker.progress.connect(self.status_label.setText)
        self._worker.one_done.connect(self._on_one)
        self._worker.finished_all.connect(self._check_done)
        self._worker.start()

    def _on_one(self, change) -> None:
        self.refresh()

    def _check_done(self, changes: list) -> None:
        self.check_btn.setEnabled(True)
        self.check_all_btn.setEnabled(True)
        self.refresh()
        if not changes:
            self.status_label.setText("检查完毕：所有监控源都没变化")
            return
        lines = [c.summary() for c in changes[:8]]
        more = f"\n…还有 {len(changes) - 8} 条" if len(changes) > 8 else ""
        text = "\n".join(lines) + more
        self.status_label.setText(f"发现 {len(changes)} 处更新：{lines[0]}")
        QApplication.clipboard().setText(text)
        info(self, "盯梢结果",
             f"发现 {len(changes)} 处更新（已复制到剪贴板）：\n\n{text}")
