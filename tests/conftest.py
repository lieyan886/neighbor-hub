"""pytest 公共夹具。

要点：
1. 全程 offscreen，不弹窗，CI 里也能跑；
2. 数据目录重定向到临时目录，绝不碰用户真实数据；
3. 种子数据（活动 / 拼单 / 优惠各一条）供各用例复用。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session", autouse=True)
def isolated_data(tmp_path_factory):
    """把 core.config 的路径常量全部指向临时目录。"""
    tmp = Path(tempfile.mkdtemp(prefix="community-hub-pytest-"))
    from core import config

    config.DATA_DIR = tmp / "data"
    config.COVER_DIR = config.DATA_DIR / "covers"
    config.EXPORT_DIR = tmp / "exports"
    config.OUTPUT_DIR = tmp / "output"
    config.DB_PATH = config.DATA_DIR / "test.db"
    config.SETTINGS_PATH = config.DATA_DIR / "settings.json"
    config.ensure_dirs()

    from core.db import get_database

    get_database(config.DB_PATH).initialize()
    return tmp


@pytest.fixture(scope="session")
def seed(isolated_data):
    """灌三条种子数据，返回 [活动, 拼单, 优惠]。"""
    from core.models import KIND_GROUPBUY, Item
    from core.repository import items as item_repo

    rows = [
        Item(title="周六亲子观影", kind="event", summary="小区活动室，免费报名",
             event_at="2026-10-07 15:00", deadline="2026-10-06 12:00",
             quota=30, unit="人", tags="亲子活动,免费", source_hash="test-event"),
        Item(title="山姆牛肉卷拼单", kind=KIND_GROUPBUY, price=168.0, origin_price=199.0,
             unit="份", quota=20, merchant="山姆", location="3栋架空层自提",
             deadline="2026-10-05", tags="生鲜果蔬", source_hash="test-groupbuy"),
        Item(title="包子铺豆浆买一送一", kind="deal", price=3.0,
             merchant="老王包子铺", tags="餐饮", source_hash="test-deal"),
    ]
    item_repo.bulk_insert(rows, dedupe=False)
    return item_repo.list_items(include_archived=True)


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication —— 重复创建会崩。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app
