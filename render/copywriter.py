"""群发文案生成与接龙文本回解析。

思路：工具负责把结构化数据写成「群里看着顺」的一段字，
也负责把群里回收的接龙文本再拆回结构化数据 —— 这条回路是工作流的关键。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core import privacy
from core.models import Item, Signup, KIND_LABELS, kind_label
from core.utils import format_price, humanize

_LINE_NUM = re.compile(r"^\s*(\d{1,3})\s*[.、．)）:：]\s*(.+)$")
_QTY_AT_END = re.compile(r"(\d+(?:\.\d+)?)\s*(份|件|个|斤|人|箱|支|盒|袋|瓶|包)?\s*$")

# —— v1.4.0：真实群聊里的脏东西 ——
# 群里复制出来的文本远不止「1. 张三 2份」，混着表情、时间戳、闲聊和改单，
# 老版本只认行尾数量，遇到这些就全废了，最后还是得团长手工删一遍。
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")
_STAMP = re.compile(
    r"^\s*(?:\d{4}年\d{1,2}月\d{1,2}日)?\s*(?:上午|下午|晚上|凌晨)?\s*"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*")
_AT = re.compile(r"@[^\s:：,，]+")

_CHAT_WORDS = (
    "收到", "好的", "好嘞", "好哒", "好勒", "ok", "okay", "嗯", "明白", "了解",
    "谢谢", "多谢", "辛苦了", "支持", "已接龙", "稍等", "在的", "来了", "安排",
    "可以", "没问题", "thanks", "thx", "哈哈", "赞",
)
_CHAT_ONLY = re.compile(r"^(?:" + "|".join(_CHAT_WORDS) + r")+$", re.I)

# 改单 / 取消：群里二次粘贴时最常见的两种「修正」
_MODIFY = re.compile(r"(?:改成|改为|换成|改为是|调整[成为]?|增加[到]?|加到|改成是)"
                     r"\s*(\d+(?:\.\d+)?)")
_CANCEL = re.compile(r"(?:不要了|不参与|不参加了|取消了?|退出|退了|算了|划掉)")

# 数量写在中间而不是行尾：张三要2份 / 张三+1 / 张三x2
_VERB_QTY = re.compile(r"(?:要|买|来|订|拿|加|提|报|需|给我|需要)"
                       r"\s*(\d+(?:\.\d+)?)\s*(份|件|个|斤|人|箱|支|盒|袋|瓶|包)?")
_PLUS_QTY = re.compile(r"[+＋]\s*(\d+(?:\.\d+)?)")
_MUL_QTY = re.compile(r"[xX×\*]\s*(\d+(?:\.\d+)?)")


# 「我要2份」这种是团长自己接的龙，解析出来名字只有一个「我」——
# 误记成一条叫「我」的报名，后面导出名单时很扎眼。这类先挡掉。
_NOT_A_NAME = re.compile(r"^(我|本人|自己|群主|团长|楼主|组织者|收货人|下单人)$")


def _clean_name(text: str, self_name: str = "") -> str:
    """把一段文本收拾成可用的人名；不是人名（纯符号/纯数字/代词）就返回空。

    self_name 是团长自己的昵称：「我要2份」这种行解析不出人名，但**知道**
    就是团长本人接的龙。给了 self_name 就替成它，没给就退回「认不出来」。
    """
    raw = re.sub(r"[\s\W_]+", "", text or "")
    name = re.sub(r"\s+", " ", text or "").strip(" ，,、。.:：!！~-—")
    if not name or re.fullmatch(r"[\W_0-9]+", name):
        return self_name if (self_name and _NOT_A_NAME.match(raw)) else ""
    if _NOT_A_NAME.match(name):
        return self_name or ""
    return name


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
# v1.4.0：发团之后的全周期文案
#
# 之前只有「发团前」的三件套（公告/接龙模板/汇总），开团之后催一轮、
# 到货通知、催收结算、没成团的解释，全都得团长手打。这四种模板固定、
# 只换名字和份数，机器生成是白捡的收益。
# ===========================================================================


def build_reminder(item: Item, signed_qty: float = 0.0, community: str = "",
                   operator: str = "") -> str:
    """催办：快到截止了，还差多少没人报。"""
    unit = item.unit or ("人" if item.kind == "event" else "份")
    lines = [f"⏰ 催一下 —— 《{item.title}》"]
    if community:
        lines.append(f"#{community}#")

    if item.deadline or item.event_at:
        when = item.event_at if item.kind == "event" and item.event_at else item.deadline
        lines.append(f"时间就在 {humanize(when)}，还没报的邻居抓紧。")

    if item.quota:
        gap = max(0.0, float(item.quota) - float(signed_qty))
        lines.append(
            f"进度：{format_price(signed_qty)}/{item.quota}{unit}"
            + (f"，还差 {format_price(gap)}{unit} 成团。" if gap > 0
               else "，名额已满，不再加单。")
        )
    else:
        lines.append(f"目前已报 {format_price(signed_qty)}{unit}。")

    lines.append("————————")
    tail = "要报的在群里接龙一句就行"
    if operator:
        tail += f"，找不到入口找我（{operator}）"
    lines.append(tail + "。")
    return "\n".join(lines)


def build_arrival_notice(item: Item, signups: list[Signup], community: str = "",
                         operator: str = "", contact: str = "") -> str:
    """到货通知：货到了，按人列份数，方便核对。"""
    unit = item.unit or "份"
    lines = [f"📦 到货啦 —— 《{item.title}》"]
    if community:
        lines.append(f"#{community}#")

    bits = []
    if item.location:
        bits.append(f"取货点：{privacy.mask_free_text(item.location)}")
    if operator:
        bits.append(f"联系人：{operator}"
                    + (f"（{contact}）" if contact else ""))
    if bits:
        lines.extend(bits)
    lines.append("————————")

    # 这份名单会直接贴到群里，按 public 口径遮罩
    for idx, s in enumerate(privacy.scrub_rows(signups, scope="public"), 1):
        lines.append(f"{idx}. {s.name}　{format_price(s.qty)}{s.unit or unit}")

    if signups:
        total = sum(float(s.qty or 0) for s in signups)
        lines.append("————————")
        lines.append(f"共 {len(signups)} 位，合计 {format_price(total)}{unit}。")
    lines.append("麻烦对一下份数，有出入群里说一声，我这边改。")
    return "\n".join(lines)


def build_settlement_chase(item: Item, signups: list[Signup], community: str = "",
                           operator: str = "", contact: str = "") -> str:
    """催收结算：只点还没结清的人，已结清的不用理会。"""
    unit = item.unit or "份"
    pending = [s for s in (signups or []) if not s.settled]
    lines = [f"💰《{item.title}》结算提醒"]
    if community:
        lines.append(f"#{community}#")

    if not pending:
        lines.append("————————")
        lines.append("已经全部结清，谢谢各位邻居！")
        return "\n".join(lines)

    if item.price is not None:
        lines.append(f"每{unit} ¥{format_price(item.price)}，按自己登记的份数转就行。")
    lines.append(f"还有 {len(pending)} 位没结清，麻烦看到转一下：")
    lines.append("————————")
    for idx, s in enumerate(privacy.scrub_rows(pending, scope="public"), 1):
        amount = (f"　¥{format_price(float(s.qty or 0) * float(item.price or 0))}"
                  if item.price is not None else "")
        lines.append(f"{idx}. {s.name}　{format_price(s.qty)}{s.unit or unit}{amount}")
    lines.append("————————")
    tail = "收齐我会在群里说一声"
    if operator:
        tail += f"，有问题找我（{operator}）"
        if contact:
            tail += f"（{contact}）"
    lines.append(tail + "。已结清的不用理会，谢谢！")
    return "\n".join(lines)


def build_fail_notice(item: Item, signed_qty: float = 0.0,
                      community: str = "", operator: str = "") -> str:
    """没成团：给已报名邻居一个交代，避免群里尴尬。"""
    unit = item.unit or ("人" if item.kind == "event" else "份")
    lines = [f"😔《{item.title}》这次先撤了"]
    if community:
        lines.append(f"#{community}#")

    if item.quota:
        lines.append(
            f"目标 {item.quota}{unit}，最后接到 {format_price(signed_qty)}{unit}，"
            "没凑够起量，这次只能先取消。"
        )
    else:
        lines.append("参与的邻居太少，这次没能成行。")

    lines.append("————————")
    lines.append("已经报名的邻居不好意思，下次有合适的我再吆喝一声。")
    if operator:
        lines.append(f"（{operator}）")
    return "\n".join(lines)


# ===========================================================================
# 回解析：文本 -> 结构化
# ===========================================================================


@dataclass
class ParseResult:
    """接龙解析结果。

    rows        新增或累加的 (名字, 数量)
    adjustments 改单与取消：(名字, 新数量)，数量为 None 表示这人不要了
    skipped     实在识别不出人名的原行，交给用户肉眼确认，不静默吞掉
    """

    rows: list[tuple[str, float]] = field(default_factory=list)
    adjustments: list[tuple[str, float | None]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def parse_solitaire_detail(text: str, unit: str = "份",
                           default_qty: float = 1.0,
                           self_name: str = "") -> ParseResult:
    """把群里回收的文本拆成三类：新增 / 改单取消 / 认不出来的。

    兼容的写法（比老版本宽得多）：
        1. 张三 2份            # 行尾数量
        2、3栋王姐 1           # 名字里带数字但不会误吞
        张三要2份               # 数量在句中间
        张三+1 / 张三x2        # 追加、翻倍
        张三改成3份             # 改单（老版本会当成新加一个人）
        张三不要了              # 取消
        收到 / 👌 / 22:14 张三  # 自动滤掉闲聊、表情、微信时间戳

    self_name：团长自己的昵称（设置里的「你的署名」）。给了之后，
    「我要2份」「本人不要了」这类行就能落到团长名下，而不是被当成认不出来。
    """
    res = ParseResult()
    for raw in (text or "").splitlines():
        line = _STAMP.sub("", _EMOJI.sub("", raw)).strip()
        line = _AT.sub("", line).strip()
        if not line:
            continue

        m = _LINE_NUM.match(line)
        body = (m.group(2) if m else line).strip()
        if not body:
            continue

        # 引导语与标题行：【xx】接龙 / 格式：1. 昵称
        if body.startswith(("格式", "接龙", "【")) or ("接龙" in body and len(body) < 12):
            continue

        compact = re.sub(r"[\s\W_]+", "", body)
        if _CHAT_ONLY.match(compact):
            res.skipped.append(raw.strip())
            continue

        if _CANCEL.search(compact):
            name = _clean_name(_CANCEL.sub("", body), self_name)
            if name:
                res.adjustments.append((name, None))
                continue

        mod = _MODIFY.search(body)
        if mod:
            name = _clean_name(body[: mod.start()], self_name)
            if name:
                res.adjustments.append((name, float(mod.group(1))))
                continue

        qty = default_qty
        for pat in (_PLUS_QTY, _MUL_QTY):
            hit = pat.search(body)
            if hit:
                qty = float(hit.group(1))
                body = f"{body[: hit.start()]} {body[hit.end():]}".strip()
                break
        else:
            hit = _VERB_QTY.search(body)
            if hit:
                qty = float(hit.group(1))
                body = f"{body[: hit.start()]} {body[hit.end():]}".strip()
            else:
                hit = _QTY_AT_END.search(body)
                if hit:
                    qty = float(hit.group(1))
                    body = body[: hit.start()].strip()

        name = _clean_name(body, self_name)
        if not name:
            res.skipped.append(raw.strip())
            continue
        res.rows.append((name, qty))
    return res


def parse_solitaire(text: str, unit: str = "份",
                    default_qty: float = 1.0,
                    self_name: str = "") -> list[tuple[str, float]]:
    """把群里回收的接龙文本拆成 [(名字, 数量), ...]。

    只要新增部分（改单/取消要区分开的用 parse_solitaire_detail）。
    """
    return parse_solitaire_detail(text, unit, default_qty, self_name).rows


def dedupe_solitaire(rows: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """同名数量累加。"""
    merged: dict[str, float] = {}
    for name, qty in rows:
        merged[name] = merged.get(name, 0.0) + qty
    return list(merged.items())
