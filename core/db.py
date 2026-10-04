"""SQLite 连接与 schema 管理。

设计原则：
1. 单一连接 + WAL 模式，桌面程序够用且不易损坏；
2. schema 用 user_version 做版本迁移，后续加字段不用清库；
3. 所有写操作走本模块的 transaction() 上下文，统一提交/回滚。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config
from .models import stamp

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'deal',
    summary       TEXT    NOT NULL DEFAULT '',
    url           TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',
    cover         TEXT    NOT NULL DEFAULT '',
    merchant      TEXT    NOT NULL DEFAULT '',
    location      TEXT    NOT NULL DEFAULT '',
    price         REAL,
    origin_price  REAL,
    unit          TEXT    NOT NULL DEFAULT '',
    quota         INTEGER NOT NULL DEFAULT 0,
    event_at      TEXT    NOT NULL DEFAULT '',
    deadline      TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'draft',
    tags          TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    source_hash   TEXT    NOT NULL DEFAULT '',
    collected     INTEGER NOT NULL DEFAULT 0,
    published_at  TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT '',
    updated_at    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_items_kind      ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_status    ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_deadline  ON items(deadline);
CREATE INDEX IF NOT EXISTS idx_items_hash      ON items(source_hash);

CREATE TABLE IF NOT EXISTS signups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL DEFAULT '',
    contact     TEXT    NOT NULL DEFAULT '',
    qty         REAL    NOT NULL DEFAULT 1,
    unit        TEXT    NOT NULL DEFAULT '份',
    note        TEXT    NOT NULL DEFAULT '',
    settled     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_signups_item ON signups(item_id);

CREATE TABLE IF NOT EXISTS card_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL DEFAULT '默认模板',
    background    TEXT    NOT NULL DEFAULT '#FAF7F0',
    accent        TEXT    NOT NULL DEFAULT '#D85A30',
    title_color   TEXT    NOT NULL DEFAULT '#2C2C2A',
    body_color    TEXT    NOT NULL DEFAULT '#5F5E5A',
    show_price    INTEGER NOT NULL DEFAULT 1,
    show_deadline INTEGER NOT NULL DEFAULT 1,
    show_qr       INTEGER NOT NULL DEFAULT 1,
    show_tags     INTEGER NOT NULL DEFAULT 1,
    footer        TEXT    NOT NULL DEFAULT '',
    width         INTEGER NOT NULL DEFAULT 900,
    height        INTEGER NOT NULL DEFAULT 1200,
    created_at    TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS publish_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    channel     TEXT    NOT NULL DEFAULT 'wechat',
    image_path  TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT ''
);

-- v1.2.0：监控源（自动盯梢）
CREATE TABLE IF NOT EXISTS watch_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL DEFAULT '',
    last_price    REAL,
    last_deadline TEXT    NOT NULL DEFAULT '',
    last_hash     TEXT    NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    item_id       INTEGER REFERENCES items(id) ON DELETE SET NULL,
    note          TEXT    NOT NULL DEFAULT '',
    last_checked  TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_watch_enabled ON watch_sources(enabled);

CREATE INDEX IF NOT EXISTS idx_logs_item ON publish_logs(item_id);

-- v1.5.0：已提醒记录。以前只在内存里存一个 set，软件一重启就忘，
-- 于是同一条内容会被重复弹好几次。落库后跨会话也只提醒一次。
CREATE TABLE IF NOT EXISTS notified (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL,          -- due / formation / settle
    notified_at TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notified_unique
    ON notified(item_id, kind);
CREATE INDEX IF NOT EXISTS idx_notified_kind ON notified(kind);
"""

DEFAULT_TEMPLATE_SQL = """
INSERT OR IGNORE INTO card_templates
    (id, name, background, accent, title_color, body_color,
     show_price, show_deadline, show_qr, show_tags, footer, width, height, created_at)
VALUES
    (1, '暖阳（默认）', '#FAF7F0', '#D85A30', '#2C2C2A', '#5F5E5A',
     1, 1, 1, 1, '', 900, 1200, :ts),
    (2, '邻里绿',       '#EFF5EE', '#1D9E75', '#173404', '#3B6D11',
     1, 1, 1, 1, '', 900, 1200, :ts),
    (3, '夜航 room',    '#1E1E22', '#7F77DD', '#F1EFE8', '#B4B2A9',
     1, 1, 1, 1, '', 900, 1200, :ts);
"""


class Database:
    """轻量数据库封装，全局单例由 get_database() 提供。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self.initialize()

    # —— 连接管理 ——

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def initialize(self) -> None:
        """建表 + 按版本增量迁移 + 灌默认数据。

        注意：这里刻意不用 transaction() 包裹 executescript —— SQLite 的
        executescript 会先隐式 COMMIT，套在显式事务里反而会报
        "cannot commit - no transaction is active"。
        """
        self.conn.executescript(SCHEMA_SQL)
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.conn.execute(
                "INSERT OR IGNORE INTO card_templates (name, created_at) VALUES (?, ?)",
                ("占位", stamp()),
            )
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.execute(DEFAULT_TEMPLATE_SQL, {"ts": stamp()})
        self.conn.execute("DELETE FROM card_templates WHERE name = '占位'")

    # —— 事务 ——

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """自动 BEGIN / COMMIT / ROLLBACK 的写事务。

        支持嵌套调用：已经在一个事务里时不再 BEGIN，由外层负责提交。
        """
        conn = self.conn
        nested = conn.in_transaction
        try:
            if not nested:
                conn.execute("BEGIN")
            yield conn
            if not nested:
                conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    # —— 便捷查询 ——

    def query(self, sql: str, params: dict | tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: dict | tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: dict | tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq) -> sqlite3.Cursor:
        return self.conn.executemany(sql, seq)


_db: Database | None = None


def get_database(path: Path | str | None = None) -> Database:
    """获取全局数据库实例（首次调用时创建并建表）。"""
    global _db
    if _db is None:
        _db = Database(path)
    return _db


def reset_database() -> None:
    """关闭并清空连接缓存，下次访问会重建。"""
    global _db
    if _db is not None:
        _db.close()
        _db = None
