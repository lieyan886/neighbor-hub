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


def _record_price(item_id: int | None, title: str, price, origin_price=None,
                  source: str = "", only_if_changed: bool = False) -> None:
    """记一笔价格历史。

    价格历史是「锦上添花」的旁支数据，写不进去绝不能让主流程失败，
    所以这里把异常全部吞掉。
    """
    if price is None:
        return
    try:
        if only_if_changed:
            prices.record_if_changed(item_id, title, float(price),
                                     float(origin_price) if origin_price else None,
                                     source)
        else:
            prices.add(item_id, title, float(price),
                       float(origin_price) if origin_price else None, source)
    except Exception:
        pass


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
        new_id = int(cur.lastrowid)
        # v1.7.0：入库这一刻的价格就是历史第一笔——不记的话走势图永远是空的
        _record_price(new_id, row.get("title", ""), row.get("price"),
                      row.get("origin_price"), "collect")
        return new_id

    def update(self, item: Item) -> None:
        """整条覆盖更新。

        注意 created_at / published_at：这两个时间戳只在 create() 里写过，
        模型对象上常常还是空字符串。直接拿 to_row() 拼 UPDATE 会把库里
        已经存好的创建时间抹成 '' —— 而 created_at 是看板「近 N 天」
        筛选的唯一定界字段，抹掉之后这条内容会直接从看板里消失。
        所以这里让它们保持原值，除非调用方显式传了。
        """
        row = item.to_row()
        row["updated_at"] = _now()
        row["id"] = item.id
        if not row.get("created_at"):
            old = self.get(item.id)
            row["created_at"] = old.created_at if old else _now()
        if not row.get("published_at"):
            old = self.get(item.id)
            row["published_at"] = old.published_at if old else ""
        # 改价要留痕：团长要靠它判断「这次是真降了还是先涨后降」
        _record_price(item.id, row.get("title", ""), row.get("price"),
                      row.get("origin_price"), "manual", only_if_changed=True)
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
        seen: set[str] = set()          # 本批次内部已经写过的指纹
        with db.transaction():
            for it in items:
                if dedupe and it.source_hash:
                    # 先比库里，再比本批次 —— 以前「批次内去重」那行是死代码
                    # （条件跟上一行一模一样），同一批重复链接照样写进去两遍
                    if it.source_hash in seen or self.hash_exists(it.source_hash):
                        skipped += 1
                        continue
                    seen.add(it.source_hash)
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
            shifted = self._shift_from_now(when, days_shift)
            if src.event_at:
                clone.event_at = shifted
            if src.deadline:
                clone.deadline = shifted
        return clone

    @staticmethod
    def _shift_from_now(when: str, days: int) -> str:
        """把时间往后挪 N 天，但基准不早于今天。

        v1.5.1 修正：以前直接拿旧的截止时间 +7 天。旧团是 9/1 截止、今天 10/5，
        复制出来就成 9/8 —— 新一期一建好就是「已过期」，团长还得手动改日期。
        真实场景里「再来一团」永远是「从今天起再开一周」，所以基准取二者较大值。
        """
        base = utils.parse_datetime(when)
        if base is None:
            return utils.shift_days(when, days)
        now = datetime.now()
        if base < now:
            base = now
        return utils.to_iso(base + timedelta(days=days))

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

    def settlement_overdue(self,
                           idle_days: int | None = None) -> list[tuple[Item, int]]:
        """结算逾期：团已经收尾，但还有人没结清。

        v1.3 做了结算闭环，数据躺在库里却没人提醒 —— 这里把它接上。
        v1.5.1：不再只认 status='expired'，没填截止时间的团靠 _finished 判定，
        否则线下自提那批永远催不到。
        返回 [(条目, 未结清人数), ...]。
        """
        days = DEFAULT_IDLE_DAYS if idle_days is None else idle_days
        rows = get_database().query(
            "SELECT i.*, SUM(CASE WHEN s.settled = 0 THEN 1 ELSE 0 END) AS unpaid "
            "FROM items i JOIN signups s ON s.item_id = i.id "
            "WHERE i.status != 'archived' GROUP BY i.id HAVING unpaid > 0 "
            "ORDER BY unpaid DESC"
        )
        return [(Item.from_row(r), int(r["unpaid"] or 0))
                for r in rows if _finished(r, days)]

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


def _bounds(days: int | None, offset_days: int = 0) -> tuple[str, str]:
    """时间窗口的 (下界, 上界)；任一为空表示这一侧不限。

    offset_days 把整段窗口往前推：offset=days 时正好是「上一个等长周期」，
    环比才能拿到两个**不相交**的窗口（以前用 overview(2d) - overview(d) 相减，
    人数这类 DISTINCT 指标会被抵消成 0，环比直接显示 +100%）。
    """
    end = datetime.now() - timedelta(days=int(offset_days or 0))
    upper = end.strftime("%Y-%m-%d %H:%M:%S") if offset_days else ""
    lower = (end - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S") if days else ""
    return lower, upper


# 没填截止时间的团，开团超过这么多天就当收尾（用于结算催收与成团率）
DEFAULT_IDLE_DAYS = 7


def _finished(row, idle_days: int = DEFAULT_IDLE_DAYS) -> bool:
    """这条内容算不算「已经收尾」。

    真实场景里大量拼单是「线下自提、随到随取」，团长根本不填截止时间 ——
    它们永远不会自动变成 expired，于是就算全员没结清也永远收不到提醒
    （v1.5.0 的结算逾期只认 status='expired'）。规则放宽为三条：
      · 状态已是 expired → 收尾
      · 填了截止/活动时间且已过去 → 收尾
      · 两者都没填 → 开团超过 idle_days 天算收尾
    草稿与归档不算收尾：草稿还没开团，归档是用户主动收起来的。
    """
    keys = row.keys() if hasattr(row, "keys") else []
    status = row["status"] if "status" in keys else ""
    if status in (STATUS_ARCHIVED, STATUS_DRAFT):
        return False
    if status == STATUS_EXPIRED:
        return True
    end = (row["deadline"] if "deadline" in keys else "") or (
        row["event_at"] if "event_at" in keys else "")
    dt = utils.parse_datetime(end) if end else None
    if dt is not None:
        return dt < datetime.now()
    created = row["created_at"] if "created_at" in keys else ""
    cdt = utils.parse_datetime(created) if created else None
    if cdt is None:
        return False
    return (datetime.now() - cdt).days >= max(1, int(idle_days))


class StatsRepository:
    """看板用的聚合查询。

    v1.5.0 起所有方法都支持 days 时间范围（None = 全部），
    并新增成团率（fulfillment）与环比（compare）。
    """

    # —— 总览 ——

    def overview(self, days: int | None = None,
                 offset_days: int = 0) -> dict[str, float]:
        """条目与报名的总览数字。

        v1.5.0 修正：signups 是「报名记录条数」，units 才是「累计份数」。
        以前看板把记录条数当份数显示（一人报 5 份只算 1），现在拆开了。
        v1.5.1：offset_days 让「上一周期」成为一个独立的窗口（见 _bounds）。
        """
        db = get_database()
        lower, upper = _bounds(days, offset_days)
        where, args = " WHERE status != 'archived'", []
        if lower:
            where += " AND created_at >= ?"
            args.append(lower)
        if upper:
            where += " AND created_at < ?"
            args.append(upper)
        row = db.query_one(
            "SELECT COUNT(*) AS items, "
            "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN status = 'ending' THEN 1 ELSE 0 END) AS ending, "
            "SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) AS draft, "
            "SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS expired "
            f"FROM items{where}", args
        )
        # 报名的统计要跟条目同一时间口径：按关联条目的创建时间算
        s_where = " FROM signups s JOIN items i ON i.id = s.item_id WHERE i.status != 'archived'"
        s_args: list = []
        if lower:
            s_where += " AND i.created_at >= ?"
            s_args.append(lower)
        if upper:
            s_where += " AND i.created_at < ?"
            s_args.append(upper)
        total = db.query_one(f"SELECT COUNT(*) AS c{s_where}", s_args)
        # v1.5.1 修正：人数按归一化后的核心名去重，跟报名归并用同一套口径。
        # 以前用 COUNT(DISTINCT name)，「张三」和「3栋张三」算两个人，
        # 而管理台那边已经把它俩合并了 —— 两边数字对不上。
        name_rows = db.query(f"SELECT DISTINCT s.name AS n{s_where}", s_args)
        people_n = len({k for k in (utils.normalize_name(r["n"]) for r in name_rows) if k})
        units = db.query_one(f"SELECT COALESCE(SUM(s.qty), 0) AS c{s_where}", s_args)
        items_n = row["items"] or 0
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
        """各类型的 (已完成份数, 目标份数)，用于画「完成 vs 目标」对比。

        v1.5.1 修正：以前 done 和 target 写在同一次 JOIN 里，一条团有 N 条报名
        就把它的 quota 累加 N 遍（quota=10 + 3 条报名 → 目标显示 30）。
        两个量必须各自独立聚合，不能共用一次扇出的 JOIN。
        """
        since = _since(days)
        args: list = []
        # 字段必须带 i. 前缀：这条 SQL 会 JOIN signups，
        # 两边都有 created_at，裸写会报 ambiguous column name
        where = " WHERE i.status != 'archived'"
        if since:
            where += " AND i.created_at >= ?"
            args.append(since)

        done_rows = get_database().query(
            "SELECT i.kind AS kind, COALESCE(SUM(s.qty), 0) AS done "
            "FROM items i LEFT JOIN signups s ON s.item_id = i.id"
            f"{where} GROUP BY i.kind", args,
        )
        target_rows = get_database().query(
            "SELECT kind, COALESCE(SUM(COALESCE(quota, 0)), 0) AS target "
            "FROM items i"
            f"{where} GROUP BY kind", args,
        )
        done_map = {r["kind"]: float(r["done"] or 0) for r in done_rows}
        target_map = {r["kind"]: float(r["target"] or 0) for r in target_rows}
        out: list[tuple[str, float, float]] = []
        for kind in sorted(set(done_map) | set(target_map)):
            out.append((kind, done_map.get(kind, 0.0), target_map.get(kind, 0.0)))
        return out

    # —— v1.5.0 新增 ——

    def fulfillment(self, days: int | None = None) -> dict[str, float]:
        """成团率：已经收尾的拼单里，有多少真的凑够了目标。

        这是团长最关心的运营指标 —— 发团数不等于成团数。
        v1.5.1 修正：以前把「还在进行中」的团也算进分母并按「没成团」计，
        指标被系统性低估（刚开的团当然还没凑够）。成团率只看**已结束**的团，
        进行中的单独报一个 ongoing 计数，别混在一起。
        """
        lower, upper = _bounds(days, 0)
        where = (" WHERE i.kind = 'groupbuy' AND i.quota > 0 "
                 "AND i.status != 'archived'")
        args: list = []
        if lower:
            where += " AND i.created_at >= ?"
            args.append(lower)
        if upper:
            where += " AND i.created_at < ?"
            args.append(upper)
        rows = get_database().query(
            "SELECT i.id, i.quota, i.status, i.deadline, i.event_at, i.created_at, "
            "COALESCE(SUM(s.qty), 0) AS got "
            "FROM items i LEFT JOIN signups s ON s.item_id = i.id"
            f"{where} GROUP BY i.id", args,
        )
        finished = [r for r in rows if _finished(r, DEFAULT_IDLE_DAYS)]
        ongoing = len(rows) - len(finished)
        total = len(finished)
        formed = sum(1 for r in finished
                     if float(r["got"]) >= float(r["quota"] or 0))
        return {
            "total": float(total),
            "formed": float(formed),
            "ongoing": float(ongoing),
            "rate": round(formed / total * 100, 1) if total else 0.0,
        }

    def compare(self, days: int = 30) -> dict[str, dict[str, float]]:
        """本期 vs 上一期的环比。

        返回 {'items': {'now': x, 'prev': y, 'delta': 百分比}, ...}
        delta 为 None 表示上期是 0（没法算增长率），UI 显示「新增」即可。
        """
        cur = self.overview(days, offset_days=0)
        prev_win = self.overview(days, offset_days=days)
        out: dict[str, dict[str, float]] = {}
        for key in ("items", "people", "units"):
            now = float(cur.get(key, 0))
            prev = float(prev_win.get(key, 0))
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

    def age_hours(self, item_id: int, kind: str) -> float | None:
        """距上次提醒过了多少小时；没提醒过返回 None。"""
        row = get_database().query_one(
            "SELECT notified_at FROM notified WHERE item_id = ? AND kind = ?",
            (item_id, kind),
        )
        if not row or not row["notified_at"]:
            return None
        dt = utils.parse_datetime(row["notified_at"])
        if dt is None:
            return None
        return (datetime.now() - dt).total_seconds() / 3600.0

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


class PriceHistoryRepository:
    """v1.7.0：价格历史。

    「这次是真便宜还是先涨后降」——只看当前价永远答不了这个问题。
    三条记账入口：采集入库、手动改价、盯梢抓到变价。
    """

    def add(self, item_id: int | None, title: str = "", price: float | None = None,
            origin_price: float | None = None, source: str = "",
            noted_at: str = "") -> int:
        """记一笔价格。价格为空就不记（没法画到图上，还得污染统计）。"""
        if price is None:
            return 0
        cur = get_database().execute(
            "INSERT INTO price_history (item_id, title, price, origin_price, "
            "source, noted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, title or "", float(price),
             float(origin_price) if origin_price else None,
             source or "manual", noted_at or _now()),
        )
        return int(cur.lastrowid)

    def record_if_changed(self, item_id: int | None, title: str,
                          price: float | None, origin_price: float | None = None,
                          source: str = "") -> bool:
        """价格跟上一笔不一样才记，避免每次保存条目都刷一堆重复点。"""
        if price is None:
            return False
        last = self.last_price(item_id, title)
        if last is not None and abs(last - float(price)) < 0.009:
            return False
        return self.add(item_id, title, price, origin_price, source) > 0

    def last_price(self, item_id: int | None, title: str = "") -> float | None:
        if item_id:
            row = get_database().query_one(
                "SELECT price FROM price_history WHERE item_id = ? "
                "ORDER BY noted_at DESC, id DESC LIMIT 1", (item_id,))
        else:
            row = get_database().query_one(
                "SELECT price FROM price_history WHERE item_id IS NULL AND title = ? "
                "ORDER BY noted_at DESC, id DESC LIMIT 1", (title,))
        if not row or row["price"] is None:
            return None
        return float(row["price"])

    def series(self, item_id: int | None = None, title: str = "",
               limit: int = 60) -> list[tuple[str, float]]:
        """某条内容的价格序列（按时间正序），返回 [(时间点, 价格), ...]。"""
        if item_id:
            rows = get_database().query(
                "SELECT noted_at, price FROM price_history WHERE item_id = ? "
                "ORDER BY noted_at ASC, id ASC LIMIT ?", (item_id, int(limit)))
        else:
            rows = get_database().query(
                "SELECT noted_at, price FROM price_history "
                "WHERE item_id IS NULL AND title = ? "
                "ORDER BY noted_at ASC, id ASC LIMIT ?", (title, int(limit)))
        return [(r["noted_at"] or "", float(r["price"] or 0)) for r in rows]

    def subjects(self, limit: int = 50) -> list[tuple[str, str, int]]:
        """有哪些内容攒下了价格历史，返回 [(key, 显示名, 点数), ...]。

        key 形如 `item:12` 或 `title:山姆牛肉卷`（监控源没入库时的兜底）。
        """
        rows = get_database().query(
            "SELECT item_id, title, COUNT(*) AS c, MAX(noted_at) AS last "
            "FROM price_history GROUP BY item_id, title "
            "ORDER BY last DESC LIMIT ?", (int(limit),))
        out: list[tuple[str, str, int]] = []
        for r in rows:
            iid = r["item_id"]
            if iid:
                key = f"item:{iid}"
                label = (r["title"] or "").strip()
                if not label:
                    it = items.get(int(iid))
                    label = it.title if it else f"#{iid}"
            else:
                key = f"title:{r['title']}"
                label = f"{r['title']}（监控源）"
            out.append((key, label, int(r["c"])))
        return out

    def summary(self, item_id: int | None = None, title: str = "") -> dict[str, float]:
        """当前价 / 最高 / 最低 / 首价，用于给走势图配一句人话结论。"""
        seq = [p for _, p in self.series(item_id, title)]
        if not seq:
            return {}
        return {
            "now": seq[-1],
            "high": max(seq),
            "low": min(seq),
            "first": seq[0],
            "points": float(len(seq)),
        }

    def prune(self, keep_days: int = 365) -> int:
        edge = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = get_database().execute(
            "DELETE FROM price_history WHERE noted_at != '' AND noted_at < ?", (edge,))
        return int(cur.rowcount)


items = ItemRepository()
signups = SignupRepository()
templates = TemplateRepository()
publishes = PublishRepository()
stats = StatsRepository()
watch_sources = WatchRepository()
notified = NotifiedRepository()
prices = PriceHistoryRepository()
