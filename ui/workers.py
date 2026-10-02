"""后台线程：抓网页、渲染图片这类慢活都放这里，避免卡住界面。"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from core.models import CardTemplate, Item
from collectors.link_parser import ScrapeResult, scrape_url
from render.card_renderer import CardContext, render_card


class ScrapeWorker(QThread):
    """逐个抓取链接，实时汇报进度。"""

    progress = Signal(int, int, str)      # 当前序号, 总数, 正在抓的 URL
    one_done = Signal(object)             # 单个 ScrapeResult
    finished_all = Signal(list)
    note = Signal(str)                    # 文字提示（浏览器兜底时的阶段说明）

    def __init__(self, urls: list[str], download_cover: bool = True,
                 parent=None) -> None:
        super().__init__(parent)
        self.urls = [u for u in urls if u]
        self.download_cover = download_cover
        self.results: list[ScrapeResult] = []
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # pragma: no cover - 线程体
        total = len(self.urls)
        for idx, url in enumerate(self.urls, 1):
            if self._cancelled:
                break
            self.progress.emit(idx, total, url)
            try:
                res = scrape_url(
                    url,
                    download_cover=self.download_cover,
                    progress=self.note.emit,
                )
            except Exception as exc:  # 单个失败不影响整批
                res = ScrapeResult(url=url, error=str(exc))
            self.results.append(res)
            self.one_done.emit(res)
        self.finished_all.emit(self.results)


class WatchWorker(QThread):
    """v1.2.0：逐个重抓监控源，实时汇报「正在看第几个」。"""

    progress = Signal(str)
    one_done = Signal(object)          # 单个 WatchChange（含无变化的）
    finished_all = Signal(list)        # 只有真正有变化/出错的那些

    def __init__(self, sources: list, parent=None) -> None:
        super().__init__(parent)
        self.sources = list(sources)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # pragma: no cover - 线程体
        from core import config
        from core.repository import watch_sources as repo
        from core.watcher import WatchService, check_source

        svc = WatchService()
        auto_import = bool(config.load_settings().get("watch_auto_import", False))
        hits = []
        total = len(self.sources)
        for idx, src in enumerate(self.sources, 1):
            if self._cancelled:
                break
            self.progress.emit(f"正在检查第 {idx}/{total} 个：{src.title or src.url}")
            try:
                change = check_source(src)
            except Exception as exc:      # 单个站点失败不影响整批
                from core.watcher import WatchChange
                change = WatchChange(source=src, error=str(exc))
            self.one_done.emit(change)
            if change.has_change and not change.error:
                svc.apply(change)
                hits.append(change)
        self.finished_all.emit(hits)


class RenderWorker(QThread):
    """批量渲染分享卡片。"""

    progress = Signal(int, int, str)
    one_done = Signal(int, str)           # item_id, 图片路径
    finished_all = Signal(list)

    def __init__(self, items: list[Item], template: CardTemplate,
                 contexts: dict[int, CardContext] | None = None,
                 output_dir: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.items = items
        self.template = template
        self.contexts = contexts or {}
        self.output_dir = output_dir
        self.paths: list[str] = []

    def run(self) -> None:  # pragma: no cover - 线程体
        total = len(self.items)
        for idx, it in enumerate(self.items, 1):
            self.progress.emit(idx, total, it.title)
            try:
                path = render_card(it, self.template,
                                   self.contexts.get(it.id or -1), self.output_dir)
            except Exception:
                continue
            self.paths.append(path)
            self.one_done.emit(it.id or -1, path)
        self.finished_all.emit(self.paths)
