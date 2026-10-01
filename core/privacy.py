"""对外脱敏：姓名、联系方式、门牌号、自由文本里的敏感串。

设计原则：库里存真值，出去的东西才打码。
采集/录入时不改数据，只在「生成群文案」和「导出文件」两个出口做处理，
这样自己核对名单时还能看到完整信息，发群或外发文件时自动收敛。

两个场景分开对待：
    public —— 发到群里的文案、卡片。昵称本来就是群里公开身份，保留；
              但手机号、门牌号、身份证这类必须遮掉。
    export —— 导出的 Excel，可能被转发或留档，比群里更敏感，收得更紧。
"""
from __future__ import annotations

import re

from core import config

# --- 脱敏等级 ---------------------------------------------------------------
LEVEL_OFF = "off"        # 不处理（自己本地核对时用）
LEVEL_MEDIUM = "medium"  # 标准：public 遮号码类，export 姓名部分打码
LEVEL_STRICT = "strict"  # 严格：public 姓名也化名，export 联系方式清空

LEVELS = (LEVEL_OFF, LEVEL_MEDIUM, LEVEL_STRICT)
LEVEL_LABELS = {
    LEVEL_OFF: "不脱敏（原样输出）",
    LEVEL_MEDIUM: "标准（推荐：号码遮罩，姓名保留昵称）",
    LEVEL_STRICT: "严格（姓名化名，导出隐去联系方式）",
}

# --- 正则 -------------------------------------------------------------------
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_IDCARD = re.compile(r"(?<!\d)(\d{17}[\dXx]|\d{15})(?!\d)")
# 门牌：3栋502 / 12号楼1单元1801 / A座1603 / 5-2-801
_ROOM = re.compile(
    r"(\d{1,3}\s*(?:栋|号楼|幢|座|[A-Za-z]座))"
    r"((?:\s*\d{1,2}\s*单元)?[\s\-]*\d{3,4})\s*(?:室|户|房)?"
)
_LONGNUM = re.compile(r"(?<!\d)(\d{6,})(?!\d)")
_EMAIL = re.compile(r"([\w.+-]{1,20})@([\w.-]+\.\w+)")

_MASK = "****"


# ===========================================================================
# 等级读写
# ===========================================================================


def current_level() -> str:
    """读取当前脱敏等级，非法值回落到 medium。"""
    lv = config.load_settings().get("privacy_level", LEVEL_MEDIUM)
    return lv if lv in LEVELS else LEVEL_MEDIUM


def set_level(level: str) -> str:
    """写入脱敏等级。"""
    lv = level if level in LEVELS else LEVEL_MEDIUM
    config.save_settings({"privacy_level": lv})
    return lv


def level_label(level: str | None = None) -> str:
    return LEVEL_LABELS.get(level or current_level(), LEVEL_MEDIUM)


# ===========================================================================
# 单项脱敏
# ===========================================================================


def mask_phone(text: str) -> str:
    """手机号保留前 3 后 4：13812345678 -> 138****5678。"""
    return _PHONE.sub(lambda m: m.group(1)[:3] + _MASK + m.group(1)[-4:], text or "")


def mask_room(text: str) -> str:
    """门牌号保留到楼栋，房间号打码：3栋502 -> 3栋***。"""
    def _sub(m: re.Match) -> str:
        return m.group(1) + "***"
    return _ROOM.sub(_sub, text or "")


def mask_name(name: str, level: str | None = None, scope: str = "public",
              index: int = 0) -> str:
    """姓名 / 昵称脱敏。

    public + medium：原样返回（群里本来就是公开身份）。
    public + strict、export + medium/strict：按规则打码或化名。
    """
    lv = level or current_level()
    raw = (name or "").strip()
    if not raw or lv == LEVEL_OFF:
        return raw
    if scope == "public" and lv == LEVEL_MEDIUM:
        return raw
    if lv == LEVEL_STRICT and scope == "public":
        return f"邻居{index or 1}"

    # 导出场景：部分打码，保留辨识度
    if len(raw) == 1:
        return raw
    if len(raw) == 2:
        return raw[0] + "*"
    return raw[0] + "*" * (len(raw) - 2) + raw[-1]


def mask_contact(value: str, level: str | None = None, scope: str = "public") -> str:
    """联系方式脱敏：手机号遮中间，微信/邮箱部分遮罩，严格级直接置空。"""
    lv = level or current_level()
    raw = (value or "").strip()
    if not raw or lv == LEVEL_OFF:
        return raw
    if lv == LEVEL_STRICT and scope == "export":
        return "已隐藏"

    out = mask_phone(raw)
    if out != raw:
        return out
    if "@" in raw:
        return _EMAIL.sub(lambda m: m.group(1)[:1] + "***@" + m.group(2), raw)
    if len(raw) <= 4:
        return raw[0] + "*" * max(len(raw) - 1, 1)
    return raw[:2] + _MASK + raw[-1:]


def mask_free_text(text: str, level: str | None = None) -> str:
    """自由文本兜底扫描：身份证 / 手机号 / 门牌 / 超长数字串 / 邮箱。"""
    lv = level or current_level()
    raw = text or ""
    if not raw or lv == LEVEL_OFF:
        return raw
    out = _IDCARD.sub(lambda m: m.group(1)[:4] + "*" * (len(m.group(1)) - 4), raw)
    out = mask_phone(out)
    out = mask_room(out)
    out = _EMAIL.sub(lambda m: m.group(1)[:1] + "***@" + m.group(2), out)
    out = _LONGNUM.sub(lambda m: m.group(1)[:2] + _MASK, out)
    return out


# ===========================================================================
# 组合出口
# ===========================================================================


def scrub_signup(signup, level: str | None = None, scope: str = "public",
                 index: int = 0):
    """返回一条报名记录的脱敏副本（不改原对象）。"""
    from copy import copy

    lv = level or current_level()
    if lv == LEVEL_OFF:
        return signup
    out = copy(signup)
    out.name = mask_name(signup.name, lv, scope, index)
    out.contact = mask_contact(signup.contact, lv, scope)
    out.note = mask_free_text(signup.note, lv)
    return out


def scrub_rows(rows: list, level: str | None = None, scope: str = "public") -> list:
    """批量脱敏报名记录，保持顺序。"""
    return [scrub_signup(s, level, scope, i) for i, s in enumerate(rows, 1)]


def scrub_text(text: str, level: str | None = None) -> str:
    """整段文案出口兜底：生成完再扫一遍，防止备注之类漏网。"""
    return mask_free_text(text, level)
