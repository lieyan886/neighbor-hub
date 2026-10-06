"""模块四：数据看板。

看的是运营结果：内容产出、同期进行、报名转化、拼单完成度。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
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
        # v1.5.0：时间范围 —— 以前所有数字都是全量聚合，看不出近期变化
        self.range_combo = QComboBox()
        self.range_combo.addItem("近 7 天", 7)
        self.range_combo.addItem("近 30 天", 30)
        self.range_combo.addItem("近 90 天", 90)
        self.range_combo.addItem("近半年", 180)
        self.range_combo.addItem("全部", None)
        self.range_combo.setCurrentIndex(1)      # 默认近 30 天
        self.range_combo.setFixedWidth(110)
        self.range_combo.currentIndexChanged.connect(self.refresh)
        head.addWidget(QLabel("统计范围"))
        head.addWidget(self.range_combo)
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
        self.card_people = StatCard("参与人数", "0", theme.ACCENT)
        self.card_units = StatCard("累计份数", "0", theme.PURPLE)
        self.card_rate = StatCard("成团率", "0%", theme.AMBER)
        self.card_per = StatCard("人均份数", "0", theme.BLUE)
        for c in (self.card_items, self.card_active, self.card_people,
                  self.card_units, self.card_rate, self.card_per):
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
        grid.addWidget(self._card_box("完成量 vs 目标", self.bar_label), 1, 0)
        grid.addWidget(self._card_box("热度排行（按人次）", self.top_label), 1, 1)

        # —— v1.7.0：价格走势 ——
        self.price_combo = QComboBox()
        self.price_combo.setToolTip(
            "入库、改价、盯梢抓到新价都会自动记一笔，攒久了能看出真降还是先涨后降")
        self.price_combo.currentIndexChanged.connect(self._refresh_price_chart)
        self.price_label = QLabel()
        self.price_label.setAlignment(Qt.AlignCenter)
        self.price_label.setMinimumHeight(200)
        self.price_hint = QLabel("")
        self.price_hint.setProperty("role", "muted")
        self.price_hint.setWordWrap(True)
        price_inner = QWidget()
        pv = QVBoxLayout(price_inner)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(6)
        pv.addWidget(self.price_combo)
        pv.addWidget(self.price_label, 1)
        pv.addWidget(self.price_hint)
        grid.addWidget(self._card_box("价格走势", price_inner), 2, 0, 1, 2)
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

    @property
    def _days(self) -> int | None:
        return self.range_combo.currentData()

    def refresh(self) -> None:
        days = self._days
        ov = stats_repo.overview(days)
        self.card_items.set_value(str(int(ov["items"])))
        self.card_active.set_value(str(int(ov["active"]) + int(ov["ending"])))
        self.card_people.set_value(str(int(ov["people"])))
        # v1.5.0 修正：这里以前用的是 overview()['signups']（报名记录条数），
        # 一人报 5 份只算 1，跟卡片标题「累计份数」不符。改用 units（SUM(qty)）。
        self.card_units.set_value(format_price(ov["units"]))

        fu = stats_repo.fulfillment(days)
        self.card_rate.set_value(f"{fu['rate']:g}%")
        self.card_rate.set_sub(f"{int(fu['formed'])}/{int(fu['total'])} 团凑够")

        ov_all = stats_repo.overview(None)
        per = ov["per_person"] or (ov_all["per_person"] if ov_all["people"] else 0)
        self.card_per.set_value(format_price(per))

        # 环比：本期 vs 上一个等长周期
        cmp_ = stats_repo.compare(days or 30)
        for card, key in ((self.card_items, "items"),
                          (self.card_people, "people"),
                          (self.card_units, "units")):
            card.set_sub(*self._delta_text(cmp_.get(key, {})))

        by_kind = stats_repo.count_by_kind(days)
        self.pie_label.setPixmap(charts.pie_chart(
            [kind_label(k) for k, _ in by_kind], [c for _, c in by_kind]))

        trend = stats_repo.monthly_trend(6 if not days or days >= 180 else 3)
        self.line_label.setPixmap(charts.line_chart(
            [l for l, _ in trend], [float(v) for _, v in trend]))

        prog = stats_repo.kind_progress(days)
        labels = [kind_label(k) for k, _, _ in prog]
        done = [float(d) for _, d, _ in prog]
        targets = [float(t) for _, _, t in prog]
        # v1.5.0：把目标值真正用上（以前只画 done，标题却写「完成进度」）
        self.bar_label.setPixmap(charts.target_bar_chart(labels, done, targets))

        tops = stats_repo.top_items(8, days)
        self.top_label.setPixmap(charts.horizontal_bar(
            [t for t, _, _ in tops], [float(h) for _, h, _ in tops]))

        self._load_price_subjects()
        self._refresh_price_chart()

    # ============================ 价格走势（v1.7.0） ============================

    def _load_price_subjects(self) -> None:
        """填充「看哪条内容的价格」下拉，尽量保住当前选择。"""
        from core.repository import prices

        current = self.price_combo.currentData()
        self.price_combo.blockSignals(True)
        self.price_combo.clear()
        subs = prices.subjects()
        if not subs:
            self.price_combo.addItem("还没有价格记录", None)
            self.price_combo.setEnabled(False)
            self.price_combo.blockSignals(False)
            return
        self.price_combo.setEnabled(True)
        for key, label, n in subs:
            self.price_combo.addItem(f"{label}（{n} 个价格点）", key)
        if current:
            idx = self.price_combo.findData(current)
            if idx >= 0:
                self.price_combo.setCurrentIndex(idx)
        self.price_combo.blockSignals(False)

    def _refresh_price_chart(self) -> None:
        from core.repository import prices

        key = self.price_combo.currentData()
        if not key:
            self.price_label.clear()
            self.price_hint.setText(
                "入库、手动改价、盯梢抓到新价都会自动记一笔；现在还没有任何记录。")
            return
        if key.startswith("item:"):
            iid: int | None = int(key.split(":", 1)[1])
            title = ""
        else:
            iid, title = None, key.split(":", 1)[1]

        seq = prices.series(iid, title)
        if len(seq) < 2:
            self.price_label.clear()
            self.price_hint.setText("只有 1 个价格点，还画不出走势——下次改价或盯梢后再来看。")
            return
        # 横坐标只留日期，同一天多次改价也能看出当天的波动
        labels = [(t[5:10] if len(t) >= 10 else t) for t, _ in seq]
        self.price_label.setPixmap(charts.price_trend_chart(
            labels, [p for _, p in seq]))

        s = prices.summary(iid, title)
        now, low, high, first = s["now"], s["low"], s["high"], s["first"]
        if now <= low + 1e-6:
            verdict = "现在是历史最低价，可以下手"
        elif now >= high - 1e-6:
            verdict = "现在是历史最高价，再等等"
        elif now < first:
            verdict = f"比首次记录（{format_price(first)}）便宜了"
        else:
            verdict = f"比首次记录（{format_price(first)}）还贵"
        self.price_hint.setText(
            f"当前 {format_price(now)}　最高 {format_price(high)}　"
            f"最低 {format_price(low)}　共 {int(s['points'])} 次记录 —— {verdict}")

    @staticmethod
    def _delta_text(c: dict) -> tuple[str, str]:
        """把环比结果转成 (文字, 颜色)。涨用红、跌用绿 —— 跟国内行情习惯一致。"""
        delta = c.get("delta")
        now, prev = c.get("now", 0), c.get("prev", 0)
        if not now and not prev:
            return "", theme.TEXT_MUTED          # 两期都空，不显示环比
        if delta is None:
            return "上期无数据" if not prev else "持平", theme.TEXT_MUTED
        if delta > 0:
            return f"↑ {delta:g}% 环比", theme.RED
        if delta < 0:
            return f"↓ {abs(delta):g}% 环比", theme.GREEN
        return "持平", theme.TEXT_MUTED

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
