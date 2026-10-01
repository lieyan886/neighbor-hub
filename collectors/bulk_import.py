"""批量导入：把 Excel / 粘贴文本变成条目草稿。

支持三种输入：
1. 一行一条，随便写，自动猜类型；
2. Excel 粘贴（Tab 分隔），表头自动识别；
3. .xlsx 文件导入。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from core.models import KIND_DEAL, Item
from core.utils import clean_text, fingerprint, parse_datetime, parse_price, tags_to_text, to_iso

# 表头别名 -> 字段
_HEADER_MAP = {
    "标题": "title", "名称": "title", "活动名称": "title", "商品": "title",
    "title": "title",
    "摘要": "summary", "说明": "summary", "描述": "summary", "简介": "summary",
    "备注": "note", "note": "note",
    "链接": "url", "地址": "url", "url": "url", "详情": "url",
    "价格": "price", "单价": "price", "到手价": "price", "price": "price",
    "原价": "origin_price", "原价(元)": "origin_price",
    "单位": "unit", "单位/份": "unit",
    "商户": "merchant", "商家": "merchant", "店铺": "merchant", "主办": "merchant",
    "地点": "location", "地址2": "location", "自提点": "location",
    "目标": "quota", "目标份数": "quota", "名额": "quota", "上限": "quota",
    "开始时间": "event_at", "活动时间": "event_at",
    "截止": "deadline", "截止时间": "deadline", "到期": "deadline", "活动截止": "deadline",
    "类型": "kind", "分类": "kind",
    "标签": "tags", "tags": "tags",
}

_KIND_ALIAS = {
    "活动": "event", "社区活动": "event", "event": "event",
    "拼单": "groupbuy", "团购": "groupbuy", "拼团": "groupbuy", "groupbuy": "groupbuy",
    "优惠": "deal", "羊毛": "deal", "deal": "deal", "折扣": "deal",
}

_LINE_SPLIT = re.compile(r"[\r\n]+")


def _looks_like_header(cells: list[str]) -> bool:
    return sum(1 for c in cells if clean_text(c) in _HEADER_MAP) >= 2


def normalize_kind(value: str) -> str:
    key = clean_text(value).lower()
    return _KIND_ALIAS.get(key, KIND_DEAL)


def parse_table(text: str) -> list[Item]:
    """解析 Tab / 竖线分隔的表格文本（Excel 直接粘出来的最常见）。"""
    lines = [ln for ln in _LINE_SPLIT.split(text or "") if ln.strip()]
    if not lines:
        return []

    rows: list[list[str]] = []
    for ln in lines:
        if "\t" in ln:
            rows.append(ln.split("\t"))
        else:
            rows.append([p.strip() for p in re.split(r"[|｜]", ln)])

    fields: list[str] | None = None
    items: list[Item] = []
    for idx, cells in enumerate(rows):
        cells = [clean_text(c) for c in cells]
        if not any(cells):
            continue
        if idx == 0 and _looks_like_header(cells):
            fields = [_HEADER_MAP.get(clean_text(c), "") for c in cells]
            continue
        items.append(_row_to_item(cells, fields or []))
    return [it for it in items if it.title]


def _row_to_item(cells: list[str], fields: list[str]) -> Item:
    item = Item()
    for i, raw in enumerate(cells):
        key = fields[i] if i < len(fields) and fields[i] else None
        if not raw:
            continue
        if key is None:
            # 没表头时按顺序兜底：标题、摘要、价格
            if i == 0:
                item.title = raw
            elif i == 1 and not item.summary:
                item.summary = raw
            continue
        if key == "title":
            item.title = raw
        elif key == "summary":
            item.summary = raw
        elif key == "note":
            item.note = raw
        elif key == "url":
            item.url = raw
        elif key == "price":
            item.price = parse_price(raw)
        elif key == "origin_price":
            item.origin_price = parse_price(raw)
        elif key == "unit":
            item.unit = raw
        elif key == "merchant":
            item.merchant = raw
        elif key == "location":
            item.location = raw
        elif key == "quota":
            item.quota = int(parse_price(raw) or 0)
        elif key == "event_at":
            item.event_at = to_iso(parse_datetime(raw))
        elif key == "deadline":
            item.deadline = to_iso(parse_datetime(raw))
        elif key == "kind":
            item.kind = normalize_kind(raw)
        elif key == "tags":
            item.tags = tags_to_text(raw.replace("、", ",").split(","))
    item.tags = item.tags or ""
    return item


def parse_lines(text: str, kind: str | None = None) -> list[Item]:
    """一行一条的自由文本导入，自动抽取价格、时间、类型。"""
    items: list[Item] = []
    for ln in _LINE_SPLIT.split(text or ""):
        raw = clean_text(ln)
        if len(raw) < 2:
            continue
        title = raw
        summary = ""
        parts = re.split(r"[|｜\-—]+", raw, maxsplit=1)
        if len(parts) == 2 and len(parts[0]) >= 4:
            title, summary = clean_text(parts[0]), clean_text(parts[1])

        from collectors.link_parser import guess_kind

        item = Item(
            title=title[:80],
            summary=summary[:200],
            kind=kind or guess_kind(title, summary),
            price=parse_price(raw),
            event_at=to_iso(parse_datetime(raw)) if _mentions_event_time(raw) else "",
            deadline=to_iso(parse_datetime(raw)) if _mentions_deadline(raw) else "",
            source="手动导入",
            source_hash=fingerprint(title, summary),
        )
        items.append(item)
    return items


_EVENT_WORDS = ("活动", "开始", "举办")
_DEADLINE_WORDS = ("截止", "之前", "到期", "closing", "至")


def _mentions_event_time(text: str) -> bool:
    return any(w in text for w in _EVENT_WORDS) and bool(re.search(r"\d{1,2}\s*[:：]", text))


def _mentions_deadline(text: str) -> bool:
    return any(w in text for w in _DEADLINE_WORDS)


def import_excel(path: str | Path, sheet: str | None = None) -> list[Item]:
    """从 xlsx 文件读取第一行表头，按别名映射成条目。"""
    from openpyxl import load_workbook

    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []

    header = [clean_text(str(c)) if c is not None else "" for c in rows[0]]
    fields = [_HEADER_MAP.get(h, "") for h in header]
    items: list[Item] = []
    for raw in rows[1:]:
        cells = [clean_text("" if v is None else str(v)) for v in raw]
        if not any(cells):
            continue
        item = _row_to_item(cells, fields)
        if item.title:
            item.source = f"Excel:{Path(path).name}"
            items.append(item)
    return items


def merge_items(*groups: Iterable[Item]) -> list[Item]:
    """合并多个来源的条目并按指纹去重，保留第一个。"""
    seen: set[str] = set()
    out: list[Item] = []
    for group in groups:
        for it in group:
            h = it.source_hash or fingerprint(it.title, it.url)
            if h in seen:
                continue
            seen.add(h)
            it.source_hash = h
            out.append(it)
    return out
