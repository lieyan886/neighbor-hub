"""条目编辑对话框：新建 / 修改三类通用内容。"""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from core import config
from core.models import (
    KIND_LABELS,
    KIND_ORDER,
    STATUS_LABELS,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_EXPIRED,
    Item,
)
from core.utils import parse_datetime, to_iso
from ui import theme

_STATUS_ORDER = (STATUS_DRAFT, STATUS_ACTIVE, "ending", STATUS_EXPIRED, STATUS_ARCHIVED)


class ItemDialog(QDialog):
    """一个对话框搞定活动 / 拼单 / 优惠三种形态的录入。"""

    def __init__(self, parent=None, item: Item | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑内容" if item and item.id else "新建内容")
        self.resize(720, 640)
        self.item = item or Item()
        self._cover_path = self.item.cover
        self._build_ui()
        self._load(self.item)
        self.setStyleSheet(theme.QSS)

    # —— UI 构建 ——

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        self.kind_combo = QComboBox()
        for k in KIND_ORDER:
            self.kind_combo.addItem(KIND_LABELS[k], k)
        self.status_combo = QComboBox()
        for s in _STATUS_ORDER:
            self.status_combo.addItem(STATUS_LABELS.get(s, s), s)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("一句话说清楚：什么活动 / 什么货 / 什么优惠")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://…（可不填）")
        self.merchant_edit = QLineEdit()
        self.merchant_edit.setPlaceholderText("商户名或主办方")
        self.location_edit = QLineEdit()
        self.location_edit.setPlaceholderText("活动地点 / 自提点")

        self.price_spin = QDoubleSpinBox()
        self.price_spin.setRange(0, 1_000_000)
        self.price_spin.setDecimals(2)
        self.price_spin.setSpecialValueText("未设置")
        self.origin_spin = QDoubleSpinBox()
        self.origin_spin.setRange(0, 1_000_000)
        self.origin_spin.setDecimals(2)
        self.origin_spin.setSpecialValueText("未设置")
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("份 / 斤 / 人")
        self.quota_spin = QSpinBox()
        self.quota_spin.setRange(0, 100_000)
        self.quota_spin.setSuffix(" 份或名额")

        self.event_edit = QLineEdit()
        self.event_edit.setPlaceholderText("如：10月5日 14:30 / 明天 18:00")
        self.deadline_edit = QLineEdit()
        self.deadline_edit.setPlaceholderText("如：10月4日 / 后天 / 2026-10-05")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("逗号分隔：生鲜果蔬,满减")

        self.summary_edit = QPlainTextEdit()
        self.summary_edit.setPlaceholderText("群里会看到的主要说明，2-3 句话说清亮点")
        self.summary_edit.setFixedHeight(96)
        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText("只有你自己看的备注：进货渠道、注意事项…")
        self.note_edit.setFixedHeight(72)

        self.cover_btn = QPushButton("选择封面图…")
        self.cover_btn.setProperty("variant", "ghost")
        self.cover_btn.clicked.connect(self._pick_cover)
        self.cover_label = QLabel("未选择")
        self.cover_label.setProperty("role", "hint")

        # 第一行：类型 + 状态
        grid.addWidget(QLabel("类型"), 0, 0)
        grid.addWidget(self.kind_combo, 0, 1)
        grid.addWidget(QLabel("状态"), 0, 2)
        grid.addWidget(self.status_combo, 0, 3)

        grid.addWidget(QLabel("标题"), 1, 0)
        grid.addWidget(self.title_edit, 1, 1, 1, 3)

        grid.addWidget(QLabel("摘要"), 2, 0)
        grid.addWidget(self.summary_edit, 2, 1, 1, 3)

        grid.addWidget(QLabel("链接"), 3, 0)
        grid.addWidget(self.url_edit, 3, 1, 1, 3)

        grid.addWidget(QLabel("商户/主办"), 4, 0)
        grid.addWidget(self.merchant_edit, 4, 1)
        grid.addWidget(QLabel("地点"), 4, 2)
        grid.addWidget(self.location_edit, 4, 3)

        grid.addWidget(QLabel("单价"), 5, 0)
        grid.addWidget(self.price_spin, 5, 1)
        grid.addWidget(QLabel("原价"), 5, 2)
        grid.addWidget(self.origin_spin, 5, 3)

        grid.addWidget(QLabel("单位"), 6, 0)
        grid.addWidget(self.unit_edit, 6, 1)
        grid.addWidget(QLabel("目标量"), 6, 2)
        grid.addWidget(self.quota_spin, 6, 3)

        grid.addWidget(QLabel("活动时间"), 7, 0)
        grid.addWidget(self.event_edit, 7, 1)
        grid.addWidget(QLabel("截止时间"), 7, 2)
        grid.addWidget(self.deadline_edit, 7, 3)

        grid.addWidget(QLabel("标签"), 8, 0)
        grid.addWidget(self.tags_edit, 8, 1, 1, 3)

        grid.addWidget(QLabel("封面"), 9, 0)
        cover_row = QHBoxLayout()
        cover_row.addWidget(self.cover_btn, 0)
        cover_row.addWidget(self.cover_label, 1)
        grid.addLayout(cover_row, 9, 1, 1, 3)

        grid.addWidget(QLabel("内部备注"), 10, 0)
        grid.addWidget(self.note_edit, 10, 1, 1, 3)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        root.addLayout(grid, 1)

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Save).setText("保存")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

    # —— 数据 ——

    def _load(self, item: Item) -> None:
        self.title_edit.setText(item.title)
        self.summary_edit.setPlainText(item.summary)
        self.url_edit.setText(item.url)
        self.merchant_edit.setText(item.merchant)
        self.location_edit.setText(item.location)
        self.unit_edit.setText(item.unit)
        self.quota_spin.setValue(int(item.quota or 0))
        self.event_edit.setText(item.event_at)
        self.deadline_edit.setText(item.deadline)
        self.tags_edit.setText(item.tags)
        self.note_edit.setPlainText(item.note)
        self.price_spin.setValue(float(item.price or 0))
        self.origin_spin.setValue(float(item.origin_price or 0))

        idx = self.kind_combo.findData(item.kind)
        if idx >= 0:
            self.kind_combo.setCurrentIndex(idx)
        sidx = self.status_combo.findData(item.status)
        if sidx >= 0:
            self.status_combo.setCurrentIndex(sidx)
        self._set_cover(item.cover)

    def _set_cover(self, path: str) -> None:
        self._cover_path = path
        self.cover_label.setText(Path(path).name if path else "未选择")

    def _pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not path:
            return
        config.ensure_dirs()
        dest = config.COVER_DIR / Path(path).name
        try:
            shutil.copy(path, dest)
            self._set_cover(str(dest))
        except OSError:
            self._set_cover(path)

    def _accept(self) -> None:
        title = self.title_edit.text().strip()
        if not title:
            self.title_edit.setFocus()
            return
        self._title_ok = True
        self.accept()

    def get_item(self) -> Item:
        """读取表单，返回一个填好的 Item（不含 id 变更逻辑）。"""
        it = self.item
        it.title = self.title_edit.text().strip()
        it.kind = self.kind_combo.currentData()
        it.status = self.status_combo.currentData()
        it.summary = self.summary_edit.toPlainText().strip()
        it.url = self.url_edit.text().strip()
        it.merchant = self.merchant_edit.text().strip()
        it.location = self.location_edit.text().strip()
        it.unit = self.unit_edit.text().strip()
        it.quota = int(self.quota_spin.value())
        it.tags = ",".join(
            t.strip() for t in self.tags_edit.text().replace("，", ",").split(",")
            if t.strip()
        )
        it.note = self.note_edit.toPlainText().strip()
        it.cover = self._cover_path or ""

        price = self.price_spin.value()
        it.price = float(price) if price > 0 else None
        origin = self.origin_spin.value()
        it.origin_price = float(origin) if origin > 0 else None

        it.event_at = to_iso(parse_datetime(self.event_edit.text()))
        it.deadline = to_iso(parse_datetime(self.deadline_edit.text()))
        return it
