"""模块二：内容管理台。

左边列表筛选三类内容，右边详情页处理生命周期与报名明细。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import (
    KIND_LABELS,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_ENDING,
    STATUS_EXPIRED,
    STATUS_LABELS,
    Item,
    kind_label,
    status_label,
)
from core.repository import items as item_repo
from core.repository import signups as signup_repo
from core.utils import format_price, humanize, parse_datetime, to_iso
from exports import excel
from ui import theme
from ui.item_dialog import ItemDialog
from ui.settle_dialog import SettleDialog
from ui.signup_dialog import PasteSolitaireDialog, SignupDialog
from ui.widgets import EmptyState, confirm, hint_label, make_table, warn

_HEADERS = ("标题", "类型", "价格", "参与", "截止", "状态")
_STATUS_ORDER = (STATUS_DRAFT, STATUS_ACTIVE, STATUS_ENDING, STATUS_EXPIRED, STATUS_ARCHIVED)


class ManagePanel(QWidget):
    """内容库：筛选、编辑、改状态、登记接龙。"""

    data_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_item: Item | None = None
        self._loading_signup = False
        self._build_ui()
        self.refresh()

    # ============================ UI ============================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(QLabel("内容管理台", objectName="none"))
        root.itemAt(root.count() - 1).widget().setProperty("role", "title")

        # —— 筛选行 ——
        head = QHBoxLayout()
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("全部类型", "")
        for k in (KIND_LABELS[k] for k in ("event", "groupbuy", "deal")):
            self.kind_combo.addItem(k, [key for key, v in KIND_LABELS.items() if v == k][0])
        self.status_combo = QComboBox()
        self.status_combo.addItem("全部状态", "")
        self.status_combo.addItem("未归档", "__active__")
        for s in _STATUS_ORDER:
            self.status_combo.addItem(STATUS_LABELS[s], s)
        self.status_combo.setCurrentIndex(1)
        self.tag_combo = QComboBox()
        self.tag_combo.addItem("全部标签", "")
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("搜索标题 / 商户 / 地点")
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        self.new_btn = QPushButton("新建")
        self.new_btn.setProperty("accent", True)
        self.new_btn.clicked.connect(self._create)

        for combo in (self.kind_combo, self.status_combo, self.tag_combo):
            combo.currentIndexChanged.connect(self.refresh)
        self.keyword_edit.returnPressed.connect(self.refresh)

        head.addWidget(self.kind_combo)
        head.addWidget(self.status_combo)
        head.addWidget(self.tag_combo)
        head.addWidget(self.keyword_edit, 1)
        head.addWidget(self.refresh_btn)
        head.addWidget(self.new_btn)
        root.addLayout(head)

        # —— 主体：表格 | 详情 ——
        self.table = make_table(_HEADERS, stretch_col=0)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setColumnWidth(1, 86)
        self.table.setColumnWidth(2, 84)
        self.table.setColumnWidth(3, 92)
        self.table.setColumnWidth(4, 118)
        self.table.setColumnWidth(5, 86)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: self._load_detail(self._selected_item())
        )

        self.detail = self._build_detail()
        self.detail.setMinimumWidth(360)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

    def _build_detail(self) -> QWidget:
        box = QFrame()
        box.setObjectName("surface")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        self.detail_title = QLabel("未选择内容")
        self.detail_title.setProperty("role", "title")
        self.detail_title.setWordWrap(True)
        self.detail_meta = QLabel("")
        self.detail_meta.setProperty("role", "muted")
        self.detail_meta.setWordWrap(True)

        self.detail_summary = QLabel("")
        self.detail_summary.setProperty("role", "muted")
        self.detail_summary.setWordWrap(True)

        btn_row = QHBoxLayout()
        self.edit_btn = QPushButton("编辑")
        self.edit_btn.clicked.connect(self._edit)
        self.publish_btn = QPushButton("发布")
        self.publish_btn.setProperty("variant", "ghost")
        self.publish_btn.clicked.connect(self._publish)
        self.archive_btn = QPushButton("归档")
        self.archive_btn.setProperty("variant", "ghost")
        self.archive_btn.clicked.connect(self._archive)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.setProperty("variant", "danger")
        self.delete_btn.clicked.connect(self._delete)
        for b in (self.edit_btn, self.publish_btn, self.archive_btn, self.delete_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(16)
        self.progress_label = hint_label("")

        sign_head = QHBoxLayout()
        sign_head.addWidget(QLabel("报名 / 接龙"))
        sign_head.addStretch(1)
        self.add_signup_btn = QPushButton("登记")
        self.add_signup_btn.clicked.connect(self._add_signup)
        self.paste_btn = QPushButton("粘贴接龙")
        self.paste_btn.clicked.connect(self._paste_solitaire)
        self.settle_btn = QPushButton("结算")
        self.settle_btn.setProperty("variant", "ghost")
        self.settle_btn.clicked.connect(self._settle)
        self.dup_btn = QPushButton("再来一团")
        self.dup_btn.setProperty("variant", "ghost")
        self.dup_btn.setToolTip("把这条内容复制成新一期：标题「第N期」自动+1、截止顺延 7 天、报名清空")
        self.dup_btn.clicked.connect(self._duplicate)
        self.del_signup_btn = QPushButton("删除")
        self.del_signup_btn.setProperty("variant", "danger")
        self.del_signup_btn.clicked.connect(self._del_signup)
        self.export_btn = QPushButton("导出 Excel")
        self.export_btn.setProperty("variant", "ghost")
        self.export_btn.clicked.connect(self._export_signups)
        for b in (self.add_signup_btn, self.paste_btn, self.settle_btn,
                  self.dup_btn, self.del_signup_btn, self.export_btn):
            sign_head.addWidget(b)

        self.signup_table = make_table(("昵称/房号", "数量", "单位", "备注", "已结清"))
        self.signup_table.setMaximumHeight(240)
        self.signup_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.signup_table.setColumnWidth(4, 76)
        self.signup_table.itemChanged.connect(self._on_signup_changed)

        layout.addWidget(self.detail_title)
        layout.addWidget(self.detail_meta)
        layout.addWidget(self.detail_summary)
        layout.addLayout(btn_row)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)
        layout.addLayout(sign_head)
        layout.addWidget(self.signup_table, 1)
        self._set_detail_enabled(False)
        return box

    def _set_detail_enabled(self, enabled: bool) -> None:
        for w in (self.edit_btn, self.publish_btn, self.archive_btn, self.delete_btn,
                  self.add_signup_btn, self.paste_btn, self.settle_btn,
                  self.dup_btn, self.del_signup_btn, self.export_btn,
                  self.signup_table):
            w.setEnabled(enabled)

    # ============================ 数据 ============================

    def refresh(self) -> None:
        tags = item_repo.all_tags()
        cur_tag = self.tag_combo.currentData()
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("全部标签", "")
        for t in tags:
            self.tag_combo.addItem(t, t)
        if cur_tag:
            idx = self.tag_combo.findData(cur_tag)
            self.tag_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.tag_combo.blockSignals(False)

        status = self.status_combo.currentData()
        rows = item_repo.list_items(
            kind=self.kind_combo.currentData() or None,
            keyword=self.keyword_edit.text().strip() or None,
            tag=self.tag_combo.currentData() or None,
            include_archived=(status == STATUS_ARCHIVED or status == ""),
            status=(status if status not in ("", "__active__") else None),
        )
        self.table.setRowCount(0)
        for it in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            price = f"¥{format_price(it.price)}" if it.price is not None else "-"
            head = signup_repo.headcount(it.id or 0)
            total = signup_repo.total_qty(it.id or 0)
            join = f"{head}人/{format_price(total)}{it.unit or ''}" if head else "-"
            shown = it.derive_status()
            values = (it.title, kind_label(it.kind), price, join,
                      humanize(it.deadline or it.event_at), status_label(shown))
            self._set_table_row(r, it, values, shown)
        self._load_detail(self._selected_item())

    def _set_table_row(self, row: int, item: Item, values: tuple, status: str) -> None:
        for col, val in enumerate(values):
            cell = QTableWidgetItem(str(val))
            cell.setData(Qt.UserRole, item.id)
            if col == 0:
                cell.setToolTip(item.summary or item.title)
            if col in (1, 4, 5):
                cell.setTextAlignment(Qt.AlignCenter)
            if col == 5:
                cell.setForeground(theme.color(theme.status_color(status)))
            if col == 1:
                cell.setForeground(theme.color(theme.kind_color(item.kind)))
            self.table.setItem(row, col, cell)

    def _selected_item(self) -> Item | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        cell = self.table.item(row, 0)
        if not cell:
            return None
        return item_repo.get(int(cell.data(Qt.UserRole)))

    def _load_detail(self, item: Item | None) -> None:
        self.current_item = item
        if item is None:
            self.detail_title.setText("未选择内容")
            self.detail_meta.setText("")
            self.detail_summary.setText("左侧选一条，这里显示详情。")
            self.signup_table.setRowCount(0)
            self.progress_bar.setValue(0)
            self.progress_label.setText("")
            self._set_detail_enabled(False)
            return

        self._set_detail_enabled(True)
        self.detail_title.setText(item.title)
        bits = [kind_label(item.kind)]
        if item.merchant:
            bits.append(item.merchant)
        if item.location:
            bits.append(item.location)
        if item.price is not None:
            bits.append(f"¥{format_price(item.price)}{'/' + item.unit if item.unit else ''}")
        when = item.event_at if item.kind == "event" and item.event_at else item.deadline
        if when:
            bits.append(humanize(when))
        bits.append(status_label(item.derive_status()))
        self.detail_meta.setText(" · ".join(bits))
        self.detail_summary.setText(item.summary or "（没有写摘要）")
        if item.url:
            self.detail_summary.setToolTip(item.url)

        rows = signup_repo.list_for(item.id or 0)
        done = sum(float(s.qty or 0) for s in rows)
        if item.quota:
            self.progress_bar.setRange(0, int(item.quota))
            self.progress_bar.setValue(int(min(done, item.quota)))
            pct = done / item.quota * 100
            self.progress_label.setText(
                f"已完成 {format_price(done)} / {item.quota}{item.unit or '份'}（{pct:.0f}%）"
            )
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_label.setText(
                f"共 {len(rows)} 人，合计 {format_price(done)}{item.unit or '份'}"
                if rows else "还没人报名"
            )

        # 「已结清」这一列做成可勾选，勾选即写库；重建表格时要压掉 itemChanged
        self._loading_signup = True
        self.signup_table.blockSignals(True)
        self.signup_table.setRowCount(0)
        for s in rows:
            r = self.signup_table.rowCount()
            self.signup_table.insertRow(r)
            vals = (s.name, f"{s.qty:g}", s.unit or "份", s.note)
            for col, val in enumerate(vals):
                cell = QTableWidgetItem(val)
                cell.setData(Qt.UserRole, s.id)
                if col in (1, 2):
                    cell.setTextAlignment(Qt.AlignCenter)
                self.signup_table.setItem(r, col, cell)
            check = QTableWidgetItem("")
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if s.settled else Qt.Unchecked)
            check.setData(Qt.UserRole, s.id)
            check.setTextAlignment(Qt.AlignCenter)
            self.signup_table.setItem(r, 4, check)
        self.signup_table.blockSignals(False)
        self._loading_signup = False

    # ============================ 操作 ============================

    def _create(self) -> None:
        dlg = ItemDialog(self)
        if dlg.exec():
            item = dlg.get_item()
            item.source = item.source or "手动新建"
            item_repo.create(item)
            self.refresh()
            self.data_changed.emit()

    def _edit(self) -> None:
        item = self.current_item
        if not item:
            return
        dlg = ItemDialog(self, item)
        if dlg.exec():
            updated = dlg.get_item()
            item_repo.update(updated)
            self.refresh()
            self.data_changed.emit()

    def _publish(self) -> None:
        item = self.current_item
        if not item:
            return
        item_repo.set_status(item.id, STATUS_ACTIVE)
        item_repo.mark_published(item.id)
        self.refresh()
        self.data_changed.emit()

    def _archive(self) -> None:
        item = self.current_item
        if not item:
            return
        item_repo.set_status(item.id, STATUS_ARCHIVED)
        self.refresh()
        self.data_changed.emit()

    def _delete(self) -> None:
        item = self.current_item
        if not item:
            return
        if not confirm(self, "删除内容", f"确定要删除「{item.title}」吗？报名记录会一起删掉。"):
            return
        item_repo.delete(item.id)
        self.current_item = None
        self.refresh()
        self.data_changed.emit()

    def _add_signup(self) -> None:
        item = self.current_item
        if not item:
            return
        dlg = SignupDialog(self, None, item)
        if dlg.exec():
            signup_repo.add(dlg.get_signup())
            self._load_detail(item)
            self.refresh()
            self.data_changed.emit()

    def _paste_solitaire(self) -> None:
        item = self.current_item
        if not item:
            return
        dlg = PasteSolitaireDialog(self, item)
        if not dlg.exec() or not (dlg.rows or dlg.result.adjustments):
            return
        try:
            count = signup_repo.batch_add(item.id, dlg.rows)
            updated, removed = signup_repo.apply_adjustments(item.id,
                                                             dlg.result.adjustments)
        except Exception as exc:
            warn(self, "导入失败", str(exc))
            return
        bits = [f"新增 {count} 条"]
        if updated:
            bits.append(f"改单 {updated} 条")
        if removed:
            bits.append(f"取消 {removed} 条")
        self._load_detail(item)
        self.refresh()
        self.data_changed.emit()
        self.progress_label.setText("导入完成：" + "，".join(bits))

    def _del_signup(self) -> None:
        row = self.signup_table.currentRow()
        if row < 0:
            return
        cell = self.signup_table.item(row, 0)
        if not cell:
            return
        signup_repo.delete(int(cell.data(Qt.UserRole)))
        self._load_detail(self.current_item)
        self.data_changed.emit()

    def _on_signup_changed(self, cell: QTableWidgetItem) -> None:
        """报名表里勾「已结清」直接写库。"""
        if self._loading_signup or cell.column() != 4:
            return
        item = self.current_item
        signup_id = cell.data(Qt.UserRole)
        if not item or not signup_id:
            return
        signup_repo.set_settled(item.id, cell.checkState() is Qt.Checked,
                                [int(signup_id)])

    def _settle(self) -> None:
        item = self.current_item
        if not item:
            return
        dlg = SettleDialog(self, item)
        if dlg.exec():
            self._load_detail(item)
            self.refresh()
            self.data_changed.emit()

    def _duplicate(self) -> None:
        """周期性团购：把上一期整个复制成新一期，报名不带过来。"""
        item = self.current_item
        if not item:
            return
        clone = item_repo.duplicate(item.id, days_shift=7)
        if clone is None:
            warn(self, "复制失败", "找不到这条内容，可能已经被删掉了。")
            return
        dlg = ItemDialog(self, clone)
        dlg.setWindowTitle("再来一团（已复制上月内容，改改日期就能发）")
        if not dlg.exec():
            return
        new_item = dlg.get_item()
        new_item.source = new_item.source or "周期性开团"
        item_repo.create(new_item)
        self.refresh()
        self.data_changed.emit()
        self.progress_label.setText(f"已复制出《{new_item.title}》并顺延了截止日期，报名是空的")

    def _export_signups(self) -> None:
        item = self.current_item
        if not item:
            return
        rows = signup_repo.list_for(item.id or 0)
        if not rows:
            warn(self, "没有数据", "这条内容还没有报名记录。")
            return
        path = excel.export_signups(item, rows)
        from ui.widgets import info

        info(self, "导出完成", f"已保存到：\n{path}")
