"""产出层：卡片渲染、文案生成、Excel 导出。"""
from __future__ import annotations

from pathlib import Path

from core import config
from core.repository import items as item_repo
from core.repository import signups as signup_repo
from core.repository import templates as tpl_repo
from exports import excel
from render import copywriter
from render.card_renderer import CardContext, render_card


def _groupbuy():
    return [i for i in item_repo.list_items(include_archived=True)
            if i.source_hash == "test-groupbuy"][0]


def test_render_card(seed):
    tpl = tpl_repo.all()[0]
    ctx = CardContext(community_name="测试小区", operator_name="团长",
                      contact_info="微信 test", done_qty=7, headcount=3)
    path = render_card(_groupbuy(), tpl, ctx, config.OUTPUT_DIR)
    assert Path(path).stat().st_size > 2000, "卡片图太小"


def test_copywriter_all_kinds(seed):
    items_all = item_repo.list_items(include_archived=True)
    ann = copywriter.build_announcement(items_all[0], "测试小区", "团长", "微信 test")
    sol = copywriter.build_solitaire(items_all[0])
    dig = copywriter.build_digest(items_all, "测试小区")
    assert ann and "接龙" in sol and "测试小区" in dig


def test_excel_exports(seed):
    items_all = item_repo.list_items(include_archived=True)
    gb = _groupbuy()
    signups = signup_repo.list_for(gb.id)
    p1 = excel.export_items(items_all, config.EXPORT_DIR)
    p2 = excel.export_signups(gb, signups, config.EXPORT_DIR)
    p3 = excel.export_groupbuy_packing([gb], {gb.id: signups}, config.EXPORT_DIR)
    for p in (p1, p2, p3):
        assert Path(p).exists(), f"导出文件不存在：{p}"
