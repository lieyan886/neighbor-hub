# -*- coding: utf-8 -*-
"""v1.8.1：分发记录查看对话框。

一键推群、手动记分发都会往 publish_logs 写一条，以前只写不读 ——
团长想知道「这条内容上次是什么时候发的、发的哪个渠道」只能翻数据库。
这里给个只读的查看入口。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import utils
from core.repository import publishes as publish_repo
from ui import theme
from ui.widgets import hint_label, section_title

_CHANNEL_LABEL = {
    "wechat": "微信群",
    "wechat_group": "微信群",
    "moments": "朋友圈",
    "offline": "线下/打印",
    "other": "其他",
}


def _channel_text(key: str) -> str:
    key = (key or "").strip()
    return _CHANNEL_LABEL.get(key, key or "未标注")


class PublishLogDialog(QDialog):
    """只读列表：最近的分发留痕。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("分发记录")
        self.resize(640, 420)
        self.setStyleSheet(theme.QSS)
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(section_title("分发记录"))
        layout.addWidget(hint_label(
            "只留在这台电脑上，不会外发。最多显示最近 100 条。"))

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时间", "内容", "渠道", "备注"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.summary = QLabel("")
        layout.addWidget(self.summary)

        row = QHBoxLayout()
        row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setProperty("variant", "ghost")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)

    def _load(self) -> None:
        logs = publish_repo.recent(limit=100)
        self.table.setRowCount(len(logs))
        for r, log in enumerate(logs):
            created = utils.humanize(log["created_at"] or "")
            title = log["title"] or "（内容已删除）"
            note = (log["text"] or "").strip() or "—"
            values = [created, title, _channel_text(log["channel"]), note]
            for c, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if c == 1:
                    cell.setToolTip(title)
                self.table.setItem(r, c, cell)
        self.summary.setText(
            f"共 {len(logs)} 条记录" if logs else "还没有分发记录——用「一键推群」发过就会记在这里")
