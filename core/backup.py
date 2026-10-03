"""数据备份与恢复。

**为什么不能直接把 data/ 目录拷走**：数据库开着 WAL 模式，`.db` 与 `-wal`
日志是分离的，程序正在跑的时候直接复制目录，很可能拿到「缺最后一段写入」
的不一致文件——拷到另一台机器上有打不开的风险。

所以这里用 SQLite 官方的 `backup` API 先导出一份**一致性快照**，再把
数据库 + 配置 + 封面缓存一起打包成 zip。恢复时反向解包，并在覆盖前把当前
数据留一份档，避免误操作无法回退。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

_DB_NAME = "community.db"
_SETTINGS_NAME = "settings.json"
_COVER_DIR_NAME = "covers"
_MANIFEST_NAME = "manifest.json"
_BACKUP_PREFIX = "邻里圈备份"


# ===========================================================================
# 备份
# ===========================================================================

def _snapshot_db(target: Path) -> None:
    """用 SQLite backup API 导出一致性快照到 target（会覆盖同名文件）。"""
    from .db import get_database

    src = get_database().conn
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()


def _counts() -> dict[str, int]:
    """快照里的关键数据量，写进 manifest 便于恢复前核对。"""
    from .db import get_database

    conn = get_database().conn
    out: dict[str, int] = {}
    for table in ("items", "signups", "card_templates", "publish_logs", "watch_sources"):
        try:
            out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:
            out[table] = 0
    return out


def create_backup(dest_dir: str | Path | None = None) -> str:
    """打包一份完整备份（数据库快照 + 配置 + 封面缓存），返回 zip 路径。"""
    config.ensure_dirs()
    dest = Path(dest_dir or config.EXPORT_DIR)
    dest.mkdir(parents=True, exist_ok=True)

    zip_path = dest / f"{_BACKUP_PREFIX}_{datetime.now():%Y%m%d_%H%M%S}.zip"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nh-backup-"))
    try:
        snap = tmp_dir / _DB_NAME
        _snapshot_db(snap)

        cover_count = 0
        if config.COVER_DIR.exists():
            cover_count = sum(1 for f in config.COVER_DIR.rglob("*") if f.is_file())

        manifest: dict[str, Any] = {
            "app": config.APP_NAME,
            "app_version": config.APP_VERSION,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "counts": _counts(),
            "cover_count": cover_count,
        }

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, _DB_NAME)
            zf.writestr(_MANIFEST_NAME,
                        json.dumps(manifest, ensure_ascii=False, indent=2))
            if config.SETTINGS_PATH.exists():
                zf.write(config.SETTINGS_PATH, _SETTINGS_NAME)
            if config.COVER_DIR.exists():
                for f in sorted(config.COVER_DIR.rglob("*")):
                    if f.is_file():
                        arc = f"{_COVER_DIR_NAME}/{f.relative_to(config.COVER_DIR).as_posix()}"
                        zf.write(f, arc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return str(zip_path)


def read_manifest(zip_path: str | Path) -> dict[str, Any]:
    """读备份包里的清单，恢复前给用户看一眼再确认。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if _MANIFEST_NAME not in zf.namelist():
                return {}
            return json.loads(zf.read(_MANIFEST_NAME).decode("utf-8"))
    except Exception:
        return {}


# ===========================================================================
# 恢复
# ===========================================================================

def _keep_current_snapshot() -> str:
    """覆盖前把当前数据库与配置留一份档，返回留档路径说明。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    kept: list[str] = []
    if config.DB_PATH.exists():
        guard = config.DB_PATH.with_name(f"{config.DB_PATH.stem}.before-restore-{stamp}.db")
        shutil.copy2(config.DB_PATH, guard)
        kept.append(guard.name)
    if config.SETTINGS_PATH.exists():
        guard_s = config.SETTINGS_PATH.with_name(
            f"{config.SETTINGS_PATH.stem}.before-restore-{stamp}.json")
        shutil.copy2(config.SETTINGS_PATH, guard_s)
        kept.append(guard_s.name)
    return "、".join(kept) or "（当前没有可留档的数据）"


def restore_backup(zip_path: str | Path) -> tuple[bool, str]:
    """从备份包恢复。返回 (是否成功, 说明)。

    注意：恢复会断开当前数据库连接并重建，调用方（UI）需要在之后刷新界面。
    """
    src = Path(zip_path)
    if not src.exists():
        return False, "备份文件不存在"
    if not zipfile.is_zipfile(src):
        return False, "这不是有效的备份文件（需要 .zip）"

    tmp = Path(tempfile.mkdtemp(prefix="nh-restore-"))
    try:
        with zipfile.ZipFile(src) as zf:
            names = zf.namelist()
            if _DB_NAME not in names:
                return False, "备份包里没有数据库文件，无法恢复"
            # 防 zip slip：任何成员都不能逃出解压目录
            root = str(tmp.resolve())
            for n in names:
                if not str((Path(root) / n).resolve()).startswith(root):
                    return False, "备份包结构异常，已中止恢复"
            zf.extractall(tmp)

        kept = _keep_current_snapshot()

        # 先断开连接，否则 Windows 上无法覆盖正在使用的 db 文件
        from .db import reset_database

        reset_database()
        config.ensure_dirs()

        shutil.copy2(tmp / _DB_NAME, config.DB_PATH)
        # 快照是完整库，不需要旧 WAL 残留
        for suffix in ("-wal", "-shm"):
            stale = Path(str(config.DB_PATH) + suffix)
            if stale.exists():
                stale.unlink()

        settings_file = tmp / _SETTINGS_NAME
        if settings_file.exists():
            shutil.copy2(settings_file, config.SETTINGS_PATH)
        config.reload_settings()

        covers = tmp / _COVER_DIR_NAME
        if covers.exists():
            config.COVER_DIR.mkdir(parents=True, exist_ok=True)
            for f in covers.rglob("*"):
                if f.is_file():
                    target = config.COVER_DIR / f.relative_to(covers)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
    except Exception as exc:  # 恢复失败不能把程序带崩
        return False, f"恢复失败：{exc}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return True, f"恢复完成。覆盖前的数据已留档：{kept}"
