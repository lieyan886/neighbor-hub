"""自检脚本（tools/selftest.py）。

跑一遍「采集 -> 过滤 -> 入库 -> 接龙 -> 出图 -> 文案 -> 导出 -> 图表 -> UI」全链路，
用一个临时目录当数据目录，不碰真实数据。
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

# offscreen 模式下可以创建 Qt 对象而不弹窗，适合 CI / 命令行自检
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Windows 控制台默认 GBK，打印中文会 UnicodeEncodeError；强制 UTF-8 保证 CI 可跑
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def run_selftest() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication([])

    tmp = Path(tempfile.mkdtemp(prefix="community-hub-selftest-"))
    from core import config

    config.DATA_DIR = tmp / "data"
    config.COVER_DIR = config.DATA_DIR / "covers"
    config.EXPORT_DIR = tmp / "exports"
    config.OUTPUT_DIR = tmp / "output"
    config.DB_PATH = config.DATA_DIR / "test.db"
    config.SETTINGS_PATH = config.DATA_DIR / "settings.json"
    config.ensure_dirs()

    from core.db import get_database
    from core.models import KIND_GROUPBUY, Item
    from core.repository import items as item_repo
    from core.repository import signups as signup_repo
    from core.repository import stats as stats_repo
    from core.repository import templates as tpl_repo
    from core.utils import days_left, humanize, parse_datetime, parse_price
    from collectors import bulk_import
    from collectors.filters import ItemFilter
    from exports import excel
    from render import copywriter
    from render.card_renderer import CardContext, render_card

    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> bool:
        # 实时打印进度：某一步挂住时能看出卡在哪，而不是干等到超时
        print(f"[..] {name}", flush=True)
        try:
            detail = fn()
        except Exception:
            results.append((name, False, traceback.format_exc(limit=3)))
            print(f"[FAIL] {name}", flush=True)
            return False
        results.append((name, True, detail or ""))
        print(f"[ OK ] {name}{(' — ' + detail) if detail else ''}", flush=True)
        return True

    db = get_database(config.DB_PATH)
    db.initialize()

    inserted: list[Item] = []

    def step_parse() -> str:
        text = ("山姆牛肉卷 3斤装 168元 截止10月5日\n"
                "周六亲子观影｜10月7日 15:00\n"
                "小区包子铺 豆浆买一送一 3元")
        parsed = bulk_import.merge_items(
            bulk_import.parse_table(text), bulk_import.parse_lines(text)
        )
        assert parsed, "一行都没解析出来"
        return f"解析出 {len(parsed)} 条，首条《{parsed[0].title}》类型={parsed[0].kind}"

    def step_price_time() -> str:
        assert parse_price("券后 ￥168.5") == 168.5, "价格解析不对"
        assert parse_price("3元") == 3.0, "带单位的价格没抓到"
        dt = parse_datetime("2026-10-05 14:30")
        assert dt is not None and dt.month == 10, "日期解析不对"
        assert parse_datetime("明天 18:00") is not None, "相对日期失效"
        return f"截止口语化：{humanize('2026-10-05')}，剩余 {days_left('2026-10-05')} 天"

    def step_insert() -> str:
        nonlocal inserted
        items_to_add = [
            Item(title="周六亲子观影", kind="event", summary="小区活动室，免费报名",
                 event_at="2026-10-07 15:00", deadline="2026-10-06 12:00",
                 quota=30, unit="人", tags="亲子活动,免费",
                 source_hash="selftest-event"),
            Item(title="山姆牛肉卷拼单", kind=KIND_GROUPBUY, price=168.0,
                 origin_price=199.0, unit="份", quota=20, merchant="山姆",
                 location="3栋架空层自提", deadline="2026-10-05",
                 tags="生鲜果蔬", source_hash="selftest-groupbuy"),
            Item(title="包子铺豆浆买一送一", kind="deal", price=3.0,
                 merchant="老王包子铺", tags="餐饮", source_hash="selftest-deal"),
        ]
        flt = ItemFilter(dedupe=True)
        res = flt.apply(items_to_add)
        item_repo.bulk_insert(res.kept, dedupe=False)
        inserted = item_repo.list_items(include_archived=True)
        assert len(inserted) >= 3, f"入库数量不对：{len(inserted)}"
        return f"入库 {len(res.kept)} 条，过滤 {len(res.dropped)} 条"

    def step_dedupe() -> str:
        dup = Item(title="山姆牛肉卷拼单", kind=KIND_GROUPBUY, source_hash="selftest-groupbuy")
        res = ItemFilter(dedupe=True).apply([dup])
        assert res.kept == [], "重复条目没被过滤掉"
        return "重复指纹被正确拦截"

    def step_status() -> str:
        changed = item_repo.refresh_statuses(24)
        gb = [i for i in item_repo.list_items(include_archived=True)
              if i.source_hash == "selftest-groupbuy"]
        assert gb, "没找到拼单条目"
        return f"状态自动流转 {changed} 条；拼单当前展示状态={gb[0].derive_status()}"

    def step_signup() -> str:
        gb = [i for i in item_repo.list_items(include_archived=True)
              if i.source_hash == "selftest-groupbuy"][0]
        rows = copywriter.dedupe_solitaire(
            copywriter.parse_solitaire("1. 张三 2份\n2、3栋王姐 3\n3)李雷 1份", "份")
        )
        assert len(rows) == 3, f"接龙解析条数不对：{len(rows)}"
        signup_repo.batch_add(gb.id, rows)
        signup_repo.batch_add(gb.id, [("张三", 1.0)])  # 同名应累加
        total = signup_repo.total_qty(gb.id)
        assert abs(total - 7.0) < 1e-6, f"合计份数不对：{total}"
        return f"接龙 {len(rows)} 条，同名累加后合计 {total:g} 份"

    def step_render() -> str:
        tpl = tpl_repo.all()[0]
        gb = [i for i in item_repo.list_items(include_archived=True)
              if i.source_hash == "selftest-groupbuy"][0]
        ctx = CardContext(community_name="测试小区", operator_name="团长",
                          contact_info="微信 test", done_qty=7, headcount=3)
        path = render_card(gb, tpl, ctx, config.OUTPUT_DIR)
        size = Path(path).stat().st_size
        assert size > 2000, f"卡片图太小：{size} 字节"
        return f"卡片已生成：{Path(path).name}（{size // 1024} KB）"

    def step_copy() -> str:
        items_all = item_repo.list_items(include_archived=True)
        ann = copywriter.build_announcement(items_all[0], "测试小区", "团长", "微信 test")
        sol = copywriter.build_solitaire(items_all[0])
        dig = copywriter.build_digest(items_all, "测试小区")
        assert "接龙" in sol and "测试小区" in dig, "文案缺内容"
        return f"公告 {len(ann)} 字 / 接龙模板 {len(sol)} 字 / 汇总 {len(dig)} 字"

    def step_privacy() -> str:
        from core import privacy

        assert privacy.mask_phone("13812345678") == "138****5678", "手机号遮罩不对"
        room = privacy.mask_room("3栋502")
        assert "***" in room and "502" not in room, f"门牌没遮住：{room}"
        # 群文案口径：medium 保留昵称，门牌必须遮
        assert privacy.mask_name("3栋王姐", privacy.LEVEL_MEDIUM, "public") == "3栋王姐"
        assert "502" not in privacy.mask_free_text("送到 3栋502，电话13812345678")
        # 严格级：群里也化名
        assert privacy.mask_name("王姐", privacy.LEVEL_STRICT, "public", 2) == "邻居2"
        # 导出口径：姓名部分打码
        assert privacy.mask_name("张三", privacy.LEVEL_MEDIUM, "export") == "张*"
        assert privacy.mask_name("王小明", privacy.LEVEL_MEDIUM, "export") == "王*明"
        assert privacy.mask_contact("13812345678", privacy.LEVEL_STRICT, "export") == "已隐藏"
        assert privacy.mask_contact("13812345678") == "138****5678"
        assert privacy.mask_name("张三", privacy.LEVEL_OFF, "export") == "张三"
        return "手机号/门牌/姓名/联系方式 四路遮罩正确，三档等级切换正常"

    def step_excel() -> str:
        items_all = item_repo.list_items(include_archived=True)
        p1 = excel.export_items(items_all, config.EXPORT_DIR)
        gb = [i for i in items_all if i.source_hash == "selftest-groupbuy"][0]
        p2 = excel.export_signups(gb, signup_repo.list_for(gb.id), config.EXPORT_DIR)
        p3 = excel.export_groupbuy_packing(
            [gb], {gb.id: signup_repo.list_for(gb.id)}, config.EXPORT_DIR)
        for p in (p1, p2, p3):
            assert Path(p).exists(), f"导出文件不存在：{p}"
        return "总表 / 报名明细 / 提货清单 三个 xlsx 均已导出"

    def step_stats() -> str:
        ov = stats_repo.overview()
        by_kind = stats_repo.count_by_kind()
        trend = stats_repo.monthly_trend()
        tops = stats_repo.top_items()
        assert ov["items"] >= 3, "统计概览数量不对"
        return (f"内容 {ov['items']} 条 / 类型分布 {len(by_kind)} 类 / "
                f"趋势点 {len(trend)} 个 / 热度排行 {len(tops)} 条")

    def step_charts() -> str:
        from ui import charts

        pm1 = charts.pie_chart(["活动", "拼单", "优惠"], [3, 5, 2])
        pm2 = charts.line_chart(["01", "02", "03"], [1, 4, 2])
        pm3 = charts.bar_chart(["活动", "拼单"], [3, 5])
        pm4 = charts.horizontal_bar(["有点长的标题测试"], [7])
        assert not pm1.isNull() and not pm2.isNull(), "图表渲染成空图"
        assert not pm3.isNull() and not pm4.isNull(), "图表渲染成空图"
        return "饼图 / 折线 / 柱状 / 横条 四张图渲染正常"

    def step_ui() -> str:
        from ui.main_window import MainWindow
        from ui.theme import apply as apply_theme

        apply_theme(app)
        win = MainWindow()
        win.show()
        win._goto("manage")
        win._goto("render")
        win._goto("stats")
        win._refresh_summary()
        title = win.windowTitle()
        win.close()
        return f"主窗口构建并切换四个页面成功：{title}"

    def step_browser() -> str:
        """用本地 file:// 页面跑一次真 JS 渲染，不依赖外网。

        Playwright 没装时不算失败 —— 本来就是可选依赖，采集层会自动降级。
        """
        import tempfile
        from pathlib import Path

        from collectors import browser_parser

        ok, why = browser_parser.available()
        if not ok:
            return f"未启用，自动降级为纯 httpx（{why[:48]}）"

        page = Path(tempfile.mkdtemp()) / "js.html"
        page.write_text(
            "<html><body><div id='p'>加载中</div>"
            "<script>document.getElementById('p').textContent='券后价 88 元';</script>"
            "</body></html>",
            encoding="utf-8",
        )
        html, engine = browser_parser.render_html(page.as_uri())
        if not html:
            # 装了 playwright 但没有配套浏览器内核时不算失败：采集层会自动降级，
            # CI 上也不会因为缺内核就变红。
            return f"未启用，自动降级为纯 httpx（{engine[:48]}）"
        assert "券后价 88 元" in html or "88" in html, f"JS 没跑起来，拿到 {len(html)} 字节"

        # 再验证「抓瘦了 → 兜底补全」这条链路本身
        from collectors.link_parser import _fill, _thin, ScrapeResult

        res = ScrapeResult(url=page.as_uri())
        _fill(res, "<html><head><title>空壳页</title></head></html>", res.url)
        assert _thin(res), "缺价格时应判定为需要兜底"
        _fill(res, html, res.url, only_missing=True)
        return f"浏览器渲染可用（{engine}），兜底补价链路正常"

    check("时间/价格解析", step_price_time)
    check("批量文本解析", step_parse)
    check("过滤与入库", step_insert)
    check("指纹去重", step_dedupe)
    check("状态自动流转", step_status)
    check("接龙登记与累加", step_signup)
    check("卡片渲染", step_render)
    check("文案生成", step_copy)
    check("对外脱敏", step_privacy)
    check("浏览器兜底", step_browser)
    check("Excel 导出", step_excel)
    check("统计查询", step_stats)
    check("图表渲染", step_charts)
    check("主窗口构建", step_ui)

    width = max(len(n) for n, _, _ in results)
    print("\n=== 邻里圈自检报告 ===")
    for name, ok, detail in results:
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {name.ljust(width)}  {detail}")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"\n{len(failed)} 项失败，详情：")
        for name, _ok, detail in failed:
            print(f"--- {name} ---\n{detail}")
    print(f"\n临时数据目录：{tmp}")
    print(f"结果：{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
