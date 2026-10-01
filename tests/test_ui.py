"""界面层：图表能出图、主窗口能建起来并切换页面。"""
from __future__ import annotations

from ui import charts


def test_charts_render(qapp):
    for pm in (
        charts.pie_chart(["活动", "拼单", "优惠"], [3, 5, 2]),
        charts.line_chart(["01", "02", "03"], [1, 4, 2]),
        charts.bar_chart(["活动", "拼单"], [3, 5]),
    ):
        assert not pm.isNull(), "图表渲染成空图"


def test_main_window_build(qapp):
    from ui.main_window import MainWindow
    from ui.theme import apply as apply_theme

    apply_theme(qapp)
    win = MainWindow()
    win.show()
    for page in ("manage", "render", "stats"):
        win._goto(page)
    win._refresh_summary()
    assert "邻里圈" in win.windowTitle()
    win.close()
