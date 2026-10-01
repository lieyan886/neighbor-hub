"""链接抓取与解析：把一条 URL 变成一条半成品条目。

不做登录、不碰需要 JS 渲染的页面 —— 目标是覆盖 80% 的公开优惠页 /
活动页 / 商品页，剩下的用手动录入兜底。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from core import config, utils

_URL_RE = re.compile(r"https?://[^\s<>\"'）)】]+")
_PRICE_META = re.compile(
    r'(?:price|价格|券后|到手)[^0-9]{0,12}?(\d{1,6}(?:\.\d{1,2})?)', re.IGNORECASE
)
_DEADLINE_RE = re.compile(
    r"(?:截止|活动至|有效期|截止日期|至)\s*[:：]?\s*"
    r"([0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2}日?|"
    r"[0-9]{1,2}[月/][0-9]{1,2}日?)"
)


@dataclass
class ScrapeResult:
    """一次抓取的结果。"""

    url: str = ""
    ok: bool = False
    title: str = ""
    summary: str = ""
    image_url: str = ""
    local_cover: str = ""
    price: float | None = None
    source: str = ""
    deadline: str = ""
    tags: list[str] = field(default_factory=list)
    error: str = ""


class _MetaCrawler(HTMLParser):
    """提取 <title> 与各类 meta 标签，容错优先。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self._in_title = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            self._buffer = []
        elif tag == "meta":
            d = {k.lower(): (v or "") for k, v in attrs}
            key = d.get("property") or d.get("name") or d.get("itemprop")
            content = d.get("content", "")
            if key and content:
                self.meta[key.lower()] = content

    def handle_endtag(self, tag):
        if tag.lower() == "title" and self._in_title:
            self.title = "".join(self._buffer).strip()
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self._buffer.append(data)

    def get(self, *keys: str) -> str:
        for k in keys:
            v = self.meta.get(k.lower(), "")
            if v:
                return v.strip()
        return ""


def extract_first_url(text: str) -> str:
    """从一段混杂文本里抠出第一个 URL。"""
    m = _URL_RE.search(text or "")
    return m.group(0) if m else ""


def guess_source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    host = re.sub(r"^www\.", "", host)
    return host.split(":")[0]


def scrape_url(url: str, download_cover: bool = True) -> ScrapeResult:
    """抓取单个链接，解析标题/头图/价格/时间。失败时把原因放进 error。"""
    result = ScrapeResult(url=url)
    if not url.startswith(("http://", "https://")):
        result.error = "不是合法的 http(s) 链接"
        return result

    settings = config.load_settings()
    timeout = float(settings.get("request_timeout", 12))
    headers = {"User-Agent": settings.get("user_agent", ""), "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        result.error = f"请求失败：{type(exc).__name__}"
        return result

    html = resp.text[:400_000]
    crawler = _MetaCrawler()
    try:
        crawler.feed(html)
    except Exception:
        # 半残 HTML 也能拿到部分 meta，不放弃
        pass

    result.title = utils.clean_text(
        crawler.get("og:title", "twitter:title") or crawler.title
    )
    result.summary = utils.clean_text(
        crawler.get("og:description", "description", "twitter:description"), limit=200
    )
    img = crawler.get("og:image", "twitter:image")
    if img:
        result.image_url = urljoin(url, img)

    # —— 价格：meta 优先，正文次之 ——
    meta_price = crawler.get("product:price:amount")
    if meta_price:
        result.price = utils.parse_price(meta_price)
    if result.price is None:
        m = _PRICE_META.search(f"{result.title} {result.summary}")
        if m:
            result.price = float(m.group(1))

    # —— 截止时间 ——
    m = _DEADLINE_RE.search(f"{result.title} {result.summary}")
    if m:
        dt = utils.parse_datetime(m.group(1))
        result.deadline = utils.to_iso(dt)

    result.source = guess_source(url)
    result.tags = _guess_tags(result.title, result.summary)
    result.ok = bool(result.title)

    if not result.title:
        result.error = "页面没有解析到标题"

    if download_cover and result.image_url and settings.get("auto_download_cover", True):
        result.local_cover = download_image(result.image_url, referer=url)

    return result


def download_image(url: str, referer: str = "") -> str:
    """把远程图片存到本地封面目录，返回本地路径；失败返回空串。"""
    try:
        config.ensure_dirs()
        settings = config.load_settings()
        headers = {"User-Agent": settings.get("user_agent", "")}
        if referer:
            headers["Referer"] = referer
        resp = httpx.get(url, headers=headers, timeout=float(settings.get("request_timeout", 12)),
                         follow_redirects=True)
        resp.raise_for_status()
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        name = utils.fingerprint(url) + ext
        dest = config.COVER_DIR / name
        dest.write_bytes(resp.content)
        return str(dest)
    except Exception:
        return ""


def _guess_tags(title: str, summary: str) -> list[str]:
    """按关键词粗打标签，方便后续筛选。"""
    text = f"{title} {summary}"
    rules = {
        "生鲜果蔬": ("生鲜", "水果", "蔬菜", "车厘子", "榴莲"),
        "餐饮": ("餐厅", "美食", "自助", "火锅", "奶茶", "咖啡"),
        "母婴": ("母婴", "奶粉", "纸尿裤", "儿童"),
        "日用": ("纸巾", "洗衣", "清洁", "囤货"),
        "亲子活动": ("亲子", "手工", "绘本", "亲子活动"),
        "免费": ("免费", "0元", "不要钱"),
        "满减": ("满减", "优惠券", "券"),
    }
    return [tag for tag, words in rules.items() if any(w in text for w in words)]


def convert_to_item(res: ScrapeResult, kind: str | None = None) -> "object":
    """把抓取结果转成 Item（延迟导入避免循环依赖）。"""
    from core.models import Item

    if kind is None:
        kind = guess_kind(res.title, res.summary)
    return Item(
        title=res.title,
        kind=kind,
        summary=res.summary,
        url=res.url,
        source=res.source,
        cover=res.local_cover,
        price=res.price,
        deadline=res.deadline,
        tags=utils.tags_to_text(res.tags),
        collected=True,
        source_hash=utils.fingerprint(res.title, res.url),
    )


def guess_kind(title: str, summary: str = "") -> str:
    """按文案特征猜条目类型。"""
    text = f"{title} {summary}"
    if any(w in text for w in ("活动", "报名", "沙龙", "亲子", "义诊", "观影", "讲座", "市集")):
        return "event"
    if any(w in text for w in ("拼单", "拼团", "团购", "起订", "凑满", "自提")):
        return "groupbuy"
    return "deal"
