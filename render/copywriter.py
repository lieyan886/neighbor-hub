"""群发文案生成与接龙文本回解析。

思路：工具负责把结构化数据写成「群里看着顺」的一段字，
也负责把群里回收的接龙文本再拆回结构化数据 —— 这条回路是工作流的关键。
"""
from __future__ import annotations

import re

from core import privacy
from core.models import Item, Signup, KIND_LABELS, kind_label
from core.utils import format_price, humanize

_LINE_NUM = re.compile(r"^\s*(\d{1,3})\s*[.、．)）:：]\s*(.+)$")
_QTY_AT_END = re.compile(r"(\d+(?:\.\d+)?)\s*(份|件|个|斤|人|箱|支|盒|袋)?\s*$")


# ===========================================================================
# 生成：结构化 -> 文本
# ===========================================================================


def build_announcement(item: Item, community: str = "", operator: str = "",
                       contact: str = "") -> str:
    """生成一条群公告文案。"""
    lines: list[str] = []
    head = f"【{kind_label(item.kind)}】{item.title}"
    lines.append(head)
    if community:
        lines.append(f"#{community}#")

    if item.summary:
        lines.append(privacy.scrub_text(item.summary))

    details: list[str] = []
    if item.price is not None:
        price = f"¥{format_price(item.price)}"
        if item.unit:
            price += f"/{item.unit}"
        if item.origin_price and item.origin_price > item.price:
            price += f"（原价 ¥{format_price(item.origin_price)}）"
        details.append(price)
    if item.merchant:
        details.append(f"商户：{item.merchant}")
    if item.location:
        # 自提点往往是团长家门牌，对外一律扫掉号码类信息
        details.append(f"地点/自提：{privacy.mask_free_text(item.location)}")
    if item.kind == "event" and item.event_at:
        details.append(f"时间：{humanize(item.event_at)}")
    if item.deadline:
        details.append(f"截止：{humanize(item.deadline)}")
    if item.quota:
        details.append(f"名额/目标：{item.quota}{item.unit or '份'}")
    if details:
        lines.append("————————")
        lines.extend(details)

    if item.url:
        lines.append(f"详情：{item.url}")

    lines.append("————————")
    tail = "想参加的邻居直接群里接龙"
    if contact:
        tail += f"，或私聊 {operator or '我'}（{contact}）"
    lines.append(tail + "。")
    return "\n".join(lines)


def build_solitaire(item: Item, signups: list[Signup] | None = None,
                    unit: str = "") -> str:
    """生成接龙文案：带序号的空模板，已有记录先占位。"""
    unit = unit or item.unit or ("人" if item.kind == "event" else "份")
    lines = [f"【{item.title}】接龙"]
    if item.kind == "event":
        tip = "格式：1. 门牌号/昵称 人数"
    else:
        tip = f"格式：1. 昵称 {unit}数"
    lines.append(tip)
    lines.append("————————")

    if signups:
        for idx, s in enumerate(privacy.scrub_rows(signups, scope="public"), 1):
            qty = f"{format_price(s.qty)}{s.unit or unit}"
            lines.append(f"{idx}. {s.name} {qty}")
        start = len(signups) + 1
    else:
        start = 1
    for i in range(start, start + 3):
        lines.append(f"{i}.")
    return "\n".join(lines)


def build_digest(items: list[Item], community: str = "", title: str = "") -> str:
    """多条内容合成一条「今日播报」清单。"""
    community = community or "邻居们"
    out = [f"📢 {title or '今日小区信息汇总'} —— {community}", ""]
    for idx, it in enumerate(items, 1):
        seg = f"{idx}. 【{kind_label(it.kind)}】{it.title}"
        bits = []
        if it.price is not None:
            bits.append(f"¥{format_price(it.price)}{('/' + it.unit) if it.unit else ''}")
        if it.merchant:
            bits.append(it.merchant)
        when = it.event_at if it.kind == "event" and it.event_at else it.deadline
        if when:
            bits.append(humanize(when))
        if bits:
            seg += f"（{'，'.join(bits)}）"
        out.append(seg)
    out.extend(["", "感兴趣的邻居群里说话，我来统计。"])
    return "\n".join(out)


# ===========================================================================
# 回解析：文本 -> 结构化
# ===========================================================================


def parse_solitaire(text: str, unit: str = "份",
                    default_qty: float = 1.0) -> list[tuple[str, float]]:
    """把群里回收的接龙文本拆成 [(名字, 数量), ...]。

    兼容以下几种写法：
        1. 张三 2份
        2、3栋王姐 1
        3)李雷  (没写数量按 1 算)
    """
    rows: list[tuple[str, float]] = []
    for ln in (text or "").splitlines():
        line = ln.strip()
        if not line:
            continue
        m = _LINE_NUM.match(line)
        body = m.group(2) if m else line
        body = body.strip()
        if not body:
            continue
        # 排除非接龙行：标题/说明里常见的引导语
        if body.startswith(("格式", "接龙")) or "接龙" in body and len(body) < 12:
            continue

        qty = default_qty
        qm = _QTY_AT_END.search(body)
        if qm:
            qty = float(qm.group(1))
            body = body[: qm.start()].strip()
        name = re.sub(r"\s+", " ", body).strip(" ，,、")
        if not name:
            continue
        rows.append((name, qty))
    return rows


def dedupe_solitaire(rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """同名数量累加。"""
    merged: dict[str, float] = {}
    for name, qty in rows:
        merged[name] = merged.get(name, 0.0) + qty
    return list(merged.items())
