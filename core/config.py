"""应用路径与运行时配置。

所有数据默认落在项目根目录下的 data/ 里，方便整目录拷走备份。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

# --- 路径常量 ---------------------------------------------------------------
APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
COVER_DIR = DATA_DIR / "covers"          # 采集到的头图缓存
EXPORT_DIR = APP_ROOT / "exports"        # Excel / 导出卡片存放目录
OUTPUT_DIR = APP_ROOT / "output"         # 渲染产出的分享卡片图
DB_PATH = DATA_DIR / "community.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "community_name": "我们小区",          # 用于分享卡片抬头与接龙文案
    "operator_name": "团长",               # 发布者署名
    "contact_info": "",                    # 联系方式，渲染到卡片底部
    # 对外脱敏等级：off / medium / strict，见 core/privacy.py
    "privacy_level": "medium",
    # —— 采集相关 ——
    "request_timeout": 12,                 # 抓取单个页面超时（秒）
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "auto_download_cover": True,           # 采集时是否把头图存到本地
    "blacklist": [],                       # 标题命中任一关键词则丢弃
    "whitelist": [],                       # 非空时，只保留命中关键词的条目
    # —— 提醒相关 ——
    "scheduler_enabled": True,
    "remind_hours": 24,                    # 截止前多少小时进入「即将截止」
    "scan_interval_minutes": 30,           # 定时扫描间隔
    # —— 渲染相关 ——
    "default_template_id": None,
    "window_width": 1280,
    "window_height": 820,
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def ensure_dirs() -> None:
    """确保运行时需要的目录全部存在。"""
    for d in (DATA_DIR, COVER_DIR, EXPORT_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict[str, Any]:
    """读取配置（内存缓存 + 磁盘合并默认值）。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data = dict(DEFAULT_SETTINGS)
        if SETTINGS_PATH.exists():
            try:
                data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                # 配置损坏时静默回退到默认值，不阻塞启动
                pass
        _cache = data
        return _cache


def save_settings(patch: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并写入配置并落盘，返回最新配置。"""
    global _cache
    with _lock:
        data = load_settings()
        if patch:
            data.update(patch)
        ensure_dirs()
        SETTINGS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _cache = dict(data)
        return _cache


def reset_settings() -> dict[str, Any]:
    """恢复出厂配置。"""
    global _cache
    with _lock:
        _cache = dict(DEFAULT_SETTINGS)
    return save_settings(_cache)
