"""通用工具：时间解析、金额提取、文本清洗、指纹去重。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path

# --- 时间解析 ---------------------------------------------------------------

_FULL_FMT = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
)
_DATE_ONLY_FMT = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日")

_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_MONTH_DAY_RE = re.compile(r"(?:(\d{4})\s*[年/\-\.])?\s*(\d{1,2})\s*[月/\-\.]\s*(\d{1,2})\s*日?")
_REL_DAYS_RE = re.compile(r"(今天|明天|后天)")
_CN_TIME_WORD = {"今天": 0, "明天": 1, "后天": 2}


def _apply_time(base: datetime, text: str) -> datetime:
    """把文本里出现的 HH:MM 套用到日期上，没有则回当天 23:59。"""
    m = _TIME_RE.search(text)
    if m:
        return base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                            second=0, microsecond=0)
    return base.replace(hour=23, minute=59, second=0, microsecond=0)


def parse_datetime(text: str | None) -> datetime | None:
    """尽力把各种人类写法的时间串解析成 datetime。

    支持：2026-10-05 14:30 / 10月5日 / 明天 18:00 / 后天 / 2026/10/5
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    for fmt in _FULL_FMT:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    for fmt in _DATE_ONLY_FMT:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    today = datetime.now().replace(second=0, microsecond=0)
    rel = _REL_DAYS_RE.search(raw)
    if rel:
        base = today + timedelta(days=_CN_TIME_WORD[rel.group(1)])
        return _apply_time(base, raw)

    m = _MONTH_DAY_RE.search(raw)
    if m:
        year = int(m.group(1)) if m.group(1) else today.year
        month, day = int(m.group(2)), int(m.group(3))
        try:
            base = today.replace(year=year, month=month, day=day)
        except ValueError:
            return None
        # 没写年份且日期已过，按明年算
        if not m.group(1) and base < today:
            try:
                base = base.replace(year=year + 1)
            except ValueError:
                pass
        return _apply_time(base, raw)

    return None


def to_iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def shift_days(text: str | None, days: int) -> str:
    """把一个时间串整体往后挪 N 天（周期性开团顺延截止用）。

    解析不出来就原样返回，绝不凭空造时间。
    """
    dt = parse_datetime(text)
    if dt is None:
        return text or ""
    return to_iso(dt + timedelta(days=days))


def humanize(iso_text: str | None, today: datetime | None = None) -> str:
    """把 ISO 串变成「今天 18:00」「10月5日」「已过期」这种口语表达。"""
    if not iso_text:
        return "未设置"
    dt = parse_datetime(iso_text)
    if dt is None:
        return iso_text
    today = today or datetime.now()
    diff = (dt.date() - today.date()).days
    hm = dt.strftime("%H:%M")
    if diff < 0:
        return f"已过期（{iso_text}）"
    if diff == 0:
        word = "今天"
    elif diff == 1:
        word = "明天"
    elif diff == 2:
        word = "后天"
    elif diff <= 7:
        word = f"{diff}天后"
    else:
        # 不让汉字进 strftime：Windows 的 C locale 下 CRT 会把「月」编码失败，
        # 抛 UnicodeEncodeError（Linux 的 UTF-8 locale 没事，所以这个坑只在 Windows 出现）
        word = f"{dt.month}月{dt.day}日"
    # 00:00 说明用户只填了日期没填时间（解析时补的零），显示「今天 00:00」
    # 反而像真的定了零点，直接把时间省掉
    tail = f" {hm}" if hm not in ("23:59", "00:00") else ""
    return f"{word}{tail}"


def days_left(iso_text: str | None) -> int | None:
    """剩余天数，已过期返回负数，无截止时间返回 None。"""
    dt = parse_datetime(iso_text)
    if dt is None:
        return None
    return (dt.date() - datetime.now().date()).days


# --- 金额解析 ---------------------------------------------------------------

_PRICE_RE = re.compile(
    r"(?:¥|￥|RMB|rmb)?\s*(\d{1,6}(?:\.\d{1,2})?)\s*(?:元|块|/件|/份|/斤)?"
)
_PRICE_CLEAN_RE = re.compile(r"[^\d.]")


def parse_price(text: str | None) -> float | None:
    """从一段文本里抠出最可能的价格数字。"""
    if not text:
        return None
    raw = str(text).strip()
    # 优先抓带货币符号或单位的部分，避免把「3件85折」里的 3 当价格
    prefer = re.search(
        r"(?:¥|￥|RMB|rmb)\s*(\d{1,6}(?:\.\d{1,2})?)", raw, flags=re.IGNORECASE
    )
    if prefer:
        return float(prefer.group(1))
    unit = re.search(r"(\d{1,6}(?:\.\d{1,2})?)\s*(?:元|块)", raw)
    if unit:
        return float(unit.group(1))
    m = _PRICE_RE.search(raw)
    return float(m.group(1)) if m else None


def format_price(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}".rstrip("0").rstrip(".") if value % 1 else f"{int(value)}"


# --- 文本工具 ---------------------------------------------------------------


def clean_text(text: str | None, limit: int = 0) -> str:
    if not text:
        return ""
    s = re.sub(r"\s+", " ", str(text)).strip()
    return s[:limit] if limit and len(s) > limit else s


# ===========================================================================
# v1.5.0：群昵称归一化
#
# 真实群里同一个人会用好几种写法出现：「张三」「3栋张三」「张三妈妈」「张三 」。
# 以前按名字精确匹配，于是同一户被记成三五个人 —— 份数被拆散，
# 结算时对不上人。这里把写法归一，用于归并判断。
# ===========================================================================

# 房号前缀：「3栋」「12号楼」「5-2」这类，剥掉后剩下的才是人
_HOUSE_PREFIX = re.compile(r"^\d+\s*(?:栋|幢|号楼|楼|座|单元|门|室|号)?\s*[-—]?\s*\d*\s*")
# 亲属后缀：家人代报名时的常见写法
_RELATION_SUFFIX = re.compile(
    r"(?:妈妈|爸爸|母亲|父亲|妈|爸|老婆|老公|爱人|媳妇|太太|先生|女士|家属|本人)$"
)
_NOISE = re.compile(r"[^\w\u4e00-\u9fff]+")     # 空白、标点、emoji、@ 等一律去掉


def normalize_name(name: str | None) -> str:
    """把群昵称归一成可比较的核心名。

    例：'3栋张三' -> '张三'；'张三妈妈 ' -> '张三'；' 张三 ' -> '张三'
    注意：只做保守剥离，'王姐' 的「姐」是称呼不是后缀，不会被吃掉。
    """
    s = _NOISE.sub("", str(name or ""))
    if not s:
        return ""
    s = _HOUSE_PREFIX.sub("", s)
    # 只在剥离后仍有内容时才剥后缀，避免把「妈妈」这种昵称整个清空
    stripped = _RELATION_SUFFIX.sub("", s)
    return stripped if stripped else s


def same_person(a: str | None, b: str | None) -> bool:
    """两个群昵称是否可能是同一个人。"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    return na == nb


def split_title_and_summary(text: str) -> tuple[str, str]:
    """把一行原始文本拆成标题和摘要，便于批量粘贴导入。"""
    s = clean_text(text)
    if not s:
        return "", ""
    parts = re.split(r"[|｜\-—\n]", s, maxsplit=1)
    title = parts[0].strip()
    summary = parts[1].strip() if len(parts) > 1 else ""
    return title, summary


def fingerprint(*parts: str) -> str:
    """生成去重指纹，用于「同一条优惠别采两遍」。"""
    base = "|".join(p for p in parts if p)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def unique_path(path: str | Path) -> Path:
    """同名的文件别互相覆盖：已经存在就换成 name (2).jpg 这种。

    手动选封面时，用户挑的两张图可能就叫 IMG_001.jpg，直接 copy 会把
    上一张的封面悄悄顶掉，事后根本查不出来。
    """
    p = Path(path)
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    parent = p.parent
    n = 2
    while True:
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


def tags_to_text(tags: list[str]) -> str:
    seen: list[str] = []
    for t in tags:
        t = clean_text(t)
        if t and t not in seen:
            seen.append(t)
    return ",".join(seen)


def text_to_tags(text: str | None) -> list[str]:
    if not text:
        return []
    return [clean_text(t) for t in str(text).split(",") if clean_text(t)]
