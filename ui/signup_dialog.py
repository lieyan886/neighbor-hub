"""报名 / 接龙录入对话框：单条编辑 + 群接龙文本批量粘贴。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.models import Item, Signup
from core.utils import clean_text
from render.copywriter import (
    ParseResult,
    dedupe_solitaire,
    parse_solitaire_detail,
)
from ui import theme
from ui.widgets import make_table

_UNITS = ("份", "件", "斤", "箱", "人", "个", "袋", "盒")


class SignupDialog(QDialog):
    """手工登记一条报名。"""

    def __init__(self, parent=None, signup: Signup | None = None,
                 item: Item | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.signup = signup or Signup(item_id=item.id if item else 0)
        self.setWindowTitle("登记报名" if not (signup and signup.id) else "修改报名")
        self.resize(420, 320)
        self.setStyleSheet(theme.QSS)
        self._build_ui()
        if signup and signup.id:
            self._load(signup)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        form = QFormLayout()
        form.setSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("昵称或门牌号")
        self.contact_edit = QLineEdit()
        self.contact_edit.setPlaceholderText("手机号 / 微信（可选）")
        self.qty_spin = QDoubleSpinBox()
        self.qty_spin.setRange(0.5, 9999)
        self.qty_spin.setDecimals(1)
        self.qty_spin.setValue(1.0)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(_UNITS)
        self.unit_combo.setEditable(True)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("如：要辣口的 / 晚点来取")
        self.settled_box = QCheckBox("已结清")

        form.addRow("昵称 / 房号", self.name_edit)
        form.addRow("联系方式", self.contact_edit)
        form.addRow("数量", self.qty_spin)
        form.addRow("单位", self.unit_combo)
        form.addRow("备注", self.note_edit)
        form.addRow("", self.settled_box)
        layout.addLayout(form)

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Save).setText("保存")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _load(self, s: Signup) -> None:
        self.name_edit.setText(s.name)
        self.contact_edit.setText(s.contact)
        self.qty_spin.setValue(float(s.qty or 1))
        idx = self.unit_combo.findText(s.unit or "份")
        if idx >= 0:
            self.unit_combo.setCurrentIndex(idx)
        else:
            self.unit_combo.setEditText(s.unit or "份")
        self.note_edit.setText(s.note)
        self.settled_box.setChecked(s.settled)

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            self.name_edit.setFocus()
            return
        self.accept()

    def get_signup(self) -> Signup:
        s = self.signup
        s.name = clean_text(self.name_edit.text())
        s.contact = clean_text(self.contact_edit.text())
        s.qty = float(self.qty_spin.value())
        s.unit = self.unit_combo.currentText().strip() or "份"
        s.note = clean_text(self.note_edit.text())
        s.settled = self.settled_box.isChecked()
        return s


class PasteSolitaireDialog(QDialog):
    """把微信群里回收的接龙文本整段粘进来，批量变结构化报名。"""

    def __init__(self, parent=None, item: Item | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.rows: list[tuple[str, float]] = []
        self.result: ParseResult = ParseResult()
        self.setWindowTitle("粘贴群接龙")
        self.resize(640, 560)
        self.setStyleSheet(theme.QSS)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        layout.addWidget(QLabel("把群里邻居回复的接龙全选复制，粘到下面："))
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "1. 张三 2份\n2. 李雷 1\n3、3栋王姐 3份\n"
            "张三改成3份\n李四不要了\n收到\n…\n"
            "（闲聊、表情、时间戳会自动滤掉）"
        )
        layout.addWidget(self.text_edit, 1)

        ctrl = QHBoxLayout()
        self.unit_edit = QLineEdit(self.item.unit if self.item else "份")
        self.unit_edit.setFixedWidth(70)
        self.parse_btn = QPushButton("解析预览")
        self.parse_btn.setProperty("accent", True)
        self.parse_btn.clicked.connect(self._parse)
        ctrl.addWidget(QLabel("单位"))
        ctrl.addWidget(self.unit_edit)
        ctrl.addWidget(self.parse_btn)
        ctrl.addStretch(1)
        layout.addLayout(ctrl)

        self.table = make_table(["昵称 / 房号", "数量", "说明"], stretch_col=0)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 90)
        layout.addWidget(self.table, 2)

        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "hint")
        layout.addWidget(self.summary_label)

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Save).setText("导入")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _parse(self) -> None:
        unit = self.unit_edit.text().strip() or "份"
        self.result = parse_solitaire_detail(self.text_edit.toPlainText(), unit)
        self.rows = self.result.rows
        self.table.setRowCount(0)

        for name, qty in self.rows:
            self._add_row(name, f"{qty:g} {unit}", "新增")
        for name, qty in self.result.adjustments:
            if qty is None:
                self._add_row(name, "—", "取消")
            else:
                self._add_row(name, f"{qty:g} {unit}", "改单")

        added = len(self.rows)
        total = sum(q for _, q in self.rows)
        bits = [f"新增 {added} 人，合计 {total:g} {unit}"]
        adj_count = len(self.result.adjustments)
        if adj_count:
            bits.append(f"改单/取消 {adj_count} 条")
        if self.result.skipped:
            bits.append(f"{len(self.result.skipped)} 行没认出来（闲聊/表情已忽略）")
        self.summary_label.setText("　·　".join(bits))

    def _add_row(self, name: str, qty: str, mark: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(name))
        cell = QTableWidgetItem(qty)
        cell.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(r, 1, cell)
        tag = QTableWidgetItem(mark)
        tag.setTextAlignment(Qt.AlignCenter)
        if mark != "新增":
            tag.setForeground(theme.color(theme.ACCENT))
        self.table.setItem(r, 2, tag)

    def accept(self) -> None:
        if not self.result:
            self._parse()
        if not self.rows and not self.result.adjustments:
            return
        super().accept()
