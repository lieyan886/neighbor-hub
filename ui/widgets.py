"""界面共用小组件：统计卡、标题、表格构造、提示框。"""
from __future__ import annotations

from typing import Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui import theme


def section_title(text: str, subtitle: str = "") -> QLabel:
    """带层级的区块标题。"""
    lab = QLabel(text)
    lab.setProperty("role", "title")
    if subtitle:
        lab.setToolTip(subtitle)
    return lab


def muted_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("role", "muted")
    lab.setWordWrap(True)
    return lab


def hint_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("role", "hint")
    lab.setWordWrap(True)
    return lab


class StatCard(QFrame):
    """数据看板顶部的一格：大数字 + 小标题。"""

    def __init__(self, title: str, value: str = "-", color: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("surface")
        self.setMinimumHeight(84)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)

        self.value_label = QLabel(value)
        self.value_label.setProperty("role", "stat-value")
        if color:
            self.value_label.setStyleSheet(f"color: {color};")
        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "stat-title")

        layout.addWidget(self.value_label)
        layout.addWidget(self.title_label)
        layout.addStretch(1)

    def set_value(self, value: str) -> None:
        self.value_label.setText(str(value))


class EmptyState(QWidget):
    """列表没东西时占位的提示面板。"""

    def __init__(self, text: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(6)
        lab = QLabel(text)
        lab.setProperty("role", "muted")
        lab.setAlignment(Qt.AlignCenter)
        layout.addWidget(lab)
        if hint:
            layout.addWidget(hint_label(hint))
        lab2 = layout.itemAt(0).widget()
        if lab2:
            lab2.setStyleSheet("font-size: 15px;")


def make_table(headers: Sequence[str], stretch_col: int | None = None,
               row_height: int = 34) -> QTableWidget:
    """构造一个统一风格的表格。"""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(False)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(row_height)
    table.setShowGrid(False)

    head = table.horizontalHeader()
    head.setHighlightSections(False)
    for i in range(len(headers)):
        head.setSectionResizeMode(i, QHeaderView.Interactive)
    if stretch_col is not None and 0 <= stretch_col < len(headers):
        head.setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    return table


def set_row(table: QTableWidget, row: int, values: Iterable,
            colors: dict[int, str] | None = None) -> None:
    """按顺序填充一行，可选指定列的颜色。"""
    colors = colors or {}
    for col, value in enumerate(values):
        item = QTableWidgetItem(str(value) if value is not None else "")
        if col in colors:
            item.setForeground(colors[col])  # type: ignore[arg-type]
        if col == 0:
            item.setData(Qt.UserRole, value)
        table.setItem(row, col, item)


def set_placeholder(widget, text: str) -> None:
    """给输入类控件设占位符（顺便兼容 QTextEdit）。"""
    try:
        widget.setPlaceholderText(text)
    except AttributeError:
        pass


def confirm(parent: QWidget, title: str, text: str) -> bool:
    """危险操作确认框。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning)
    box.setStyleSheet(theme.QSS)
    yes = box.addButton("确认删除", QMessageBox.AcceptRole)
    no = box.addButton("取消", QMessageBox.RejectRole)
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


def info(parent: QWidget, title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Information)
    box.setStyleSheet(theme.QSS)
    box.addButton("知道了", QMessageBox.AcceptRole)
    box.exec()


def warn(parent: QWidget, title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning)
    box.setStyleSheet(theme.QSS)
    box.addButton("知道了", QMessageBox.AcceptRole)
    box.exec()


def busy_label(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("role", "hint")
    return lab
