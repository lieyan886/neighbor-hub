"""v1.5.0：报名查重归并对话框。

群里同一个人会有好几种写法（「张三」「3栋张三」「张三妈妈」），
以前按名字精确匹配就会被记成几个人，份数散开、结算时对不上人。
这里把疑似同人的分组列出来，让用户自己确认要不要合并、保留哪个名字。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from core.models import Item, Signup
from core.repository import signups as signup_repo
from core import utils
from ui import theme
from ui.widgets import EmptyState, confirm, hint_label, info, section_title


class _GroupBox(QWidget):
    """一个疑似同人的分组：单选保留哪个名字 + 显示各自的份数。"""

    def __init__(self, group: list[Signup], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.group = group
        self.buttons: list[tuple[QRadioButton, Signup]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        core_name = utils.normalize_name(group[0].name)
        total = sum(float(s.qty or 0) for s in group)
        layout.addWidget(hint_label(
            f"核心名「{core_name}」· {len(group)} 条记录 · 合并后共 {total:g} 份"))

        for idx, s in enumerate(group):
            row = QHBoxLayout()
            rb = QRadioButton(f"{s.name}（{float(s.qty or 0):g} 份"
                              + ("，已结清" if s.settled else "") + "）")
            rb.setChecked(idx == 0)
            row.addWidget(rb)
            row.addStretch(1)
            layout.addLayout(row)
            self.buttons.append((rb, s))

    def chosen(self) -> Signup | None:
        for rb, s in self.buttons:
            if rb.isChecked():
                return s
        return None


class MergeDialog(QDialog):
    """列出所有疑似重复分组，批量合并。"""

    def __init__(self, parent: QWidget | None, item: Item) -> None:
        super().__init__(parent)
        self.item = item
        self.merged = 0
        self.setWindowTitle(f"查重归并 · {item.title}")
        self.resize(520, 460)
        self.setStyleSheet(theme.QSS)
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(section_title("疑似同一个人的报名"))
        layout.addWidget(hint_label(
            "同一条内容里，昵称去掉房号/亲属后缀后相同的会被归为一组。"
            "确认是同一个人再合并，份数会相加。"))

        self.body = QVBoxLayout()
        layout.addLayout(self.body, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.merge_btn = QPushButton("合并所选分组")
        self.merge_btn.clicked.connect(self._merge)
        close_btn = QPushButton("关闭")
        close_btn.setProperty("variant", "ghost")
        close_btn.clicked.connect(self.accept)
        foot.addWidget(close_btn)
        foot.addWidget(self.merge_btn)
        layout.addLayout(foot)

    def _load(self) -> None:
        # 清空旧的分组控件
        while self.body.count():
            it = self.body.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        groups = signup_repo.duplicate_groups(self.item.id or 0)
        if not groups:
            self.body.addWidget(EmptyState("没有发现疑似重复的报名", "登记得很干净。"))
            self.merge_btn.setEnabled(False)
            return
        self.merge_btn.setEnabled(True)
        self.boxes: list[_GroupBox] = []
        for g in groups:
            box = _GroupBox(g)
            self.boxes.append(box)
            self.body.addWidget(box)
        self.body.addStretch(1)

    def _merge(self) -> None:
        if not getattr(self, "boxes", None):
            return
        if not confirm(self, "确认合并",
                       "合并后份数相加、结清状态取「都结清才算结清」，"
                       "被合并的记录会删除。继续？"):
            return
        done = 0
        for box in self.boxes:
            keep = box.chosen()
            if keep is None or keep.id is None:
                continue
            others = [s.id for s in box.group
                      if s.id is not None and s.id != keep.id]
            if not others:
                continue
            signup_repo.merge_signups(keep.id, others)  # type: ignore[arg-type]
            done += 1
        self.merged = done
        info(self, "合并完成", f"已合并 {done} 组。")
        self._load()
