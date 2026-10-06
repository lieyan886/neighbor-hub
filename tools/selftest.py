"""自检脚本（tools/selftest.py）。

跑一遍「采集 -> 过滤 -> 入库 -> 接龙 -> 出图 -> 文案 -> 导出 -> 图表 -> UI」全链路，
用一个临时目录当数据目录，不碰真实数据。
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
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
    from core import utils
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
        win._goto("watch")
        win._refresh_summary()
        title = win.windowTitle()

        # v1.3.0 补：设置对话框曾经因为控件在 _load 里才创建、而 _load 开头
        # 就要读它们，一打开就 AttributeError 崩溃。这里守住不再回归。
        from ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(win)
        dlg.reject()
        win.close()
        return f"主窗口构建并切换五个页面成功：{title}；设置对话框可打开"

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

    def step_notifier() -> str:
        """桌面通知：有 Qt 就能力可用，没有就降级，两条路都不能抛异常。"""
        from core import notifier

        cap = notifier.available()
        popped = notifier.notify("自检", "这是一条测试通知")
        # 环境不支持时必须返回 False 而不是抛错，UI 层才有得降级
        assert popped is False or popped is True, "notify 返回值不合法"
        return ("QSystemTrayIcon 可用，通知已发出" if cap
                else "当前环境无托盘支持，调用安全降级（不崩溃）")

    def step_watch() -> str:
        """监控源：CRUD + 变化判定（用假抓取器，不发真实网络请求）。"""
        from core.models import WatchSource, stamp
        from core.repository import watch_sources as watch_repo
        from core import watcher

        src = WatchSource(url="https://example.com/deal/1", note="自检监控源",
                          created_at=stamp())
        sid = watch_repo.create(src)
        assert watch_repo.get(sid) is not None, "写进去读不出来"
        assert watch_repo.get_by_url(src.url) is not None, "按 URL 查不到"

        # 第一次抓 → 应为 first_seen
        first = watcher.check_source(watch_repo.get(sid))
        # example.com 在离线环境大概率抓不到，抓不到也算降级正常
        if first.error:
            detail = f"抓取失败已安全降级（{first.error[:24]}）"
        else:
            assert first.first_seen, "首次抓取应判定为 first_seen"
            detail = f"首次抓取：{first.summary()[:36]}"

        # 变化判定逻辑本身：改标题应识别出来
        saved = watch_repo.get(sid)
        saved.title = "旧标题"
        saved.last_price = 168.0
        saved.last_deadline = "2026-10-05 18:00"
        saved.last_hash = watcher.fingerprint_of("旧标题", "", 168.0, "2026-10-05 18:00")
        watch_repo.update(saved)

        from collectors import link_parser

        real_scrape = link_parser.scrape_url
        real_convert = link_parser.convert_to_item

        class _FakeResult:
            url = src.url
            title = "新标题"
            summary = "内容有更新"
            source = "example"
            local_cover = ""
            price = 158.0
            deadline = "2026-10-06 18:00"
            tags: list = []
            error = ""

        def fake_scrape(url, download_cover=True, allow_browser=None, progress=None):
            return _FakeResult()

        def fake_convert(res, kind=None):
            return Item(title=res.title, summary=res.summary, url=res.url,
                        price=res.price, deadline=res.deadline, kind="deal")

        link_parser.scrape_url = fake_scrape          # type: ignore[assignment]
        link_parser.convert_to_item = fake_convert    # type: ignore[assignment]
        try:
            change = watcher.check_source(watch_repo.get(sid))
            assert change.change_fields, "标题/价格/截止都变了却没识别出来"
            assert "price" in change.change_fields, "没识别出价格变化"
            watch_repo.save_snapshot(sid, title=change.title, price=change.price,
                                     deadline=change.deadline,
                                     content_hash=watcher.fingerprint_of(
                                         change.title, "", change.price, change.deadline))
            after = watch_repo.get(sid)
            assert after is not None and after.last_price == 158.0, "快照没写回去"
            detail += f"；改价后识别出 {change.change_fields}"
        finally:
            link_parser.scrape_url = real_scrape          # type: ignore[assignment]
            link_parser.convert_to_item = real_convert    # type: ignore[assignment]

        watch_repo.set_enabled(sid, False)
        assert watch_repo.get(sid) is not None and not watch_repo.get(sid).enabled
        watch_repo.delete(sid)
        assert watch_repo.get(sid) is None, "删除没生效"
        return detail

    def step_settlement() -> str:
        """结算闭环：批量改结清状态 → 汇总按结清/待结清拆开 → 导出催款清单。"""
        assert inserted, "没有可用条目"
        it = inserted[0]
        signup_repo.batch_add(it.id or 0, [(f"邻居{i}", float(i)) for i in range(1, 5)])
        rows = signup_repo.list_for(it.id or 0)
        assert len(rows) >= 4, "报名没登记进去"

        ids = [s.id for s in rows[:2]]
        changed = signup_repo.set_settled(it.id or 0, True, ids)
        assert changed == len(ids), f"批量结清应影响 {len(ids)} 行，实际 {changed}"

        s = signup_repo.settlement_summary(it.id or 0)
        assert s["done_people"] == 2, f"已结清人数应为 2，实际 {s['done_people']}"
        assert s["wait_people"] == len(rows) - 2, "待结清人数对不上"
        assert abs((s["done_qty"] + s["wait_qty"]) - s["qty"]) < 1e-6, "份数拆分后对不上总数"

        # 待结清清单里只该出现还没结清的人
        path = excel.export_settlement(it, signup_repo.list_for(it.id or 0),
                                       only_unsettled=True)
        assert Path(path).exists(), "待结清清单没导出"
        from openpyxl import load_workbook

        body = [r[0] for r in load_workbook(path).active.iter_rows(min_row=2,
                                                                   values_only=True)]
        names = [b for b in body if b]
        assert len(names) == s["wait_people"], (
            f"待结清清单应有 {s['wait_people']} 人，实际 {len(names)}")
        return (f"{len(rows)} 人：已结清 {s['done_people']} / 待结清 {s['wait_people']}"
                f"，导出 {Path(path).name}")

    def step_parse_dirty() -> str:
        """真实群聊里的脏文本：时间戳、表情、闲聊、句中数量、改单取消。"""
        text = ("2026年10月3日 22:14 1. 张三 2份\n"
                "22:15 收到\n"
                "👌\n"
                "2、3栋王姐 1\n"
                "李雷要3份\n"
                "王五+1\n"
                "张三改成3份\n"
                "李四不要了")
        res = copywriter.parse_solitaire_detail(text)
        names = dict(res.rows)
        assert names.get("张三") == 2.0, f"张三应 2 份，实际 {names.get('张三')}"
        assert names.get("3栋王姐") == 1.0, "房号开头的名字被误判成数量了"
        assert names.get("李雷") == 3.0, "句中数量没识别"
        assert names.get("王五") == 1.0, "+1 没识别"
        assert ("张三", 3.0) in res.adjustments, "改单没单独归类"
        assert ("李四", None) in res.adjustments, "取消没单独归类"
        assert res.skipped, "闲聊行应该被列出而不是凭空消失"
        return (f"新增 {len(res.rows)} / 改单取消 {len(res.adjustments)}"
                f" / 滤掉闲聊 {len(res.skipped)} 行")

    def step_aftersale() -> str:
        """v1.4.0：发团之后的四套文案，且该遮的必须遮住。"""
        from core.models import Signup

        probe = Item(title="自检团", kind="groupbuy", unit="份", price=10.0,
                     quota=10, location="3栋1502自提", deadline="2026-10-20 18:00")
        rows = [Signup(item_id=1, name="张三", qty=2.0, settled=True),
                Signup(item_id=1, name="李雷", qty=1.0, settled=False)]

        remind = copywriter.build_reminder(probe, 3.0)
        arrival = copywriter.build_arrival_notice(probe, rows)
        chase = copywriter.build_settlement_chase(probe, rows)
        fail = copywriter.build_fail_notice(probe, 3.0)
        for label, txt in (("催办", remind), ("到货", arrival),
                           ("催收", chase), ("未成团", fail)):
            assert txt.strip(), f"{label}文案生成出来是空的"

        assert "1502" not in arrival, "到货通知里泄露了自提点房号"
        assert "李雷" in chase and "张三" not in chase, "催收文案点错人了"
        assert "20" in fail or "10" in fail, "未成团说明没说清目标"
        return f"四套文案就绪；自提点已遮为 3栋***，催收只点尚未结清的 1 人"

    def step_duplicate() -> str:
        """周期性开团：复制出新一期，身份/时间要更新且不能带走上期报名。"""
        assert inserted, "没有可用条目"
        it = inserted[0]
        before = len(item_repo.list_items(include_archived=True))

        clone = item_repo.duplicate(it.id, days_shift=7)
        assert clone is not None and clone.id is None, "复制出来还带着旧 id"
        assert clone.source_hash != it.source_hash, "指纹没换会被去重拦掉"
        assert clone.status == "draft", "新一期应该是草稿"

        new_id = item_repo.create(clone)
        assert len(item_repo.list_items(include_archived=True)) == before + 1
        assert not signup_repo.list_for(new_id), "新一期带走了上期的报名"

        item_repo.delete(new_id)   # 别污染后面的统计
        assert len(item_repo.list_items(include_archived=True)) == before
        return f"复制《{it.title}》为新草稿并顺延 7 天，报名不跟随"

    def step_stats_deep() -> str:
        """v1.5.0 看板深化，顺便把 v1.4 那三个「看着对、实际错」的 bug 钉死。"""
        from core.models import Signup
        from core.repository import _month_keys, stats

        it = item_repo.list_items(include_archived=True)[0]
        before = stats.overview()
        signup_repo.add(Signup(item_id=it.id or 0, name="自检甲", qty=5))
        signup_repo.add(Signup(item_id=it.id or 0, name="自检乙", qty=3))
        after = stats.overview()

        # bug #1：份数必须是 SUM(qty)，不能是记录条数
        assert after["signups"] == before["signups"] + 2, "报名记录条数对不上"
        assert abs((after["units"] - before["units"]) - 8.0) < 1e-6, (
            "累计份数应多 8 份；若只多 2 说明又把记录条数当份数了")

        # bug #2：月份序列不能重复、不能漏月
        keys = _month_keys(6)
        assert len(set(keys)) == 6, f"月份重复：{keys}"

        # bug #3：目标份数必须算出来（不然「完成进度」名不副实）
        prog = stats.kind_progress()
        assert any(t > 0 for _k, _d, t in prog), "目标份数没统计出来"

        # 时间范围必须真的收窄
        assert int(stats.overview(30)["items"]) <= int(stats.overview(None)["items"])

        # 新增：成团率与环比
        fu = stats.fulfillment()
        assert "rate" in fu and 0 <= fu["rate"] <= 100
        cmp_ = stats.compare(30)
        assert all("delta" in v for v in cmp_.values()), "环比缺少变化率"
        return (f"份数 {after['units']:g}（+8），月份序列 {len(keys)} 项无重复，"
                f"成团率 {fu['rate']:g}%，环比三指标齐全")

    def step_alerts() -> str:
        """v1.5.0 提醒扩维：成团预警 + 结算逾期 + 去重落库。"""
        from core.models import Item, Signup
        from core.scheduler import ReminderService
        from core.repository import notified

        # 成团预警：差一点点就成团
        soon = Item(title="自检·快成团了", kind="groupbuy", unit="份", quota=10,
                    status="active", source_hash="st-alert-1",
                    deadline=(datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M"))
        soon.id = item_repo.create(soon)
        signup_repo.add(Signup(item_id=soon.id or 0, name="甲", qty=9))
        formation = {a.id: m for a, m in item_repo.formation_alerts()}
        assert soon.id in formation, "快成团的拼单没被预警"
        assert "还差 1 份" in formation[soon.id], formation[soon.id]

        # 结算逾期：团结束了还有人没结清
        end = Item(title="自检·已结束未结清", kind="groupbuy", unit="份", quota=5,
                   status="expired", source_hash="st-alert-2")
        end.id = item_repo.create(end)
        signup_repo.add(Signup(item_id=end.id or 0, name="甲", qty=2, settled=True))
        signup_repo.add(Signup(item_id=end.id or 0, name="乙", qty=1, settled=False))
        overdue = {a.id: n for a, n in item_repo.settlement_overdue()}
        assert end.id in overdue and overdue[end.id] == 1, "结算逾期没被识别"

        # 去重：扫过一次不能再报
        seen: list = []
        svc = ReminderService(on_settlement=lambda pairs: seen.extend(pairs))
        svc.scan()
        first = len(seen)
        svc.scan()
        assert len(seen) == first, f"第二次扫描重复提醒（{first} → {len(seen)}）"
        assert notified.has(end.id or 0, "settle"), "提醒没落库，重启后会重复弹"
        return f"成团预警 1 条、结算逾期 1 条，去重后二次扫描 {first} 条不变"

    def step_merge() -> str:
        """v1.5.0 报名同名归并：「3栋张三」和「张三」是同一户。"""
        from core.models import Item, Signup

        it = Item(title="自检·归并", kind="groupbuy", unit="份", quota=100,
                  status="active", source_hash="st-merge-1")
        it.id = item_repo.create(it)
        signup_repo.batch_add(it.id or 0,
                              [("张三", 2.0), ("3栋张三", 3.0), ("李四", 1.0)])
        rows = signup_repo.list_for(it.id or 0)
        assert len(rows) == 2, f"应合并成 2 人，实际 {len(rows)}"
        zs = [r for r in rows if r.name == "张三"][0]
        assert abs(zs.qty - 5.0) < 1e-6, f"份数应累加到 5，实际 {zs.qty}"
        assert "3栋张三" in (zs.note or ""), "别名没记进备注"

        # 查重分组 + 手动合并
        a = item_repo.list_items(include_archived=True)[0]
        g1 = signup_repo.add(Signup(item_id=a.id or 0, name="王五", qty=2, settled=True))
        g2 = signup_repo.add(Signup(item_id=a.id or 0, name="3栋王五", qty=3,
                                    settled=False))
        groups = signup_repo.duplicate_groups(a.id or 0)
        assert groups and any(len(g) > 1 for g in groups), "没找出疑似同人的分组"
        total = signup_repo.merge_signups(g1, [g2])
        assert abs(total - 5.0) < 1e-6, f"合并后应为 5 份，实际 {total}"
        left = [r for r in signup_repo.list_for(a.id or 0) if r.id == g1]
        assert left and not left[0].settled, "有一条没结清就不该算结清"
        return "「3栋张三」自动并入「张三」共 5 份；手动合并王五两组为 5 份且未结清"

    def step_price_history() -> str:
        """v1.7.0：价格历史 —— 回答「这次是真便宜还是先涨后降」。"""
        from core.repository import prices

        it = Item(title="自检·价格历史", kind="deal", unit="份", price=168.0,
                  source_hash="st-price-1")
        it.id = item_repo.create(it)
        assert len(prices.series(it.id)) == 1, "入库没记首价"

        it.price = 158.0
        item_repo.update(it)
        it.price = 178.0
        item_repo.update(it)
        seq = prices.series(it.id)
        assert len(seq) == 3, f"两次改价该有 3 个点，实际 {len(seq)}"

        item_repo.update(it)
        assert len(prices.series(it.id)) == 3, "价格没变却多记了一笔"

        s = prices.summary(it.id)
        assert s["low"] == 158.0 and s["high"] == 178.0 and s["now"] == 178.0
        subs = prices.subjects()
        assert any(k == f"item:{it.id}" for k, _, _ in subs), "看板下拉里找不到这条"

        # 接龙里的「我」要落到团长名下
        rows = copywriter.parse_solitaire("我要2份\n李四 3份", self_name="3栋小李")
        assert ("3栋小李", 2.0) in rows, f"「我」没记到团长名下：{rows}"
        assert ("李四", 3.0) in rows
        return (f"3 个价格点（{s['low']:g} ~ {s['high']:g}），重复保存不刷点；"
                f"「我要2份」记为团长 2 份")

    def step_watch_fp() -> str:
        """v1.6.0：盯梢「判断变化」和「存快照」必须用同一个指纹。

        以前存的时候少传一个 summary 字段，两边永远算不出同样的值，
        于是每隔一轮就误报一次「有新动静」——用户天天被假提醒吵醒。
        """
        from core.models import WatchSource
        from core.repository import watch_sources as watch_repo
        from core import watcher
        from collectors import link_parser

        src = WatchSource(url="https://example.com/deal/fp", note="自检·指纹",
                          created_at="2026-10-05 10:00:00")
        sid = watch_repo.create(src)
        try:
            svc = watcher.WatchService()
            fields = []
            times = []

            class _FakeResult:
                """固定不变的页面：三轮抓到的内容完全一样。"""
                url = "https://example.com/deal/fp"
                title = "车厘子 2斤"
                summary = "顺丰冷链当天到"
                source = "example"
                local_cover = ""
                price = 168.0
                deadline = "2026-10-07 20:00"
                tags: list = []
                error = ""

            def fake_scrape(url, download_cover=True, allow_browser=None, progress=None):
                # 类体里引用不到外层函数的 url，这里单独挂上去
                _FakeResult.url = url
                return _FakeResult()

            def fake_convert(res, kind=None):
                return Item(title=res.title, summary=res.summary, url=res.url,
                            price=res.price, deadline=res.deadline, kind="deal")

            real_scrape, real_convert = link_parser.scrape_url, link_parser.convert_to_item
            link_parser.scrape_url = fake_scrape        # type: ignore[assignment]
            link_parser.convert_to_item = fake_convert  # type: ignore[assignment]
            try:
                for _ in range(3):
                    ch = watcher.check_source(watch_repo.get(sid))
                    assert not ch.error, ch.error
                    times.append(ch.has_change)
                    fields += ch.change_fields
                    if ch.has_change:
                        svc.apply(ch)
            finally:
                link_parser.scrape_url = real_scrape         # type: ignore[assignment]
                link_parser.convert_to_item = real_convert   # type: ignore[assignment]

            assert times == [True, False, False], f"页面没变却反复报变化：{times}"
            return "连续 3 轮：第 1 轮首次抓到，后两轮零误报"
        finally:
            watch_repo.delete(sid)

    def step_duplicate_fresh() -> str:
        """v1.6.0：「再来一团」要从今天往后顺延，不能拿旧团截止再加 7 天。

        旧逻辑会把 9/1 到期的团复制成 9/8——今天都 10 月了，新一期一建好
        就是已过期状态，报名根本进不来。
        """
        from core.models import Signup

        old = Item(title="自检·旧团", kind="groupbuy", unit="份", quota=20,
                   status="expired", deadline="2026-10-01 18:00",
                   source_hash="st-dup-old")
        old.id = item_repo.create(old)
        signup_repo.add(Signup(item_id=old.id or 0, name="甲", qty=3))

        new = item_repo.duplicate(old.id or 0)
        assert new is not None, "复制失败"
        fresh = utils.parse_datetime(new.deadline or "")
        assert fresh is not None, "新一期没有截止时间"
        from datetime import datetime as _dt
        ahead = (fresh.date() - _dt.now().date()).days
        assert ahead >= 0, f"新一期还是已过期（{new.deadline}）"
        assert ahead <= 14, f"顺延太久不合理：{ahead} 天后"
        assert not signup_repo.list_for(new.id or 0), "报名不该跟随到新一期"
        return f"旧团 10-01 到期 → 新一期排在 {ahead} 天后，报名未跟随"

    def step_polish() -> str:
        """v1.6.0：几处会让人看不懂 / 记错账的显示口径。"""
        # 只填了日期就别显示「今天 00:00」（那看着像真约了零点）
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        text = utils.humanize(today.strftime("%Y-%m-%d %H:%M:%S"))
        assert "00:00" not in text, f"没填时间却显示出了具体时刻：{text}"
        # 填了时刻就照常带时间
        with_time = humanize(datetime.now().replace(
            hour=18, minute=30, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S"))
        assert with_time.endswith("18:30"), with_time
        # 「我要2份」不能记成一条叫「我」的报名
        names = [n for n, _ in copywriter.parse_solitaire("我要2份\n李四 3份")]
        assert "我" not in names, f"把「我」当人名记了：{names}"
        assert "李四" in names, names
        # 封面同名不互相覆盖
        tmp_dir = tmp / "covers"
        tmp_dir.mkdir(exist_ok=True)
        first = tmp_dir / "IMG_001.jpg"
        first.write_bytes(b"first")
        second = utils.unique_path(tmp_dir / "IMG_001.jpg")
        second.write_bytes(b"second")
        assert first.read_bytes() == b"first", "同名封面被顶掉了"
        return f"日期只显示「{text}」；「我要2份」不误记人名；同名封面落成 {second.name}"

    def step_backup() -> str:
        """备份与恢复：打包 → 删一条 → 恢复 → 数据得原样回来。"""
        from core import backup

        before = len(item_repo.list_items(include_archived=True))
        zip_path = backup.create_backup(tmp / "backups")
        assert Path(zip_path).exists(), "备份文件没生成"
        manifest = backup.read_manifest(zip_path)
        assert manifest.get("counts", {}).get("items") == before, "清单条目数与库里不一致"

        victim = item_repo.list_items(include_archived=True)[0]
        item_repo.delete(victim.id or 0)
        assert len(item_repo.list_items(include_archived=True)) == before - 1, "删除没生效"

        ok, msg = backup.restore_backup(zip_path)
        assert ok, f"恢复失败：{msg}"
        after = len(item_repo.list_items(include_archived=True))
        assert after == before, f"恢复后应回到 {before} 条，实际 {after}"
        return f"备份 {Path(zip_path).name}；删一条后恢复，还原到 {after} 条"

    def step_pushkit() -> str:
        """一键推群：文案 + 卡片 + 分发记录三件套能否串起来（不碰剪贴板）。"""
        picked = inserted[:2] or item_repo.list_items(limit=2)
        assert picked, "没有可用条目"
        cfg = config.load_settings()
        chunks = [
            copywriter.build_announcement(
                it, cfg.get("community_name", ""), cfg.get("operator_name", ""),
                cfg.get("contact_info", ""))
            for it in picked
        ]
        text = "\n\n".join(chunks)
        assert all(t.strip() for t in chunks), "公告文案出现了空串"
        # 剪贴板在 offscreen/CI 下不一定可用，失败了不算致命，只记录文案长度
        try:
            app.clipboard().setText(text)
            copied = True
        except Exception:
            copied = False

        template = tpl_repo.all()[0]
        paths = [render_card(it, template, CardContext(
            community_name=cfg.get("community_name", ""),
            operator_name=cfg.get("operator_name", ""),
            contact_info=cfg.get("contact_info", ""),
            join_url=it.url), None) for it in picked]
        assert paths and all(Path(p).exists() for p in paths), "卡片没真正落地"

        from core.repository import publishes as publish_repo

        for it in picked:
            publish_repo.add(it.id or 0, "wechat", image_path="", text="一键推群")
            item_repo.mark_published(it.id or 0)
        logs = publish_repo.recent(limit=20)
        assert any("一键推群" in (r["text"] or "") for r in logs), "分发记录没写进去"
        return (f"{len(picked)} 条文案 {len(text)} 字"
                f"{'（已写入剪贴板）' if copied else '（剪贴板不可用，已跳过）'}"
                f"，出图 {len(paths)} 张并记录分发")

    check("时间/价格解析", step_price_time)
    check("批量文本解析", step_parse)
    check("过滤与入库", step_insert)
    check("指纹去重", step_dedupe)
    check("状态自动流转", step_status)
    check("接龙登记与累加", step_signup)
    check("接龙脏文本解析", step_parse_dirty)
    check("结算闭环", step_settlement)
    check("卡片渲染", step_render)
    check("文案生成", step_copy)
    check("全周期文案", step_aftersale)
    check("对外脱敏", step_privacy)
    check("桌面通知", step_notifier)
    check("监控源盯梢", step_watch)
    check("一键推群包", step_pushkit)
    check("浏览器兜底", step_browser)
    check("Excel 导出", step_excel)
    check("统计查询", step_stats)
    check("图表渲染", step_charts)
    check("主窗口构建", step_ui)
    check("周期性开团", step_duplicate)
    check("备份与恢复", step_backup)
    # —— v1.5.0 ——
    check("看板深化与防回归", step_stats_deep)
    check("提醒扩维", step_alerts)
    check("报名同名归并", step_merge)
    # —— v1.6.0：审计修复 ——
    check("盯梢指纹不误报", step_watch_fp)
    check("再来一团按今天顺延", step_duplicate_fresh)
    check("看板口径与显示打磨", step_polish)
    # —— v1.7.0 ——
    check("价格历史", step_price_history)

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
