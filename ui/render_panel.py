"""模块三：分发生成器。

选条目 -> 套模板 -> 出图；同时生成能直接粘进微信群的文案。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core import config
from core.models import CardTemplate, Item, kind_label
from core.repository import items as item_repo
from core.repository import publishes as publish_repo
from core.repository import signups as signup_repo
from core.repository import templates as tpl_repo
from core.utils import format_price
from render import copywriter
from render.card_renderer import CardContext
from ui import theme
from ui.widgets import confirm, hint_label, make_table, section_title, set_placeholder, warn
from ui.workers import RenderWorker

# v1.4.0：发团之后要发的四套文案（群公告/接龙/汇总都在发团之前）
_AFTERSALE_KINDS = (
    ("催报名（快截止了催一轮）", "remind"),
    ("到货通知（列领取名单）", "arrival"),
    ("催收结算（只点没结清的）", "chase"),
    ("未成团说明（给邻居一个交代）", "fail"),
)


class TemplateDialog(QDialog):
    """卡片模板编辑：颜色、是否显示价格/二维码等。"""

    def __init__(self, parent=None, template: CardTemplate | None = None) -> None:
        super().__init__(parent)
        self.tpl = template or CardTemplate()
        self.setWindowTitle("卡片模板")
        self.setStyleSheet(theme.QSS)
        self.resize(420, 420)
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        form = QHBoxLayout()

        labels_col = QVBoxLayout()
        field_col = QVBoxLayout()
        self.name_edit = QLineEdit()
        self.bg_edit = QLineEdit()
        self.accent_edit = QLineEdit()
        self.footer_edit = QLineEdit()
        set_placeholder(self.footer_edit, "卡片底部固定话术，留空则用默认")
        self.price_box = QCheckBox("显示价格")
        self.deadline_box = QCheckBox("显示截止时间")
        self.qr_box = QCheckBox("显示二维码")
        self.tags_box = QCheckBox("显示标签")

        for lab_text, widget in (
            ("模板名称", self.name_edit), ("背景色", self.bg_edit),
            ("强调色", self.accent_edit), ("底部话术", self.footer_edit),
        ):
            labels_col.addWidget(QLabel(lab_text))
            field_col.addWidget(widget)
        form.addLayout(labels_col)
        form.addLayout(field_col, 1)
        layout.addLayout(form)

        toggles = QHBoxLayout()
        for b in (self.price_box, self.deadline_box, self.qr_box, self.tags_box):
            toggles.addWidget(b)
        layout.addLayout(toggles)
        layout.addWidget(hint_label("颜色用 #RRGGBB，例如背景 #FAF7F0、强调 #D85A30"))

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Save).setText("保存")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _load(self) -> None:
        t = self.tpl
        self.name_edit.setText(t.name)
        self.bg_edit.setText(t.background)
        self.accent_edit.setText(t.accent)
        self.footer_edit.setText(t.footer)
        self.price_box.setChecked(t.show_price)
        self.deadline_box.setChecked(t.show_deadline)
        self.qr_box.setChecked(t.show_qr)
        self.tags_box.setChecked(t.show_tags)

    def get_template(self) -> CardTemplate:
        t = self.tpl
        t.name = self.name_edit.text().strip() or "未命名模板"
        t.background = self.bg_edit.text().strip() or "#FAF7F0"
        t.accent = self.accent_edit.text().strip() or "#D85A30"
        t.footer = self.footer_edit.text().strip()
        t.show_price = self.price_box.isChecked()
        t.show_deadline = self.deadline_box.isChecked()
        t.show_qr = self.qr_box.isChecked()
        t.show_tags = self.tags_box.isChecked()
        return t


class RenderPanel(QWidget):
    """选内容 -> 生成卡片图 -> 生成群文案 -> 复制到剪贴板。"""

    published = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.items: list[Item] = []
        self.templates: list[CardTemplate] = []
        self._worker: RenderWorker | None = None
        self._last_paths: list[str] = []
        self._push_after_render = False   # 一键推群：出完图自动收尾
        self._pushed: list[Item] = []
        self._build_ui()
        self.refresh_all()

    # ============================ UI ============================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(section_title("分发生成器"))

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

    def _build_left(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(QLabel("选择内容"))
        head.addStretch(1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setProperty("variant", "ghost")
        self.refresh_btn.clicked.connect(self.refresh_all)
        head.addWidget(self.refresh_btn)
        layout.addLayout(head)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.MultiSelection)
        layout.addWidget(self.list, 1)

        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.setProperty("variant", "ghost")
        self.select_all_btn.clicked.connect(self.list.selectAll)
        layout.addWidget(self.select_all_btn)

        tpl_row = QHBoxLayout()
        tpl_row.addWidget(QLabel("卡片模板"))
        self.tpl_combo = QComboBox()
        self.tpl_combo.currentIndexChanged.connect(lambda *_: None)
        self.new_tpl_btn = QPushButton("新建")
        self.new_tpl_btn.setProperty("variant", "ghost")
        self.new_tpl_btn.clicked.connect(self._new_template)
        self.edit_tpl_btn = QPushButton("编辑")
        self.edit_tpl_btn.setProperty("variant", "ghost")
        self.edit_tpl_btn.clicked.connect(self._edit_template)
        self.del_tpl_btn = QPushButton("删除")
        self.del_tpl_btn.setProperty("variant", "danger")
        self.del_tpl_btn.clicked.connect(self._del_template)
        tpl_row.addWidget(self.tpl_combo, 1)
        tpl_row.addWidget(self.new_tpl_btn)
        tpl_row.addWidget(self.edit_tpl_btn)
        tpl_row.addWidget(self.del_tpl_btn)
        layout.addLayout(tpl_row)

        self.render_btn = QPushButton("生成分享卡片")
        self.render_btn.setProperty("accent", True)
        self.render_btn.clicked.connect(self._render)
        layout.addWidget(self.render_btn)

        # v1.2.0：一键把「文案 + 卡片图 + 分发记录」三件事串起来
        self.push_btn = QPushButton("一键推群")
        self.push_btn.setProperty("accent", True)
        self.push_btn.setToolTip(
            "一次做完三件事：群公告文案进剪贴板、分享卡片出图存到本地、"
            "记一条分发记录。然后你在群里直接 Ctrl+V 粘贴就行。")
        self.push_btn.clicked.connect(self._one_click_push)
        layout.addWidget(self.push_btn)

        self.open_btn = QPushButton("打开输出文件夹")
        self.open_btn.setProperty("variant", "ghost")
        self.open_btn.clicked.connect(self._open_output)
        layout.addWidget(self.open_btn)
        return box

    def _build_right(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(QLabel("卡片预览"))
        head.addStretch(1)
        self.preview_count = hint_label("")
        head.addWidget(self.preview_count)
        layout.addLayout(head)

        self.preview_list = QListWidget()
        self.preview_list.setViewMode(QListWidget.IconMode)
        self.preview_list.setIconSize(QSize(160, 160))
        self.preview_list.setGridSize(QSize(180, 200))
        self.preview_list.setResizeMode(QListWidget.Adjust)
        self.preview_list.setMovement(QListWidget.Static)
        self.preview_list.setSpacing(8)
        self.preview_list.itemDoubleClicked.connect(self._open_image)
        layout.addWidget(self.preview_list, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_text_tab("announce"), "群公告")
        self.tabs.addTab(self._build_text_tab("solitaire"), "接龙模板")
        self.tabs.addTab(self._build_text_tab("digest"), "今日汇总")
        # v1.4.0：发团之后的四套文案共用一个标签页，用下拉切换，避免标签挤成七个
        self.tabs.addTab(self._build_text_tab("aftersale"), "售后文案")
        layout.addWidget(self.tabs, 1)
        return box

    def _build_text_tab(self, key: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 8, 6, 8)
        layout.setSpacing(8)

        # 「售后文案」页多一个类型下拉：催报名 / 到货通知 / 催收结算 / 未成团
        if key == "aftersale":
            head = QHBoxLayout()
            head.addWidget(QLabel("类型"))
            combo = QComboBox()
            for label, sub in _AFTERSALE_KINDS:
                combo.addItem(label, sub)
            combo.currentIndexChanged.connect(lambda *_: self._generate_text(key))
            head.addWidget(combo, 1)
            layout.insertLayout(0, head)
            self._aftersale_combo = combo

        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        layout.addWidget(edit, 1)

        row = QHBoxLayout()
        gen = QPushButton("生成文案")
        copy = QPushButton("复制")
        log = QPushButton("记录分发")
        gen.setProperty("variant", "ghost")
        log.setProperty("variant", "ghost")
        gen.clicked.connect(lambda: self._generate_text(key))
        copy.clicked.connect(lambda: self._copy_text(key))
        log.clicked.connect(lambda: self._log_publish(key))
        row.addWidget(gen)
        row.addWidget(copy)
        row.addWidget(log)
        row.addStretch(1)
        layout.addLayout(row)

        setattr(self, f"_{key}_edit", edit)
        return page

    # ============================ 数据 ============================

    def refresh_all(self) -> None:
        self.items = item_repo.list_items(include_archived=False)
        self.list.clear()
        for it in self.items:
            label = f"[{kind_label(it.kind)}] {it.title}"
            entry = QListWidgetItem(label)
            entry.setData(Qt.UserRole, it.id)
            self.list.addItem(entry)

        cur_text = self.tpl_combo.currentText()
        self.templates = tpl_repo.all()
        self.tpl_combo.blockSignals(True)
        self.tpl_combo.clear()
        for t in self.templates:
            self.tpl_combo.addItem(t.name, t.id)
        idx = self.tpl_combo.findText(cur_text)
        self.tpl_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.tpl_combo.blockSignals(False)

    def _selected_items(self) -> list[Item]:
        picked: set[int] = {i.data(Qt.UserRole) for i in self.list.selectedItems()}
        return [it for it in self.items if it.id in picked]

    def _current_template(self) -> CardTemplate:
        tid = self.tpl_combo.currentData()
        for t in self.templates:
            if t.id == tid:
                return t
        return CardTemplate()

    def _contexts(self, picked: list[Item]) -> dict[int, CardContext]:
        cfg = config.load_settings()
        ctxs: dict[int, CardContext] = {}
        for it in picked:
            rows = signup_repo.list_for(it.id or 0)
            ctxs[it.id or -1] = CardContext(
                community_name=cfg.get("community_name", "我们小区"),
                operator_name=cfg.get("operator_name", "团长"),
                contact_info=cfg.get("contact_info", ""),
                join_url=it.url,
                done_qty=sum(float(s.qty or 0) for s in rows),
                headcount=len(rows),
            )
        return ctxs

    # ============================ 渲染 ============================

    def _render(self) -> None:
        picked = self._selected_items()
        if not picked:
            warn(self, "没选内容", "先在左边勾选要出图的内容。")
            return
        if self._worker and self._worker.isRunning():
            return
        self.preview_list.clear()
        self.render_btn.setEnabled(False)

        self._progress = QProgressDialog("正在生成卡片…", "取消", 0, len(picked), self)
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.show()

        self._worker = RenderWorker(picked, self._current_template(),
                                    self._contexts(picked), parent=self)
        self._worker.progress.connect(self._on_render_progress)
        self._worker.one_done.connect(self._add_preview)
        self._worker.finished_all.connect(self._render_done)
        self._worker.start()

    @Slot(int, int, str)
    def _on_render_progress(self, cur: int, total: int, title: str) -> None:
        self._progress.setValue(cur)
        self._progress.setLabelText(f"正在生成 {cur}/{total}：{title[:30]}")

    @Slot(int, str)
    def _add_preview(self, item_id: int, path: str) -> None:
        pm = QPixmap(path)
        if pm.isNull():
            return
        entry = QListWidgetItem(QIcon(pm.scaled(160, 160, Qt.KeepAspectRatio,
                                                Qt.SmoothTransformation)),
                                Path(path).name)
        entry.setToolTip(path)
        entry.setData(Qt.UserRole, path)
        self.preview_list.addItem(entry)

    def _render_done(self, paths: list[str]) -> None:
        self.render_btn.setEnabled(True)
        self._progress.close()
        self.preview_count.setText(f"共 {len(paths)} 张")
        for p in paths:
            item = item_repo.get(self._item_id_of(p))
        if paths:
            self._last_paths = paths
        if getattr(self, "_push_after_render", False):
            self._push_after_render = False
            self._finish_push(paths)

    # ============================ 一键推群（v1.2.0） ============================

    def _one_click_push(self) -> None:
        """文案进剪贴板 → 出图 → 记录分发 → 打开文件夹 → 桌面通知。"""
        picked = self._selected_items()
        if not picked:
            warn(self, "没选内容", "先勾选要发群的内容。")
            return
        if self._worker and self._worker.isRunning():
            return

        # 1. 文案先落到剪贴板（同步，快），顺带让用户看见内容
        self._generate_text("announce")
        text = self._announce_edit.toPlainText().strip()
        if text:
            QApplication.clipboard().setText(text)
        self._pushed = picked

        # 2. 出图是异步的，结束后在 _finish_push 里收尾
        self._push_after_render = True
        self._render()

    def _finish_push(self, paths: list[str]) -> None:
        picked = getattr(self, "_pushed", []) or []
        for it in picked:
            try:
                publish_repo.add(it.id or 0, "wechat", text="一键推群")
                item_repo.mark_published(it.id or 0)
            except Exception:
                continue

        config.ensure_dirs()
        try:
            import os

            os.startfile(str(config.OUTPUT_DIR))  # noqa: S606
        except Exception:
            pass

        msg = f"{len(picked)} 条文案已在剪贴板，{len(paths)} 张卡片已出图"
        self.preview_count.setText(msg + "，去群里粘贴即可")
        if config.load_settings().get("notify_enabled", True):
            from core import notifier

            notifier.notify("可以发群了", msg + "（图片文件夹已打开）")
        self.published.emit()

    def _item_id_of(self, path: str) -> int:
        name = Path(path).name
        try:
            return int(name.split("_")[0])
        except ValueError:
            return 0

    def _open_image(self, entry: QListWidgetItem) -> None:
        path = entry.data(Qt.UserRole)
        if path and Path(path).exists():
            import os

            os.startfile(path)  # noqa: S606 - Windows 上打开图片很方便

    def _open_output(self) -> None:
        import os

        config.ensure_dirs()
        os.startfile(str(config.OUTPUT_DIR))  # noqa: S606

    # ============================ 文案 ============================

    def _edit_of(self, key: str) -> QPlainTextEdit:
        return getattr(self, f"_{key}_edit")

    def _generate_text(self, key: str) -> None:
        cfg = config.load_settings()
        picked = self._selected_items() or self.items[:3]
        if not picked:
            warn(self, "没有内容", "内容库里还没有条目。")
            return
        edit = self._edit_of(key)
        if key == "announce":
            chunks = [copywriter.build_announcement(
                it, cfg.get("community_name", ""), cfg.get("operator_name", ""),
                cfg.get("contact_info", "")) for it in picked]
            edit.setPlainText("\n\n".join(chunks))
        elif key == "solitaire":
            chunks = []
            for it in picked:
                rows = signup_repo.list_for(it.id or 0)
                chunks.append(copywriter.build_solitaire(it, rows))
            edit.setPlainText("\n\n".join(chunks))
        elif key == "aftersale":
            sub = self._aftersale_combo.currentData() or "remind"
            community = cfg.get("community_name", "")
            operator = cfg.get("operator_name", "")
            contact = cfg.get("contact_info", "")
            chunks = []
            for it in picked:
                rows = signup_repo.list_for(it.id or 0)
                signed = signup_repo.total_qty(it.id or 0)
                if sub == "remind":
                    chunks.append(copywriter.build_reminder(
                        it, signed, community, operator))
                elif sub == "arrival":
                    chunks.append(copywriter.build_arrival_notice(
                        it, rows, community, operator, contact))
                elif sub == "chase":
                    chunks.append(copywriter.build_settlement_chase(
                        it, rows, community, operator, contact))
                else:
                    chunks.append(copywriter.build_fail_notice(
                        it, signed, community, operator))
            edit.setPlainText("\n\n".join(chunks))
        else:
            edit.setPlainText(copywriter.build_digest(
                picked, cfg.get("community_name", "")))

    def _copy_text(self, key: str) -> None:
        text = self._edit_of(key).toPlainText().strip()
        if not text:
            warn(self, "没有文案", "先点「生成文案」。")
            return
        QApplication.clipboard().setText(text)
        self.preview_count.setText("文案已复制，去群里粘贴即可")

    def _log_publish(self, key: str) -> None:
        picked = self._selected_items()
        if not picked:
            warn(self, "没选内容", "先勾选要记录的内容。")
            return
        text = self._edit_of(key).toPlainText().strip()
        for it in picked:
            publish_repo.add(it.id, key, image_path="", text=text[:500])
            item_repo.mark_published(it.id)
        self.published.emit()

    # ============================ 模板 ============================

    def _new_template(self) -> None:
        dlg = TemplateDialog(self)
        if dlg.exec():
            tpl_repo.create(dlg.get_template())
            self.refresh_all()

    def _edit_template(self) -> None:
        tpl = self._current_template()
        if not tpl.id:
            return
        dlg = TemplateDialog(self, tpl)
        if dlg.exec():
            tpl_repo.update(dlg.get_template())
            self.refresh_all()

    def _del_template(self) -> None:
        tpl = self._current_template()
        if not tpl.id:
            return
        if len(self.templates) <= 1:
            warn(self, "别删了", "至少保留一个模板。")
            return
        if confirm(self, "删除模板", f"确定删除模板「{tpl.name}」？"):
            tpl_repo.delete(tpl.id)
            self.refresh_all()
