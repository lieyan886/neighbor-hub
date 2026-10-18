# -*- coding: utf-8 -*-
"""v1.8.1：分发记录可查看 + 备份提示补隐私警示。"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from core.db import Database
from core.models import Item
from core.repository import items as item_repo
from core.repository import publishes as publish_repo


def _mk(title: str) -> int:
    it = Item(title=title, kind="groupbuy", unit="份", source_hash=f"v181-{title}")
    return item_repo.create(it)


def test_publish_log_recent_joins_title(isolated_data):
    """分发记录能带着内容标题一起读出来（查看对话框的数据来源）。"""
    item_id = _mk("留痕测试")
    publish_repo.add(item_id, "wechat", text="一键推群")
    logs = publish_repo.recent(limit=20)
    assert logs, "分发记录没写进去"
    row = logs[0]
    assert row["title"] == "留痕测试"
    assert row["channel"] == "wechat"


def test_publish_log_survives_item_delete(isolated_data):
    """内容删了，历史留痕还在（标题显示「已删除」而不是崩）。"""
    item_id = _mk("删了还能查")
    publish_repo.add(item_id, "wechat", text="手动记录")
    item_repo.delete(item_id)
    logs = publish_repo.recent(limit=20)
    assert any(r["title"] is None for r in logs), \
        "级联删除后标题应为 None，界面显示「（内容已删除）」"


def test_publish_log_dialog_builds(qapp):
    """对话框能在无头环境建起来并载入数据。"""
    from ui.publish_log_dialog import PublishLogDialog

    dlg = PublishLogDialog()
    assert dlg.table.columnCount() == 4
    assert dlg.table.horizontalHeaderItem(1).text() == "内容"
    dlg.close()


def test_publish_log_dialog_lists_rows(qapp, isolated_data):
    """有记录时表格行数对得上。"""
    from ui.publish_log_dialog import PublishLogDialog

    item_id = _mk("对话框测试")
    publish_repo.add(item_id, "wechat", text="一键推群")
    publish_repo.add(item_id, "moments", text="手动记录")
    dlg = PublishLogDialog()
    assert dlg.table.rowCount() >= 2
    dlg.close()


def test_v5_database_migrates_publish_logs(tmp_path):
    """v5 老库（item_id NOT NULL + CASCADE）升级后：留痕熬过内容被删。

    造一个手写的 v5 库，走项目的 initialize 迁移，验证表重建 + SET NULL 生效。
    """
    from core.db import reset_database

    old = tmp_path / "v5.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
    CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'deal', summary TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', cover TEXT NOT NULL DEFAULT '',
        merchant TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', price REAL,
        origin_price REAL, unit TEXT NOT NULL DEFAULT '', quota INTEGER NOT NULL DEFAULT 0,
        event_at TEXT NOT NULL DEFAULT '', deadline TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft', tags TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', source_hash TEXT NOT NULL DEFAULT '',
        collected INTEGER NOT NULL DEFAULT 0, published_at TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
    CREATE TABLE publish_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        channel TEXT NOT NULL DEFAULT 'wechat', image_path TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '');
    CREATE INDEX idx_logs_item ON publish_logs(item_id);
    INSERT INTO items (title, source_hash) VALUES ('老数据团购', 'old-1');
    INSERT INTO publish_logs (item_id, channel, text, created_at)
        VALUES (1, 'wechat', '一键推群', '2026-10-01 10:00:00');
    PRAGMA user_version = 5;
    """)
    conn.close()

    reset_database()
    db = Database(old)          # 打开即触发 initialize → _migrate
    try:
        cols = {r["name"]: r for r in db.conn.execute("PRAGMA table_info(publish_logs)")}
        assert cols["item_id"]["notnull"] == 0, "item_id 应改成可空"
        rows = db.query(
            "SELECT l.*, i.title FROM publish_logs l "
            "LEFT JOIN items i ON i.id = l.item_id")
        assert len(rows) == 1 and rows[0]["title"] == "老数据团购", "历史留痕要保住"
        db.execute("DELETE FROM items WHERE id = 1")
        left = db.query("SELECT * FROM publish_logs")
        assert len(left) == 1, "删内容后留痕必须还在（SET NULL）"
    finally:
        db.close()
        reset_database()
