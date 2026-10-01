"""把 matplotlib 图渲染成 QPixmap，供看板面板直接贴到 QLabel。"""
from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")  # 必须在 pyplot 之前，且不用交互式后端

from matplotlib import font_manager, pyplot as plt  # noqa: E402

from ui import theme

_FONT_READY = False


def _prepare_fonts() -> None:
    """让图表能显示中文，找不到雅黑就退回系统默认（方框总比报错好）。"""
    global _FONT_READY
    if _FONT_READY:
        return
    candidates = ["Microsoft YaHei", "微软雅黑", "SimHei", "PingFang SC", "Noto Sans CJK SC"]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    hit = [c for c in candidates if c in installed]
    if hit:
        plt.rcParams["font.sans-serif"] = hit + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    _FONT_READY = True


def _to_pixmap(fig) -> "object":
    from PySide6.QtGui import QPixmap

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor(),
                bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return pm


def _style_axes(ax, fig, title: str = "") -> None:
    fig.patch.set_facecolor(theme.SURFACE)
    ax.set_facecolor(theme.SURFACE)
    for spine in ax.spines.values():
        spine.set_color(theme.BORDER)
    ax.tick_params(colors=theme.TEXT_MUTED, labelsize=9)
    if title:
        ax.set_title(title, color=theme.TEXT, fontsize=11, pad=10)
    try:
        ax.title.set_fontname("Microsoft YaHei")
    except Exception:
        pass


def bar_chart(labels: list[str], values: list[float], title: str = "",
              color: str | None = None) -> "object":
    """柱状图：各类型分布 / 各条目人次。"""
    _prepare_fonts()
    if not labels:
        labels, values = ["暂无数据"], [0]
    color = color or theme.ACCENT
    fig, ax = plt.subplots(figsize=(5.2, 2.8))
    bars = ax.bar(labels, values, color=color, width=0.55, linewidth=0)
    for b, v in zip(bars, values):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                f"{v:g}", ha="center", va="bottom", color=theme.TEXT_MUTED, fontsize=9)
    ax.set_ylim(0, max(values + [1]) * 1.25)
    ax.tick_params(axis="x", labelrotation=0)
    _style_axes(ax, fig, title)
    return _to_pixmap(fig)


def pie_chart(labels: list[str], values: list[float], title: str = "") -> "object":
    """饼图：内容类型占比。"""
    _prepare_fonts()
    palette = [theme.ACCENT, theme.TEAL, theme.BLUE, theme.PURPLE, theme.AMBER]
    if not labels or sum(values) == 0:
        labels, values = ["暂无数据"], [1]
        palette = [theme.BORDER]
    fig, ax = plt.subplots(figsize=(4.0, 2.8))
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct="%1.0f%%",
        colors=palette[: len(labels)], startangle=100,
        wedgeprops={"width": 0.45, "edgecolor": theme.SURFACE, "linewidth": 1.6},
        textprops={"color": theme.TEXT, "fontsize": 9},
    )
    for t in autotexts:
        t.set_color(theme.TEXT_MUTED)
        t.set_fontsize(9)
    _style_axes(ax, fig, title)
    return _to_pixmap(fig)


def line_chart(labels: list[str], values: list[float], title: str = "") -> "object":
    """折线图：月度发布趋势。"""
    _prepare_fonts()
    if not labels:
        labels, values = ["暂无"], [0]
    fig, ax = plt.subplots(figsize=(5.2, 2.8))
    ax.plot(labels, values, color=theme.ACCENT, linewidth=2, marker="o",
            markersize=5, markerfacecolor=theme.SURFACE, markeredgecolor=theme.ACCENT,
            markeredgewidth=1.6)
    ax.fill_between(range(len(values)), values, color=theme.ACCENT, alpha=0.14)
    ax.set_ylim(0, max(values + [1]) * 1.3)
    _style_axes(ax, fig, title)
    return _to_pixmap(fig)


def horizontal_bar(labels: list[str], values: list[float], title: str = "") -> "object":
    """横向条形图：条目热度排行（标题长时用这个不打架）。"""
    _prepare_fonts()
    if not labels:
        labels, values = ["暂无数据"], [0]
    labels = [(l[:12] + "…") if len(l) > 13 else l for l in labels]
    fig, ax = plt.subplots(figsize=(5.2, 2.8))
    y = range(len(labels))
    ax.barh(list(y), values, color=theme.TEAL, height=0.5)
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=9, color=theme.TEXT_MUTED)
    ax.invert_yaxis()
    _style_axes(ax, fig, title)
    return _to_pixmap(fig)
