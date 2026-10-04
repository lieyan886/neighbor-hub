"""数据访问层：所有 SQL 集中在这里，UI 和业务逻辑不直接写 SQL。"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Iterable

from .db import get_database
from .models import (
    ACTIVE_STATUSES,
    CardTemplate,
    Item,
    Signup,
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_ENDING,
    STATUS_EXPIRED,
    WatchSource,
    stamp,
)
from . import utils


def _now() -> str:
    return stamp()


_ROUND_IN_TITLE = re.compile(r"第\s*(\d+)\s*期")


def _next_round_title(title: str) -> str:
    """标题里带「第N期」的自动 +1，没带的保持原样。"""
    hit = _ROUND_IN_TITLE.search(title or "")
    if not hit:
        return title
    return _ROUND_IN_TITLE.sub(f"第{int(hit.group(1)) + 1}期", title, count=1)


# ===========================================================================
# 条目仓储
# ===========================================================================


class ItemRepository:
    """items 表的增删改查。"""

    COLUMNS = (
        "title, kind, summary, url, source, cover, merchant, location, price, "
        "origin_price, unit, quota, event_at, deadline, status, tags, note, "
        "source_hash, collected, published_at, created_at, updated_at"
    )

    def list_items(
        self,
        kind: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        tag: str | None = None,
        include_archived: bool = False,
        limit: int = 500,
    ) -> list[Item]:
        sql = "SELECT * FROM items WHERE 1 = 1"
        params: dict[str, object] = {}
        if kind:
            sql += " AND kind = :kind"
            params["kind"] = kind
        if status:
            sql += " AND status = :status"
            params["status"] = status
        elif not include_archived:
            marks = ",".join(f":st{i}" for i in range(len(ACTIVE_STATUSES)))
            sql += f" AND status IN ({marks})"
            params.update({f"st{i}": s for i, s in enumerate(ACTIVE_STATUSES)})
        if keyword:
            sql += (" AND (title LIKE :kw OR summary LIKE :kw OR merchant "
                    "LIKE :kw OR location LIKE :kw)")
            params["kw"] = f"%{keyword}%"
        if tag:
            sql += " AND tags LIKE :tag"
            params["tag"] = f"%{tag}%"
        sql += " ORDER BY COALESCE(deadline, event_at) DESC, id DESC LIMIT :limit"
        params["limit"] = limit
        rows = get_database().query(sql, params)
        return [Item.from_row(r) for r in rows]

    def get(self, item_id: int) -> Item | None:
        row = get_database().query_one("SELECT * FROM items WHERE id = ?", (item_id,))
        return Item.from_row(row) if row else None

    def create(self, item: Item) -> int:
        row = item.to_row()
        row["created_at"] = row["updated_at"] = _now()
        if not row["source_hash"]:
            row["source_hash"] = utils.fingerprint(row["title"], row["url"])
        keys = ", ".join(row.keys())
        marks = ", ".join(f":{k}" for k in row)
        cur = get_database().execute(
            f"INSERT INTO items ({keys}) VALUES ({marks})", row
        )
        return int(cur.lastrowid)

    def update(self, item: Item) -> None:
        row = item.to_row()
        row["updated_at"] = _now()
        row["id"] = item.id
        sets = ", ".join(f"{k} = :{k}" for k in row if k != "id")
        get_database().execute(f"UPDATE items SET {sets} WHERE id = :id", row)

    def delete(self, item_id: int) -> None:
        get_database().execute("DELETE FROM items WHERE id = ?", (item_id,))

    def set_status(self, item_id: int, status: str) -> None:
        get_database().execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), item_id),
        )

    def mark_published(self, item_id: int) -> None:
        get_database().execute(
            "UPDATE items SET published_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), item_id),
        )

    def bulk_insert(self, items: Iterable[Item], dedupe: bool = True) -> tuple[int, int]:
        """批量写入，返回 (写入数, 因去重跳过的数量)。"""
        db = get_database()
        inserted, skipped = 0, 0
        with db.transaction():
            for it in items:
                if dedupe and self.hash_exists(it.source_hash):
                    skipped += 1
                    continue
                # 同一批内部也可能重复
                if dedupe and inserted and self.hash_exists(it.source_hash):
                    skipped += 1
                    continue
                self.create(it)
                inserted += 1
        return inserted, skipped

    # —— v1.4.0：周期性开团 ——

    def duplicate(self, item_id: int, days_shift: int = 7) -> Item | None:
        """复制一条内容开新一期，返回复制出来的草稿（未落库）。

        每周/每月重复的团是团长最高频的动作，重录一遍同样的商品纯属浪费。
        这里刻意改掉三样东西：身份（id/指纹）、发布痕迹、时间。
        """
        src = self.get(item_id)
        if src is None:
            return None
        clone = Item(**{k: v for k, v in src.__dict__.items() if k != "id"})
        clone.id = None
        clone.status = STATUS_DRAFT
        clone.collected = False
        clone.published_at = ""
        clone.created_at = ""
        clone.updated_at = ""
        # 新一期必须有自己的指纹，否则会被去重逻辑判成重复旧帖
        clone.source_hash = f"dup-{item_id}-{int(datetime.now().timestamp())}"
        clone.title = _next_round_title(src.title)

        when = src.event_at if (src.kind == "event" and src.event_at) else src.deadline
        if when and days_shift:
            shifted = utils.shift_days(when, days_shift)
            if src.event_at:
                clone.event_at = shifted
            if src.deadline:
                clone.deadline = shifted
        return clone

    def hash_exists(self, source_hash: str) -> bool:
        if not source_hash:
            return False
        row = get_database().query_one(
            "SELECT 1 FROM items WHERE source_hash = ? LIMIT 1", (source_hash,)
        )
        return row is not None

    def refresh_statuses(self, remind_hours: int = 24) -> int:
        """按截止时间批量推进状态：进行中 -> 即将截止 / 已过期。"""
        db = get_database()
        rows = db.query(
            "SELECT * FROM items WHERE status NOT IN (?, ?)",
            (STATUS_ARCHIVED, STATUS_EXPIRED),
        )
        changed = 0
        with db.transaction():
            for r in rows:
                item = Item.from_row(r)
                new_status = item.derive_status(remind_hours)
                if new_status != item.status and new_status in (
                    STATUS_ENDING,
                    STATUS_EXPIRED,
                ):
                    db.execute(
                        "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
                        (new_status, _now(), item.id),
                    )
                    changed += 1
        return changed

    def due_soon(self, hours: int = 24) -> list[Item]:
        """取出即将到期的条目，用于提醒弹窗。"""
        horizon = datetime.now() + timedelta(hours=hours)
        rows = get_database().query(
            "SELECT * FROM items WHERE status IN ('active', 'ending') "
            "AND deadline != '' AND deadline <= :hz "
            "ORDER BY deadline ASC",
            {"hz": horizon.strftime("%Y-%m-%d %H:%M")},
        )
        items = []
        for r in rows:
            it = Item.from_row(r)
            if (it.days_left or 0) >= 0:
                items.append(it)
        return items

    # —— v1.5.0：提醒扩维 ——

    def formation_alerts(self) -> list[tuple[Item, str]]:
        """成团预警：设了目标份数、还在进行中的拼单。

        两种情形：
          · 快成了 —— 进度已过八成但还没凑够，差几份一目了然，值得再推一把
          · 迟迟不成 —— 时间过半还不到一半，该催或者该考虑改方案了
        返回 [(条目, 提示语), ...]。
        """
        rows = get_database().query(
            "SELECT i.*, COALESCE(SUM(s.qty), 0) AS got FROM items i "
            "LEFT JOIN signups s ON s.item_id = i.id "
            "WHERE i.kind = 'groupbuy' AND i.quota > 0 "
            "AND i.status IN ('active', 'ending') GROUP BY i.id"
        )
        out: list[tuple[Item, str]] = []
        now = datetime.now()
        for r in rows:
            it = Item.from_row(r)
            quota = float(it.quota or 0)
            got = float(r["got"] or 0)
            if quota <= 0:
                continue
            gap = quota - got
            if 0 < gap <= quota * 0.2:
                out.append((it, f"还差 {gap:g} 份就成团（{got:g}/{quota:g}）"))
                continue
            # 迟迟不成团：需要能算出起止时间才判断「过半」
            start = utils.parse_datetime(it.created_at)
            end = utils.parse_datetime(it.deadline or it.event_at)
            if not start or not end or end <= start:
                continue
            if start <= now <= end and now >= start + (end - start) / 2:
                if got < quota * 0.5:
                    pct = got / quota * 100
                    out.append((it, f"时间过半才 {pct:g}%（{got:g}/{quota:g}），该催了"))
        return out

    def settlement_overdue(self) -> list[tuple[Item, int]]:
        """结算逾期：团已经结束，但还有人没结清。

        v1.3 做了结算闭环，数据躺在库里却没人提醒 —— 这里把它接上。
        返回 [(条目, 未结清人数), ...]。
        """
        rows = get_database().query(
            "SELECT i.*, SUM(CASE WHEN s.settled = 0 THEN 1 ELSE 0 END) AS unpaid "
            "FROM items i JOIN signups s ON s.item_id = i.id "
            "WHERE i.status = 'expired' GROUP BY i.id HAVING unpaid > 0 "
            "ORDER BY unpaid DESC"
        )
        return [(Item.from_row(r), int(r["unpaid"] or 0)) for r in rows]

    def all_tags(self) -> list[str]:
        rows = get_database().query(
            "SELECT tags FROM items WHERE tags != '' AND status != ?",
            (STATUS_ARCHIVED,),
        )
        bag: list[str] = []
        for r in rows:
            for t in utils.text_to_tags(r["tags"]):
                if t not in bag:
                    bag.append(t)
        return sorted(bag)


# ===========================================================================
# 报名 / 接龙仓储
# ===========================================================================


class SignupRepository:
    """signups 表：群接龙的登记结果。"""

    def list_for(self, item_id: int) -> list[Signup]:
        rows = get_database().query(
            "SELECT * FROM signups WHERE item_id = ? ORDER BY id ASC", (item_id,)
        )
        return [Signup.from_row(r) for r in rows]

    def add(self, signup: Signup) -> int:
        row = signup.to_row()
        row["created_at"] = _now()
        keys = ", ".join(row.keys())
        marks = ", ".join(f":{k}" for k in row)
        cur = get_database().execute(
            f"INSERT INTO signups ({keys}) VALUES ({marks})", row
        )
        return int(cur.lastrowid)

    def update(self, signup: Signup) -> None:
        row = signup.to_row()
        row["id"] = signup.id
        sets = ", ".join(f"{k} = :{k}" for k in row if k != "id")
        get_database().execute(f"UPDATE signups SET {sets} WHERE id = :id", row)

    def delete(self, signup_id: int) -> None:
        get_database().execute("DELETE FROM signups WHERE id = ?", (signup_id,))

    def get(self, signup_id: int) -> Signup | None:
        row = get_database().query_one("SELECT * FROM signups WHERE id = ?", (signup_id,))
        return Signup.from_row(row) if row else None

    def total_qty(self, item_id: int) -> float:
        row = get_database().query_one(
            "SELECT COALESCE(SUM(qty), 0) AS total FROM signups WHERE item_id = ?",
            (item_id,),
        )
        return float(row["total"]) if row else 0.0

    def headcount(self, item_id: int) -> int:
        row = get_database().query_one(
            "SELECT COUNT(*) AS c FROM signups WHERE item_id = ?", (item_id,)
        )
        return int(row["c"]) if row else 0

    def batch_add(self, item_id: int, rows: list[tuple[str, float]]) -> int:
        """批量登记群里的接龙，rows 为 [(名字, 数量), ...]。

        同名会自动累加数量，避免重复粘贴同一条接龙。
        v1.5.0：匹配从「名字完全相同」放宽到「归一化后同一个人」——
        群里「张三」和「3栋张三」本来就是同一户，以前会被记成两个人。
        """
        db = get_database()
        added = 0
        with db.transaction():
            for name, qty in rows:
                name = utils.clean_text(name)
                if not name:
                    continue
                exist = self._find_same(item_id, name)
                if exist:
                    db.execute(
                        "UPDATE signups SET qty = qty + ? WHERE id = ?",
                        (qty, exist["id"]),
                    )
                    # 写法不同但归到同一条时，把别名记进备注，方便事后核对
                    if exist["name"] != name:
                        alias = name
                        old_note = exist["note"] or ""
                        if alias not in old_note:
                            merged = (f"{old_note} / {alias}" if old_note else f"别名：{alias}")
                            db.execute("UPDATE signups SET note = ? WHERE id = ?",
                                       (merged.strip(" /"), exist["id"]))
                else:
                    db.execute(
                        "INSERT INTO signups (item_id, name, qty, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (item_id, name, qty, _now()),
                    )
                added += 1
        return added

    @staticmethod
    def _find_same(item_id: int, name: str):
        """先精确匹配，再按归一化后的核心名匹配。"""
        db = get_database()
        row = db.query_one(
            "SELECT * FROM signups WHERE item_id = ? AND name = ?", (item_id, name))
        if row:
            return row
        for r in db.query("SELECT * FROM signups WHERE item_id = ?", (item_id,)):
            if utils.same_person(r["name"], name):
                return r
        return None

    def duplicate_groups(self, item_id: int) -> list[list[Signup]]:
        """找出同一条目里疑似同一个人的报名分组。

        只返回「核心名相同但写法不同」的组（组里至少 2 条），
        完全同名的已经在登记时合并了。给用户确认后再动手合并。
        """
        rows = self.list_for(item_id)
        buckets: dict[str, list[Signup]] = {}
        for s in rows:
            key = utils.normalize_name(s.name)
            if not key:
                continue
            buckets.setdefault(key, []).append(s)
        return [g for g in buckets.values() if len(g) > 1]

    def merge_signups(self, keep_id: int, merge_ids: list[int]) -> float:
        """把若干条报名合并到 keep_id 上：份数相加、结清取「都结清才算结清」。

        返回合并后的总份数。
        """
        db = get_database()
        total = 0.0
        with db.transaction():
            keep = self.get(keep_id)
            if not keep:
                return 0.0
            total = float(keep.qty or 0)
            all_settled = bool(keep.settled)
            notes = [keep.note] if keep.note else []
            for sid in merge_ids:
                s = self.get(sid)
                if not s or sid == keep_id:
                    continue
                total += float(s.qty or 0)
                all_settled = all_settled and bool(s.settled)
                if s.name and s.name != keep.name:
                    notes.append(s.name)
                if s.note:
                    notes.append(s.note)
                db.execute("DELETE FROM signups WHERE id = ?", (sid,))
            seen: list[str] = []
            note = ""
            for n in notes:
                n = (n or "").strip(" /")
                if n and n not in seen:
                    seen.append(n)
            note = " / ".join(seen)
            db.execute(
                "UPDATE signups SET qty = ?, settled = ?, note = ? WHERE id = ?",
                (total, 1 if all_settled else 0, note[:200], keep_id),
            )
        return total

    def apply_adjustments(self, item_id: int,
                          adjustments: list[tuple[str, float | None]]) -> tuple[int, int]:
        """处理改单与取消：[(名字, 新数量)]，数量为 None 表示这人不要了。

        只对已经存在的记录生效——群里改单时人本来就在名单里，
        找不到同名就跳过，绝不凭空造一条。
        返回 (更新了多少条, 删了多少条)。
        """
        db = get_database()
        updated = removed = 0
        with db.transaction():
            for name, qty in adjustments:
                row = db.query_one(
                    "SELECT * FROM signups WHERE item_id = ? AND name = ?",
                    (item_id, name),
                )
                if not row:
                    continue
                if qty is None:
                    db.execute("DELETE FROM signups WHERE id = ?", (row["id"],))
                    removed += 1
                else:
                    db.execute("UPDATE signups SET qty = ? WHERE id = ?",
                               (qty, row["id"]))
                    updated += 1
        return updated, removed

    # —— v1.3.0：结算 ——

    def set_settled(self, item_id: int, settled: bool,
                    ids: list[int] | None = None) -> int:
        """批量改「是否已结清」，返回受影响行数。

        ids 为空表示整条内容一起改（全选 / 全部取消）。
        注意：这里只记结清状态，不碰任何金额——工具不当账本。
        """
        flag = 1 if settled else 0
        db = get_database()
        if ids:
            marks = ",".join("?" * len(ids))
            cursor = db.execute(
                f"UPDATE signups SET settled = ? WHERE item_id = ? AND id IN ({marks})",
                (flag, item_id, *ids),
            )
        else:
            cursor = db.execute(
                "UPDATE signups SET settled = ? WHERE item_id = ?", (flag, item_id)
            )
        return int(cursor.rowcount or 0)

    def settlement_summary(self, item_id: int) -> dict[str, float]:
        """结算台要的汇总：人数与份数按「已结清 / 待结清」拆开。"""
        row = get_database().query_one(
            "SELECT "
            "  COUNT(*) AS people, "
            "  COALESCE(SUM(qty), 0) AS qty, "
            "  COALESCE(SUM(CASE WHEN settled = 1 THEN 1 ELSE 0 END), 0) AS done_people, "
            "  COALESCE(SUM(CASE WHEN settled = 1 THEN qty ELSE 0 END), 0) AS done_qty, "
            "  COALESCE(SUM(CASE WHEN settled = 0 THEN 1 ELSE 0 END), 0) AS wait_people, "
            "  COALESCE(SUM(CASE WHEN settled = 0 THEN qty ELSE 0 END), 0) AS wait_qty "
            "FROM signups WHERE item_id = ?",
            (item_id,),
        )
        if not row:
            return {"people": 0, "qty": 0.0, "done_people": 0, "done_qty": 0.0,
                    "wait_people": 0, "wait_qty": 0.0}
        return {k: (float(row[k]) if k.endswith("qty") else int(row[k]))
                for k in row.keys()}


# ===========================================================================
# 卡片模板与分发记录
# ===========================================================================


class TemplateRepository:
    """分享卡片模板。"""

    def all(self) -> list[CardTemplate]:
        rows = get_database().query("SELECT * FROM card_templates ORDER BY id ASC")
        return [CardTemplate.from_row(r) for r in rows]

    def get(self, template_id: int) -> CardTemplate | None:
        row = get_database().query_one(
            "SELECT * FROM card_templates WHERE id = ?", (template_id,)
        )
        return CardTemplate.from_row(row) if row else None

    def create(self, tpl: CardTemplate) -> int:
        row = tpl.to_row()
        row["created_at"] = _now()
        keys = ", ".join(row.keys())
        marks = ", ".join(f":{k}" for k in row)
        cur = get_database().execute(
            f"INSERT INTO card_templates ({keys}) VALUES ({marks})", row
        )
        return int(cur.lastrowid)

    def update(self, tpl: CardTemplate) -> None:
        row = tpl.to_row()
        row["id"] = tpl.id
        sets = ", ".join(f"{k} = :{k}" for k in row if k != "id")
        get_database().execute(f"UPDATE card_templates SET {sets} WHERE id = :id", row)

    def delete(self, template_id: int) -> None:
        get_database().execute("DELETE FROM card_templates WHERE id = ?", (template_id,))


class PublishRepository:
    """分发留痕：哪条内容发过什么渠道、生成了什么图。"""

    def add(self, item_id: int, channel: str, image_path: str = "", text: str = "") -> int:
        cur = get_database().execute(
            "INSERT INTO publish_logs (item_id, channel, image_path, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, channel, image_path, text, _now()),
        )
        return int(cur.lastrowid)

    def recent(self, limit: int = 50) -> list[sqlite3.Row]:
        return get_database().query(
            "SELECT l.*, i.title FROM publish_logs l "
            "LEFT JOIN items i ON i.id = l.item_id "
            "ORDER BY l.id DESC LIMIT ?",
            (limit,),
        )

    def last_channel_of(self, item_id: int) -> str:
        row = get_database().query_one(
            "SELECT channel FROM publish_logs WHERE item_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (item_id,),
        )
        return row["channel"] if row else ""


# ===========================================================================
# 统计查询
# ===========================================================================


def _month_keys(months: int) -> list[str]:
    """按真实日历逐月回退，返回 ['2026-05', '2026-06', ...]（升序）。

    注意：不能用 today - timedelta(days=30*i) 反推 —— 步长 30 天撞上大小月
    会算出重复月份并漏掉整月（例如从 3/31 起算：3/31→03月、3/1→又是03月，
    2 月直接消失）。必须按年月算术递减。
    """
    y, m = datetime.now().year, datetime.now().month
    out: list[str] = []
    for _ in range(max(1, months)):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _since(days: int | None) -> str:
    """把「近 N 天」转成 created_at 的下界字符串；days 为空表示不限。"""
    if not days:
        return ""
    return (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")


class StatsRepository:
    """看板用的聚合查询。

    v1.5.0 起所有方法都支持 days 时间范围（None = 全部），
    并新增成团率（fulfillment）与环比（compare）。
    """

    # —— 总览 ——

    def overview(self, days: int | None = None) -> dict[str, float]:
        """条目与报名的总览数字。

        v1.5.0 修正：signups 是「报名记录条数」，units 才是「累计份数」。
        以前看板把记录条数当份数显示（一人报 5 份只算 1），现在拆开了。
        """
        db = get_database()
        where, args = " WHERE status != 'archived'", []
        since = _since(days)
        if since:
            where += " AND created_at >= ?"
            args.append(since)
        row = db.query_one(
            "SELECT COUNT(*) AS items, "
            "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN status = 'ending' THEN 1 ELSE 0 END) AS ending, "
            "SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) AS draft, "
            "SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS expired "
            f"FROM items{where}", args
        )
        # 报名的统计要跟条目同一时间口径：按关联条目的创建时间算
        if since:
            s_where = (" FROM signups s JOIN items i ON i.id = s.item_id "
                       "WHERE i.status != 'archived' AND i.created_at >= ?")
            s_args: list = [since]
        else:
            s_where = (" FROM signups s JOIN items i ON i.id = s.item_id "
                       "WHERE i.status != 'archived'")
            s_args = []
        total = db.query_one(f"SELECT COUNT(*) AS c{s_where}", s_args)
        people = db.query_one(f"SELECT COUNT(DISTINCT s.name) AS c{s_where}", s_args)
        units = db.query_one(f"SELECT COALESCE(SUM(s.qty), 0) AS c{s_where}", s_args)
        items_n = row["items"] or 0
        people_n = people["c"] if people else 0
        units_n = float(units["c"]) if units else 0.0
        return {
            "items": items_n,
            "active": row["active"] or 0,
            "ending": row["ending"] or 0,
            "draft": row["draft"] or 0,
            "expired": row["expired"] or 0,
            "signups": total["c"] if total else 0,
            "people": people_n,
            "units": units_n,
            # 人均份数：没有报名时给 0，避免除零
            "per_person": round(units_n / people_n, 2) if people_n else 0.0,
        }

    def count_by_kind(self, days: int | None = None) -> list[tuple[str, int]]:
        where, args = " WHERE status != 'archived'", []
        since = _since(days)
        if since:
            where += " AND created_at >= ?"
            args.append(since)
        rows = get_database().query(
            f"SELECT kind, COUNT(*) AS c FROM items{where} GROUP BY kind ORDER BY c DESC",
            args,
        )
        return [(r["kind"], r["c"]) for r in rows]

    def monthly_trend(self, months: int = 6) -> list[tuple[str, int]]:
        """近 N 个自然月每月新建条目数，用于趋势折线。"""
        keys = _month_keys(months)
        rows = get_database().query(
            "SELECT substr(created_at, 1, 7) AS ym, COUNT(*) AS c FROM items "
            "WHERE created_at IS NOT NULL AND created_at != '' GROUP BY ym"
        )
        data = {r["ym"]: r["c"] for r in rows}
        return [(k[2:], int(data.get(k, 0))) for k in keys]

    def top_items(self, limit: int = 8,
                  days: int | None = None) -> list[tuple[str, int, float]]:
        """报名人次最高的条目。"""
        where, args = " WHERE i.status != 'archived'", []
        since = _since(days)
        if since:
            where += " AND i.created_at >= ?"
            args.append(since)
        args.append(limit)
        rows = get_database().query(
            "SELECT i.title, COUNT(s.id) AS head, COALESCE(SUM(s.qty), 0) AS qty "
            "FROM signups s JOIN items i ON i.id = s.item_id"
            f"{where} GROUP BY s.item_id ORDER BY head DESC, qty DESC LIMIT ?",
            args,
        )
        return [(r["title"], r["head"], float(r["qty"])) for r in rows]

    def kind_progress(self, days: int | None = None) -> list[tuple[str, float, float]]:
        """各类型的 (已完成份数, 目标份数)，用于画「完成 vs 目标」对比。"""
        where, args = " WHERE i.status != 'archived'", []
        since = _since(days)
        if since:
            where += " AND i.created_at >= ?"
            args.append(since)
        rows = get_database().query(
            "SELECT i.kind, COALESCE(SUM(s.qty), 0) AS done, "
            "SUM(COALESCE(NULLIF(i.quota, 0), 0)) AS target "
            "FROM items i LEFT JOIN signups s ON s.item_id = i.id"
            f"{where} GROUP BY i.kind", args,
        )
        return [(r["kind"], float(r["done"]), float(r["target"])) for r in rows]

    # —— v1.5.0 新增 ——

    def fulfillment(self, days: int | None = None) -> dict[str, float]:
        """成团率：设了目标份数的拼单里，有多少真的凑够了。

        这是团长最关心的运营指标 —— 发团数不等于成团数。
        """
        since = _since(days)
        where = " WHERE i.kind = 'groupbuy' AND i.quota > 0 AND i.status != 'archived'"
        args: list = []
        if since:
            where += " AND i.created_at >= ?"
            args.append(since)
        rows = get_database().query(
            "SELECT i.id, i.quota, COALESCE(SUM(s.qty), 0) AS got "
            "FROM items i LEFT JOIN signups s ON s.item_id = i.id"
            f"{where} GROUP BY i.id", args,
        )
        total = len(rows)
        formed = sum(1 for r in rows if float(r["got"]) >= float(r["quota"] or 0))
        return {
            "total": float(total),
            "formed": float(formed),
            "rate": round(formed / total * 100, 1) if total else 0.0,
        }

    def compare(self, days: int = 30) -> dict[str, dict[str, float]]:
        """本期 vs 上一期的环比。

        返回 {'items': {'now': x, 'prev': y, 'delta': 百分比}, ...}
        delta 为 None 表示上期是 0（没法算增长率），UI 显示「新增」即可。
        """
        cur = self.overview(days)
        # 上一期 = 往前再推 days 天，取那一段的累计值
        prev_total = self.overview(days * 2)
        out: dict[str, dict[str, float]] = {}
        for key in ("items", "people", "units"):
            now = float(cur.get(key, 0))
            prev = float(prev_total.get(key, 0)) - now
            if prev <= 0:
                delta = None if now <= 0 else 100.0
            else:
                delta = round((now - prev) / prev * 100, 1)
            out[key] = {"now": now, "prev": prev, "delta": delta}
        return out


class WatchRepository:
    """v1.2.0：监控源 watch_sources 表的增删改查。"""

    def all(self, only_enabled: bool = False) -> list["WatchSource"]:
        sql = "SELECT * FROM watch_sources"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at DESC, id DESC"
        return [WatchSource.from_row(r) for r in get_database().query(sql)]

    def get(self, source_id: int) -> "WatchSource | None":
        row = get_database().query_one(
            "SELECT * FROM watch_sources WHERE id = ?", (source_id,))
        return WatchSource.from_row(row) if row else None

    def get_by_url(self, url: str) -> "WatchSource | None":
        row = get_database().query_one(
            "SELECT * FROM watch_sources WHERE url = ?", (url,))
        return WatchSource.from_row(row) if row else None

    def create(self, src: "WatchSource") -> int:
        row = src.to_row()
        row["created_at"] = row["created_at"] or _now()
        keys = ", ".join(row.keys())
        marks = ", ".join(f":{k}" for k in row)
        cur = get_database().execute(
            f"INSERT INTO watch_sources ({keys}) VALUES ({marks})", row
        )
        return int(cur.lastrowid)

    def update(self, src: "WatchSource") -> None:
        if src.id is None:
            return
        row = src.to_row()
        sets = ", ".join(f"{k} = :{k}" for k in row)
        get_database().execute(
            f"UPDATE watch_sources SET {sets} WHERE id = :id", {**row, "id": src.id}
        )

    def save_snapshot(
        self,
        source_id: int,
        title: str = "",
        price: float | None = None,
        deadline: str = "",
        content_hash: str = "",
        item_id: int | None = None,
    ) -> None:
        """抓完一轮后回写最后一次观察到的状态。"""
        get_database().execute(
            "UPDATE watch_sources SET title = ?, last_price = ?, last_deadline = ?, "
            "last_hash = ?, item_id = COALESCE(?, item_id), last_checked = ? "
            "WHERE id = ?",
            (title, price, deadline, content_hash, item_id, _now(), source_id),
        )

    def set_enabled(self, source_id: int, enabled: bool) -> None:
        get_database().execute(
            "UPDATE watch_sources SET enabled = ? WHERE id = ?",
            (int(enabled), source_id),
        )

    def delete(self, source_id: int) -> None:
        get_database().execute("DELETE FROM watch_sources WHERE id = ?", (source_id,))

    def count(self) -> tuple[int, int]:
        """返回 (启用数, 总数)。"""
        row = get_database().query_one(
            "SELECT SUM(enabled) AS on, COUNT(*) AS total FROM watch_sources")
        if not row:
            return 0, 0
        return int(row["on"] or 0), int(row["total"] or 0)


class NotifiedRepository:
    """v1.5.0：已提醒记录，避免同一件事反复弹。

    以前 ReminderService 用内存 set 去重，软件一重启就忘，
    于是同一条内容每天都被提醒一次。落库后跨会话也只提醒一次，
    用户对条目做了处理（比如结清）后可以 forget 掉让它重新提醒。
    """

    def has(self, item_id: int, kind: str) -> bool:
        row = get_database().query_one(
            "SELECT 1 AS x FROM notified WHERE item_id = ? AND kind = ?",
            (item_id, kind),
        )
        return row is not None

    def mark(self, item_id: int, kind: str) -> None:
        get_database().execute(
            "INSERT OR REPLACE INTO notified (item_id, kind, notified_at) "
            "VALUES (?, ?, ?)", (item_id, kind, _now()),
        )

    def forget(self, item_id: int, kind: str | None = None) -> None:
        if kind:
            get_database().execute(
                "DELETE FROM notified WHERE item_id = ? AND kind = ?", (item_id, kind))
        else:
            get_database().execute(
                "DELETE FROM notified WHERE item_id = ?", (item_id,))

    def clear(self, kind: str | None = None) -> None:
        if kind:
            get_database().execute("DELETE FROM notified WHERE kind = ?", (kind,))
        else:
            get_database().execute("DELETE FROM notified")

    def prune(self, keep_days: int = 60) -> int:
        """清掉太久以前的记录，免得表无限长。"""
        edge = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = get_database().execute(
            "DELETE FROM notified WHERE notified_at != '' AND notified_at < ?", (edge,))
        return int(cur.rowcount)


items = ItemRepository()
signups = SignupRepository()
templates = TemplateRepository()
publishes = PublishRepository()
stats = StatsRepository()
watch_sources = WatchRepository()
notified = NotifiedRepository()
