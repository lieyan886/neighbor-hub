"""模块四：数据看板。

看的是运营结果：内容产出、同期进行、报名转化、拼单完成度。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.models import KIND_LABELS, kind_label
from core.repository import items as item_repo
from core.repository import signups as signup_repo
from core.repository import stats as stats_repo
from core.utils import format_price
from exports import excel
from ui import charts, theme
from ui.widgets import StatCard, info, section_title, warn


class StatsPanel(QWidget):
    """数据看板：数字卡 + 四张图表 + 导出。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self.refresh()

    # ============================ UI ============================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.addWidget(section_title("数据看板"))
        head.addStretch(1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setProperty("variant", "ghost")
        self.refresh_btn.clicked.connect(self.refresh)
        self.export_items_btn = QPushButton("导出内容总表")
        self.export_items_btn.setProperty("variant", "ghost")
        self.export_items_btn.clicked.connect(self._export_items)
        self.export_pack_btn = QPushButton("导出提货清单")
        self.export_pack_btn.setProperty("variant", "ghost")
        self.export_pack_btn.clicked.connect(self._export_packing)
        head.addWidget(self.export_items_btn)
        head.addWidget(self.export_pack_btn)
        head.addWidget(self.refresh_btn)
        root.addLayout(head)

        # —— 数字卡 ——
        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(12)
        self.card_items = StatCard("内容总数", "0")
        self.card_active = StatCard("进行中", "0", theme.TEAL)
        self.card_ending = StatCard("即将截止", "0", theme.AMBER)
        self.card_people = StatCard("参与人次", "0", theme.ACCENT)
        self.card_units = StatCard("累计份数", "0", theme.PURPLE)
        for c in (self.card_items, self.card_active, self.card_ending,
                  self.card_people, self.card_units):
            self.cards_row.addWidget(c)
        root.addLayout(self.cards_row)

        # —— 图表区 ——
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setSpacing(14)

        self.pie_label = QLabel()
        self.line_label = QLabel()
        self.bar_label = QLabel()
        self.top_label = QLabel()
        for lab in (self.pie_label, self.line_label, self.bar_label, self.top_label):
            lab.setAlignment(Qt.AlignCenter)
            lab.setMinimumHeight(220)

        grid.addWidget(self._card_box("内容类型分布", self.pie_label), 0, 0)
        grid.addWidget(self._card_box("近半年发布趋势", self.line_label), 0, 1)
        grid.addWidget(self._card_box("各类型完成进度", self.bar_label), 1, 0)
        grid.addWidget(self._card_box("热度排行（按人次）", self.top_label), 1, 1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

    def _card_box(self, title: str, inner: QWidget) -> QFrame:
        box = QFrame()
        box.setObjectName("surface")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 10, 12, 12)
        lab = QLabel(title)
        lab.setProperty("role", "stat-title")
        layout.addWidget(lab)
        layout.addWidget(inner, 1)
        return box

    # ============================ 数据 ============================

    def refresh(self) -> None:
        ov = stats_repo.overview()
        self.card_items.set_value(str(ov["items"]))
        self.card_active.set_value(str(ov["active"] + ov["ending"]))
        self.card_ending.set_value(str(ov["ending"]))
        self.card_people.set_value(str(ov["people"]))
        self.card_units.set_value(format_price(
            stats_repo.overview().get("signups", 0)))

        by_kind = stats_repo.count_by_kind()
        self.pie_label.setPixmap(charts.pie_chart(
            [kind_label(k) for k, _ in by_kind], [c for _, c in by_kind]))

        trend = stats_repo.monthly_trend()
        self.line_label.setPixmap(charts.line_chart(
            [l for l, _ in trend], [float(v) for _, v in trend]))

        prog = stats_repo.kind_progress()
        labels = [kind_label(k) for k, _, _ in prog]
        values = [int(done) for _, done, _ in prog]
        targets = [int(t) for _, _, t in prog]
        self.bar_label.setPixmap(charts.bar_chart(labels, values))

        tops = stats_repo.top_items()
        self.top_label.setPixmap(charts.horizontal_bar(
            [t for t, _, _ in tops], [float(h) for _, h, _ in tops]))

    # ============================ 导出 ============================

    def _export_items(self) -> None:
        rows = item_repo.list_items(include_archived=True, limit=5000)
        if not rows:
            warn(self, "没有数据", "内容库还是空的。")
            return
        path = excel.export_items(rows)
        info(self, "导出完成", f"已保存到：\n{path}")

    def _export_packing(self) -> None:
        rows = item_repo.list_items(kind="groupbuy", include_archived=False)
        if not rows:
            warn(self, "没有数据", "还没有拼单内容。")
            return
        mapping = {it.id: signup_repo.list_for(it.id) for it in rows}
        if not any(mapping.values()):
            warn(self, "没有数据", "拼单还没有人参与。")
            return
        path = excel.export_groupbuy_packing(rows, mapping)
        info(self, "导出完成", f"已保存到：\n{path}")
