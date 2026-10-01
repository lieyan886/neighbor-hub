"""数据层：时间/价格解析、对外脱敏、配置默认值。"""
from __future__ import annotations

from core import config, privacy
from core.utils import days_left, humanize, parse_datetime, parse_price


def test_parse_price():
    assert parse_price("券后 ￥168.5") == 168.5
    assert parse_price("3元") == 3.0
    assert parse_price("不要钱") is None


def test_parse_datetime():
    dt = parse_datetime("2026-10-05 14:30")
    assert dt is not None and dt.month == 10 and dt.day == 5
    assert parse_datetime("明天 18:00") is not None


def test_humanize_and_days_left():
    assert humanize("2026-10-05"), "口语化结果不应为空"
    assert isinstance(days_left("2026-10-05"), int)


def test_privacy_phone_and_room():
    assert privacy.mask_phone("13812345678") == "138****5678"
    room = privacy.mask_room("3栋502")
    assert "***" in room and "502" not in room
    assert "502" not in privacy.mask_free_text("送到 3栋502，电话13812345678")


def test_privacy_levels_public_vs_export():
    # 群里保留昵称，导出时打码
    assert privacy.mask_name("3栋王姐", privacy.LEVEL_MEDIUM, "public") == "3栋王姐"
    assert privacy.mask_name("张三", privacy.LEVEL_MEDIUM, "export") == "张*"
    assert privacy.mask_name("王小明", privacy.LEVEL_MEDIUM, "export") == "王*明"
    # 严格级：群里也化名，导出直接隐藏联系方式
    assert privacy.mask_name("王姐", privacy.LEVEL_STRICT, "public", 2) == "邻居2"
    assert privacy.mask_contact("13812345678", privacy.LEVEL_STRICT, "export") == "已隐藏"
    # 关闭脱敏：原样返回
    assert privacy.mask_name("张三", privacy.LEVEL_OFF, "export") == "张三"


def test_settings_defaults_cover_new_keys():
    for key in ("privacy_level", "browser_fallback", "auto_download_cover"):
        assert key in config.DEFAULT_SETTINGS, f"默认配置缺 {key}"
