"""业务数据模型。

三类条目（社区活动 / 邻里拼单 / 周边优惠）共用一张 items 表，
差别体现在 kind 字段与各自的业务列上，避免维护三套几乎一样的结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import utils
from .utils import days_left

# --- 条目类型 ---------------------------------------------------------------
KIND_EVENT = "event"
KIND_GROUPBUY = "groupbuy"
KIND_DEAL = "deal"

KIND_LABELS = {
    KIND_EVENT: "社区活动",
    KIND_GROUPBUY: "邻里拼单",
    KIND_DEAL: "周边优惠",
}
KIND_ORDER = (KIND_EVENT, KIND_GROUPBUY, KIND_DEAL)

# --- 生命周期状态 -----------------------------------------------------------
STATUS_DRAFT = "draft"        # 草稿，还没打算发群
STATUS_ACTIVE = "active"      # 进行中
STATUS_ENDING = "ending"      # 即将截止（会自动晋级）
STATUS_EXPIRED = "expired"    # 已过期
STATUS_ARCHIVED = "archived"  # 归档，不再参与统计

STATUS_LABELS = {
    STATUS_DRAFT: "草稿",
    STATUS_ACTIVE: "进行中",
    STATUS_ENDING: "即将截止",
    STATUS_EXPIRED: "已过期",
    STATUS_ARCHIVED: "已归档",
}
ACTIVE_STATUSES = (STATUS_DRAFT, STATUS_ACTIVE, STATUS_ENDING)


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind or "未知")


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "未知")


@dataclass
class Item:
    """一条社群内容：活动、拼单或优惠信息。"""

    title: str = ""
    kind: str = KIND_DEAL
    id: int | None = None
    summary: str = ""
    url: str = ""
    source: str = ""
    cover: str = ""
    merchant: str = ""          # 商户 / 主办方
    location: str = ""          # 活动地点 / 自提点
    price: float | None = None  # 单价或人均
    origin_price: float | None = None
    unit: str = ""              # 计价单位：份 / 斤 / 人
    quota: int = 0              # 目标份数或名额上限，0 表示不限
    event_at: str = ""          # 活动开始时间
    deadline: str = ""          # 报名或优惠截止时间
    status: str = STATUS_DRAFT
    tags: str = ""
    note: str = ""
    source_hash: str = ""
    collected: bool = False
    published_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    # —— 派生属性 ——

    @property
    def tag_list(self) -> list[str]:
        return utils.text_to_tags(self.tags)

    @property
    def progress(self) -> tuple[float, float]:
        """拼单/报名进度，返回 (已认领数量, 目标数量)。"""

        return 0.0, float(self.quota or 0)

    @property
    def deadline_text(self) -> str:
        if self.kind == KIND_EVENT and self.event_at:
            return utils.humanize(self.event_at)
        return utils.humanize(self.deadline)

    @property
    def days_left(self) -> int | None:
        return days_left(self.deadline or self.event_at)

    def derive_status(self, remind_hours: int = 24) -> str:
        """按时间自动推导显示状态：过期 / 即将截止。"""
        if self.status in (STATUS_ARCHIVED, STATUS_EXPIRED):
            return self.status
        left = self.days_left
        target = self.deadline or self.event_at
        if left is not None and target:
            if left < 0:
                return STATUS_EXPIRED
            if left == 0 or (self.deadline and left * 24 <= remind_hours):
                return STATUS_ENDING
        if self.status == STATUS_DRAFT:
            return STATUS_DRAFT
        return STATUS_ACTIVE

    # —— 转换 ——

    @classmethod
    def from_row(cls, row) -> "Item":
        keys = row.keys() if hasattr(row, "keys") else []
        data = {k: row[k] for k in keys}
        return cls(**{
            k: v for k, v in {
                "id": data.get("id"),
                "title": data.get("title") or "",
                "kind": data.get("kind") or KIND_DEAL,
                "summary": data.get("summary") or "",
                "url": data.get("url") or "",
                "source": data.get("source") or "",
                "cover": data.get("cover") or "",
                "merchant": data.get("merchant") or "",
                "location": data.get("location") or "",
                "price": data.get("price"),
                "origin_price": data.get("origin_price"),
                "unit": data.get("unit") or "",
                "quota": data.get("quota") or 0,
                "event_at": data.get("event_at") or "",
                "deadline": data.get("deadline") or "",
                "status": data.get("status") or STATUS_DRAFT,
                "tags": data.get("tags") or "",
                "note": data.get("note") or "",
                "source_hash": data.get("source_hash") or "",
                "collected": bool(data.get("collected")),
                "published_at": data.get("published_at") or "",
                "created_at": data.get("created_at") or "",
                "updated_at": data.get("updated_at") or "",
            }.items()
        })

    def to_row(self, include_id: bool = False) -> dict:
        data = {
            "title": self.title,
            "kind": self.kind,
            "summary": self.summary,
            "url": self.url,
            "source": self.source,
            "cover": self.cover,
            "merchant": self.merchant,
            "location": self.location,
            "price": self.price,
            "origin_price": self.origin_price,
            "unit": self.unit,
            "quota": self.quota,
            "event_at": self.event_at,
            "deadline": self.deadline,
            "status": self.status,
            "tags": self.tags,
            "note": self.note,
            "source_hash": self.source_hash,
            "collected": int(self.collected),
            "published_at": self.published_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_id and self.id is not None:
            data["id"] = self.id
        return data


@dataclass
class Signup:
    """报名 / 拼单接龙记录。

    注意：这里刻意不存金额流水。工具只负责「谁要了几份、有没有结清」，
    收付款走微信群收款或线下，避免工具变成账本。
    """

    item_id: int
    name: str = ""
    id: int | None = None
    contact: str = ""
    qty: float = 1.0
    unit: str = "份"
    note: str = ""
    settled: bool = False
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Signup":
        return cls(
            id=row["id"],
            item_id=row["item_id"],
            name=row["name"] or "",
            contact=row["contact"] or "",
            qty=row["qty"] or 1.0,
            unit=row["unit"] or "份",
            note=row["note"] or "",
            settled=bool(row["settled"]),
            created_at=row["created_at"] or "",
        )

    def to_row(self, include_id: bool = False) -> dict:
        data = {
            "item_id": self.item_id,
            "name": self.name,
            "contact": self.contact,
            "qty": self.qty,
            "unit": self.unit,
            "note": self.note,
            "settled": int(self.settled),
            "created_at": self.created_at,
        }
        if include_id and self.id is not None:
            data["id"] = self.id
        return data


@dataclass
class CardTemplate:
    """分享卡片的渲染模板。"""

    name: str = "默认模板"
    id: int | None = None
    background: str = "#FAF7F0"      # 卡片底色
    accent: str = "#D85A30"          # 强调色（价格、标题条）
    title_color: str = "#2C2C2A"
    body_color: str = "#5F5E5A"
    show_price: bool = True
    show_deadline: bool = True
    show_qr: bool = True
    show_tags: bool = True
    footer: str = ""                 # 底部固定话术
    width: int = 900
    height: int = 1200
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "CardTemplate":
        return cls(
            id=row["id"],
            name=row["name"] or "未命名模板",
            background=row["background"] or "#FAF7F0",
            accent=row["accent"] or "#D85A30",
            title_color=row["title_color"] or "#2C2C2A",
            body_color=row["body_color"] or "#5F5E5A",
            show_price=bool(row["show_price"]),
            show_deadline=bool(row["show_deadline"]),
            show_qr=bool(row["show_qr"]),
            show_tags=bool(row["show_tags"]),
            footer=row["footer"] or "",
            width=row["width"] or 900,
            height=row["height"] or 1200,
            created_at=row["created_at"] or "",
        )

    def to_row(self, include_id: bool = False) -> dict:
        data = {
            "name": self.name,
            "background": self.background,
            "accent": self.accent,
            "title_color": self.title_color,
            "body_color": self.body_color,
            "show_price": int(self.show_price),
            "show_deadline": int(self.show_deadline),
            "show_qr": int(self.show_qr),
            "show_tags": int(self.show_tags),
            "footer": self.footer,
            "width": self.width,
            "height": self.height,
            "created_at": self.created_at,
        }
        if include_id and self.id is not None:
            data["id"] = self.id
        return data


@dataclass
class PublishLog:
    """一次分发记录：生成了什么图、什么文案、发给谁。"""

    item_id: int
    channel: str = "wechat"     # wechat / image / clipboard
    id: int | None = None
    image_path: str = ""
    text: str = ""
    created_at: str = ""


@dataclass
class WatchSource:
    """v1.2.0：一个被「盯梢」的页面。

    典型场景是团长常看的团购/拼单链接：标题和档期会变，工具定时重抓一遍，
    价格、截止时间、标题有变化就推送提醒，省得每天自己去开链接确认。
    """

    url: str = ""
    id: int | None = None
    title: str = ""             # 最近一次抓到的标题
    last_price: float | None = None
    last_deadline: str = ""
    last_hash: str = ""         # 内容指纹，用于判断要不要提醒
    enabled: bool = True
    item_id: int | None = None  # 如果已入库，指向那条内容，方便同步更新
    note: str = ""              # 用户备注，比如「团长的牛肉卷」
    last_checked: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "WatchSource":
        return cls(
            id=row["id"],
            url=row["url"] or "",
            title=row["title"] or "",
            last_price=row["last_price"],
            last_deadline=row["last_deadline"] or "",
            last_hash=row["last_hash"] or "",
            enabled=bool(row["enabled"]),
            item_id=row["item_id"],
            note=row["note"] or "",
            last_checked=row["last_checked"] or "",
            created_at=row["created_at"] or "",
        )

    def to_row(self, include_id: bool = False) -> dict:
        data = {
            "url": self.url,
            "title": self.title,
            "last_price": self.last_price,
            "last_deadline": self.last_deadline,
            "last_hash": self.last_hash,
            "enabled": int(self.enabled),
            "item_id": self.item_id,
            "note": self.note,
            "last_checked": self.last_checked,
            "created_at": self.created_at,
        }
        if include_id and self.id is not None:
            data["id"] = self.id
        return data


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
