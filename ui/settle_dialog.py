"""结算对话框：一条内容收尾时，谁结清了、谁还没，一屏看清。

工具不碰钱——这里只有「结清 / 未结清」的状态和份数统计，
金额由团长按群收款自己核。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.models import Item
from core.repository import signups as signup_repo
from core.utils import format_price
from exports import excel
from ui import theme
from ui.widgets import confirm, info, make_table, warn

_HEADERS = ("已结清", "昵称 / 房号", "数量", "单位", "备注", "登记时间")


class SettleDialog(QDialog):
    """勾选结清 + 汇总 + 导出催款清单。"""

    def __init__(self, parent=None, item: Item | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.rows: list = []
        self._loading = False
        self.setWindowTitle(f"结算 · {item.title if item else ''}")
        self.resize(620, 520)
        self.setStyleSheet(theme.QSS)
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "title")
        self.summary_label.setWordWrap(True)
        self.detail_label = QLabel("")
        self.detail_label.setProperty("role", "muted")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.detail_label)

        self.table = make_table(_HEADERS, stretch_col=1)
        self.table.setColumnWidth(0, 74)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 60)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.all_btn = QPushButton("全部结清")
        self.all_btn.clicked.connect(lambda: self._mark_all(True))
        self.none_btn = QPushButton("全部取消")
        self.none_btn.setProperty("variant", "ghost")
        self.none_btn.clicked.connect(lambda: self._mark_all(False))
        self.export_pending_btn = QPushButton("导出待结清")
        self.export_pending_btn.setProperty("accent", True)
        self.export_pending_btn.clicked.connect(lambda: self._export(True))
        self.export_all_btn = QPushButton("导出结算清单")
        self.export_all_btn.setProperty("variant", "ghost")
        self.export_all_btn.clicked.connect(lambda: self._export(False))
        self.close_btn = QPushButton("关闭")
        self.close_btn.setProperty("variant", "ghost")
        self.close_btn.clicked.connect(self.accept)
        for b in (self.all_btn, self.none_btn, self.export_pending_btn,
                  self.export_all_btn, self.close_btn):
            row.addWidget(b)
        layout.addLayout(row)

    # ============================ 数据 ============================

    def reload(self) -> None:
        if not self.item:
            return
        self.rows = signup_repo.list_for(self.item.id or 0)
        self._loading = True
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for s in self.rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            check = QTableWidgetItem("")
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if s.settled else Qt.Unchecked)
            check.setData(Qt.UserRole, s.id)
            check.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 0, check)
            vals = (s.name, f"{s.qty:g}", s.unit or self.item.unit or "份",
                    s.note, s.created_at)
            for col, val in enumerate(vals, start=1):
                cell = QTableWidgetItem(val)
                cell.setData(Qt.UserRole, s.id)
                if col in (2, 3):
                    cell.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, col, cell)
        self.table.blockSignals(False)
        self._loading = False
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        if not self.item:
            return
        s = signup_repo.settlement_summary(self.item.id or 0)
        unit = self.item.unit or "份"
        if not s["people"]:
            self.summary_label.setText("还没有人报名")
            self.detail_label.setText("先在管理台登记或粘贴接龙。")
            return
        self.summary_label.setText(
            f"共 {s['people']} 人 · {format_price(s['qty'])}{unit}"
        )
        self.detail_label.setText(
            f"已结清 {s['done_people']} 人（{format_price(s['done_qty'])}{unit}）"
            f"　·　待结清 {s['wait_people']} 人（{format_price(s['wait_qty'])}{unit}）"
        )

    # ============================ 操作 ============================

    def _on_item_changed(self, cell: QTableWidgetItem) -> None:
        if self._loading or cell.column() != 0 or not self.item:
            return
        signup_id = cell.data(Qt.UserRole)
        if not signup_id:
            return
        settled = cell.checkState() is Qt.Checked
        signup_repo.set_settled(self.item.id, settled, [int(signup_id)])
        # 同步内存里的快照，否则「导出待结清」用的还是打开对话框那一刻的旧数据，
        # 刚勾掉的人照样被列进催款名单 —— 群里催错人很难看。
        for s in self.rows:
            if s.id == int(signup_id):
                s.settled = settled
                break
        self._refresh_summary()

    def _mark_all(self, settled: bool) -> None:
        if not self.item:
            return
        word = "全部标记为已结清" if settled else "全部取消结清"
        if not confirm(self, "批量结算", f"确定{word}吗？（共 {len(self.rows)} 条）",
                       ok_text="确认" if settled else "确认取消"):
            return
        signup_repo.set_settled(self.item.id, settled)
        self.reload()

    def _export(self, only_unsettled: bool) -> None:
        if not self.item:
            return
        # 用库里的最新状态，别用打开对话框时的快照（勾过几条之后快照就过期了）
        rows = signup_repo.list_for(self.item.id or 0)
        if not rows:
            warn(self, "没有数据", "这条内容还没有报名记录。")
            return
        if only_unsettled and not any(not s.settled for s in rows):
            info(self, "都已结清", "大家都结清了，没有待结清名单。")
            return
        path = excel.export_settlement(self.item, rows, only_unsettled=only_unsettled)
        info(self, "导出完成", f"已保存到：\n{path}")
