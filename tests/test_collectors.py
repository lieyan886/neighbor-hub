"""采集层：批量文本解析、过滤去重、链接解析与浏览器兜底。"""
from __future__ import annotations

import pytest

from collectors import bulk_import
from collectors.filters import ItemFilter
from collectors.link_parser import _fill, _thin, ScrapeResult, guess_kind, guess_source
from core.models import KIND_GROUPBUY, Item

SAMPLE = (
    "山姆牛肉卷 3斤装 168元 截止10月5日\n"
    "周六亲子观影｜10月7日 15:00\n"
    "小区包子铺 豆浆买一送一 3元"
)


def test_parse_lines_and_table():
    parsed = bulk_import.merge_items(
        bulk_import.parse_table(SAMPLE), bulk_import.parse_lines(SAMPLE)
    )
    assert parsed, "一行都没解析出来"
    assert any("山姆" in p.title for p in parsed)


def test_guess_source_and_kind():
    assert guess_source("https://www.smzdm.com/p/123/") == "smzdm.com"
    assert guess_kind("周六亲子观影活动报名") == "event"
    assert guess_kind("车厘子拼单") == "groupbuy"
    assert guess_kind("包子铺买一送一") == "deal"


def test_filter_dedupe_by_fingerprint(seed):
    dup = Item(title="山姆牛肉卷拼单", kind=KIND_GROUPBUY, source_hash="test-groupbuy")
    res = ItemFilter(dedupe=True).apply([dup])
    assert res.kept == [], "重复条目没被过滤掉"


def test_filter_blacklist():
    item = Item(title="成人用品特惠", kind="deal", source_hash="x1")
    flt = ItemFilter(dedupe=False, blacklist=["成人"])
    assert flt.apply([item]).kept == []


def test_fill_and_thin():
    """缺价格时应判定为「太瘦」，触发浏览器兜底。"""
    res = ScrapeResult(url="https://example.com/x")
    _fill(res, "<html><head><title>空壳页</title></head></html>", res.url)
    assert res.title == "空壳页"
    assert _thin(res), "没有价格就该判定需要兜底"

    # 正文里带价格，剥标签后应能被抓到
    html = ("<html><head><title>商品</title></head><body>"
            "<script>var a=1;</script><div>券后价 88 元</div></body></html>")
    res2 = ScrapeResult(url="https://example.com/y")
    _fill(res2, html, res2.url)
    assert res2.price == 88.0


@pytest.mark.browser
def test_browser_fallback_runs_real_js(tmp_path):
    """用本地 file:// 页面跑一次真 JS 渲染（默认不跑，见 pytest.ini）。

    两个坑，都已绕开：
    1. Playwright 的 sync API 在 pytest 进程内会挂死（greenlet 与 pytest 冲突，
       已用最小用例复现，与本项目代码无关）→ 放进子进程跑；
    2. Playwright 会拉起 node driver 子进程并继承 stdout，用管道 capture_output
       会永远等不到 EOF → 改写成临时文件再读。
    """
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    from collectors import browser_parser

    ok, why = browser_parser.available()
    if not ok:
        pytest.skip(f"playwright 不可用：{why}")

    page = tmp_path / "js.html"
    page.write_text(
        "<html><body><div id='p'>加载中</div>"
        "<script>document.getElementById('p').textContent='券后价 88 元';</script>"
        "</body></html>",
        encoding="utf-8",
    )
    script = (
        "from collectors.browser_parser import render_html;"
        f"html, engine = render_html({page.as_uri()!r});"
        "print('RENDER_OK' if '88' in html else 'RENDER_EMPTY')"
    )
    root = Path(__file__).resolve().parents[1]
    out_file = tmp_path / "out.txt"
    err_file = tmp_path / "err.txt"
    with open(out_file, "wb") as fo, open(err_file, "wb") as fe:
        proc = subprocess.run([sys.executable, "-c", script], cwd=str(root),
                              stdout=fo, stderr=fe, timeout=180)
    stdout = out_file.read_text(encoding="utf-8", errors="replace")
    stderr = err_file.read_text(encoding="utf-8", errors="replace")
    assert "RENDER_OK" in stdout, (
        f"JS 没跑起来（exit={proc.returncode}）。stdout={stdout[:200]} stderr={stderr[:300]}"
    )

    # 兜底补价链路本身（纯字符串处理，不启浏览器）
    html = "<html><body><div>券后价 88 元</div></body></html>"
    res = ScrapeResult(url=page.as_uri())
    _fill(res, "<html><head><title>空壳</title></head></html>", res.url)
    assert _thin(res)
    _fill(res, html, res.url, only_missing=True)
    assert res.price == 88.0, "兜底后仍没补上价格"
