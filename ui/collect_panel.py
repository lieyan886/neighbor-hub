"""模块一：信息采集台。

负责把外部世界的零碎信息（链接、拼团文本、Excel）变成库里的草稿条目。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from collectors import bulk_import
from collectors.filters import ItemFilter
from collectors.link_parser import ScrapeResult, convert_to_item, extract_first_url
from core import config
from core.models import KIND_LABELS, KIND_ORDER, Item, kind_label
from core.repository import items as item_repo
from ui import theme
from ui.widgets import hint_label, make_table, section_title, set_placeholder, warn
from ui.workers import ScrapeWorker

_HEADERS = ("", "标题", "类型", "价格", "截止", "来源", "说明")


class CollectPanel(QWidget):
    """采集台：抓链接、粘文本、导 Excel，过滤后批量入库。"""

    items_imported = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.candidates: list[Item] = []
        self._worker: ScrapeWorker | None = None
        self._build_ui()

    # ============================ UI ============================

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)
        root.addWidget(section_title("信息采集台"))
        root.addWidget(hint_label(
            "粘贴优惠链接自动抓标题价格和配图；也可以整段粘贴群里看到的清单，或从 Excel 导入。"
        ))

        # —— 第一部分：链接抓取 ——
        link_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        set_placeholder(self.url_edit, "粘贴商品 / 活动链接，多个链接用空格或换行分隔")
        self.scrape_btn = QPushButton("抓取")
        self.scrape_btn.setProperty("accent", True)
        self.scrape_btn.clicked.connect(self._start_scrape)
        self.clip_btn = QPushButton("读剪贴板")
        self.clip_btn.setProperty("variant", "ghost")
        self.clip_btn.clicked.connect(self._read_clipboard)
        link_row.addWidget(self.url_edit, 1)
        link_row.addWidget(self.scrape_btn)
        link_row.addWidget(self.clip_btn)
        root.addLayout(link_row)

        # —— 第二部分：批量文本 / Excel ——
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "一行一条，随便写：\n"
            "山姆牛排 3斤装 168元 截止10月5日\n"
            "周六亲子观影｜10月7日 15:00\n"
            "也可以直接把 Excel 表格整块粘进来（自动认表头：标题/价格/截止/标签）"
        )
        self.text_edit.setFixedHeight(110)
        root.addWidget(self.text_edit)

        file_row = QHBoxLayout()
        self.parse_btn = QPushButton("解析文本")
        self.parse_btn.clicked.connect(self._parse_text)
        self.excel_btn = QPushButton("导入 Excel…")
        self.excel_btn.setProperty("variant", "ghost")
        self.excel_btn.clicked.connect(self._import_excel)
        self.clear_btn = QPushButton("清空候选")
        self.clear_btn.setProperty("variant", "ghost")
        self.clear_btn.clicked.connect(self._clear_candidates)
        file_row.addWidget(self.parse_btn)
        file_row.addWidget(self.excel_btn)
        file_row.addWidget(self.clear_btn)
        file_row.addStretch(1)
        root.addLayout(file_row)

        # —— 过滤条件 ——
        filter_row = QHBoxLayout()
        cfg = config.load_settings()
        self.black_edit = QLineEdit(",".join(cfg.get("blacklist", [])))
        set_placeholder(self.black_edit, "黑名单关键词，逗号分隔")
        self.white_edit = QLineEdit(",".join(cfg.get("whitelist", [])))
        set_placeholder(self.white_edit, "白名单（留空表示不过滤）")
        self.dedupe_box = QCheckBox("去重")
        self.dedupe_box.setChecked(True)
        filter_row.addWidget(QLabel("过滤"))
        filter_row.addWidget(self.black_edit, 1)
        filter_row.addWidget(self.white_edit, 1)
        filter_row.addWidget(self.dedupe_box)
        self.save_filter_btn = QPushButton("记住")
        self.save_filter_btn.setProperty("variant", "ghost")
        self.save_filter_btn.clicked.connect(self._save_filters)
        filter_row.addWidget(self.save_filter_btn)
        root.addLayout(filter_row)

        # —— 候选表 ——
        self.table = make_table(_HEADERS, stretch_col=1)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 110)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        # —— 底部操作 ——
        bottom = QHBoxLayout()
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.setProperty("variant", "ghost")
        self.select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        self.unselect_btn = QPushButton("取消全选")
        self.unselect_btn.setProperty("variant", "ghost")
        self.unselect_btn.clicked.connect(lambda: self._set_all_checked(False))
        self.commit_btn = QPushButton("入库选中条目")
        self.commit_btn.setProperty("accent", True)
        self.commit_btn.clicked.connect(self._commit)
        bottom.addWidget(self.select_all_btn)
        bottom.addWidget(self.unselect_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.commit_btn)
        root.addLayout(bottom)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        root.addWidget(self.progress)

        self.status_label = hint_label("")
        root.addWidget(self.status_label)

    # ============================ 抓取 ============================

    def _collect_urls(self) -> list[str]:
        text = self.url_edit.text()
        urls = [u.strip() for u in text.replace("\n", " ").split()]
        extra = self.text_edit.toPlainText()
        if extra.strip():
            urls += [u for u in extra.replace("\n", " ").split()
                     if u.startswith(("http://", "https://"))]
        seen, out = set(), []
        for u in urls:
            if u.startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _start_scrape(self) -> None:
        urls = self._collect_urls()
        if not urls:
            warn(self, "没有链接", "先在输入框里粘贴至少一个 http(s) 链接。")
            return
        if self._worker and self._worker.isRunning():
            return
        self.scrape_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(urls))
        self.progress.setValue(0)

        self._worker = ScrapeWorker(
            urls, download_cover=bool(config.load_settings().get("auto_download_cover", True)),
            parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        if hasattr(self._worker, "note"):
            self._worker.note.connect(self.status_label.setText)
        self._worker.one_done.connect(self._append_result)
        self._worker.finished_all.connect(self._scrape_done)
        self._worker.start()

    def _on_progress(self, cur: int, total: int, url: str) -> None:
        self.progress.setValue(cur)
        self.status_label.setText(f"正在抓取 {cur}/{total}：{url[:60]}…")

    def _scrape_done(self, results: list[ScrapeResult]) -> None:
        self.scrape_btn.setEnabled(True)
        self.progress.setVisible(False)
        ok = sum(1 for r in results if r.ok)
        self.status_label.setText(
            f"抓取完成：成功 {ok} 条，失败 {len(results) - ok} 条；候选共 {self.table.rowCount()} 行"
        )

    def _read_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication

        text = QApplication.clipboard().text()
        url = extract_first_url(text)
        if url:
            current = self.url_edit.text().strip()
            self.url_edit.setText(f"{current} {url}".strip())
        elif text.strip():
            self.text_edit.setPlainText(text.strip())
        else:
            warn(self, "剪贴板是空的", "先去复制一段链接或清单文本。")

    # ============================ 解析 ============================

    def _parse_text(self) -> None:
        text = self.text_edit.toPlainText()
        if not text.strip():
            warn(self, "没有内容", "把要导入的清单粘到上面的文本框里。")
            return
        parsed = bulk_import.parse_table(text)
        if len(parsed) <= 1 or all(len(p.summary) == 0 for p in parsed) and len(text.splitlines()) > 1 and "\t" not in text:
            parsed = bulk_import.merge_items(parsed, bulk_import.parse_lines(text))
        added = self._add_candidates(parsed)
        self.status_label.setText(f"解析到 {added} 条候选（已自动去重）")

    def _import_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Excel 文件", "", "Excel 文件 (*.xlsx *.xlsm)"
        )
        if not path:
            return
        try:
            parsed = bulk_import.import_excel(path)
        except Exception as exc:
            warn(self, "导入失败", f"读不了这个文件：{exc}")
            return
        added = self._add_candidates(parsed)
        self.status_label.setText(
            f"从 {Path(path).name} 解析到 {added} 条候选" if added else "文件里没解析出可用行"
        )

    def _add_candidates(self, parsed: list[Item]) -> int:
        if not parsed:
            return 0
        merged = bulk_import.merge_items(self.candidates, parsed)
        new_items = [it for it in merged if it not in self.candidates]
        for it in new_items:
            self.candidates.append(it)
            self._append_row(it)
        return len(new_items)

    def _append_result(self, res: ScrapeResult) -> None:
        if not res.ok:
            self.status_label.setText(f"抓取失败：{res.url[:50]} — {res.error}")
            return
        item = convert_to_item(res)
        self.candidates.append(item)
        self._append_row(item)

    def _append_row(self, item: Item) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        chk = QTableWidgetItem("")
        chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        chk.setCheckState(Qt.Checked)
        self.table.setItem(row, 0, chk)

        price = f"¥{item.price:g}" if item.price is not None else "-"
        cols = (
            "", item.title, kind_label(item.kind), price,
            item.deadline or item.event_at or "-", item.source or "-",
            item.summary[:40] or "-",
        )
        for col, val in enumerate(cols):
            if col == 0:
                continue
            cell = QTableWidgetItem(val)
            if col == 2:
                cell.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, cell)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell:
                cell.setCheckState(state)

    def _clear_candidates(self) -> None:
        self.candidates.clear()
        self.table.setRowCount(0)
        self.status_label.setText("已清空候选列表")

    def _checked_items(self) -> list[Item]:
        picked: list[Item] = []
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell and cell.checkState() == Qt.Checked and r < len(self.candidates):
                picked.append(self.candidates[r])
        return picked

    # ============================ 入库 ============================

    def _commit(self) -> None:
        picked = self._checked_items()
        if not picked:
            warn(self, "没有选中条目", "至少勾选一行再入库。")
            return
        flt = ItemFilter(
            blacklist=self.black_edit.text().replace("，", ",").split(","),
            whitelist=self.white_edit.text().replace("，", ",").split(","),
            dedupe=self.dedupe_box.isChecked(),
        )
        result = flt.apply(picked)
        if not result.kept:
            warn(self, "一条都没留下", "\n".join(f"{i.title}：{why}" for i, why in result.dropped[:8]))
            return
        # 勾了「去重」就要真的去重：以前这里写死 dedupe=False，
        # 每周抓同一批链接照样重复入库，库里一堆同名条目。
        try:
            inserted, skipped = item_repo.bulk_insert(
                result.kept, dedupe=self.dedupe_box.isChecked())
        except Exception as exc:
            warn(self, "入库失败", str(exc))
            return

        # 被去重跳过的条目要从候选里撤掉，否则还留在表里等下次再点一次
        for it in result.kept:
            self._remove_candidate(it)
        msg = f"已入库 {inserted} 条"
        if skipped:
            msg += f"，去重跳过 {skipped} 条"
        if result.dropped:
            msg += f"，过滤掉 {len(result.dropped)} 条（" + "、".join(
                sorted({why for _, why in result.dropped})[:3]
            ) + "）"
        self.status_label.setText(msg)
        self.items_imported.emit(inserted)

    def _remove_candidate(self, item: Item) -> None:
        if item in self.candidates:
            idx = self.candidates.index(item)
            self.candidates.pop(idx)
            self.table.removeRow(idx)

    def _save_filters(self) -> None:
        config.save_settings({
            "blacklist": [w.strip() for w in self.black_edit.text().replace("，", ",").split(",") if w.strip()],
            "whitelist": [w.strip() for w in self.white_edit.text().replace("，", ",").split(",") if w.strip()],
        })
        self.status_label.setText("过滤词已保存，下次启动仍生效")
