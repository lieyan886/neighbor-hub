"""桌面通知：优先走系统托盘气泡，没有 Qt 环境时静默降级。

为什么不直接用 QSystemTrayIcon：core 层刻意不依赖 Qt（见 core/scheduler.py
的注释），命令行脚本和单元测试要能直接跑。所以这里延迟导入 PySide6，
拿不到就返回 False，调用方负责决定是否再补 UI 提示。
"""
from __future__ import annotations

from typing import Any

_TRAY_ICON = None          # 全局唯一的 QSystemTrayIcon
_SUPPORTED: bool | None = None


def available() -> bool:
    """当前环境能不能弹系统通知。"""
    global _SUPPORTED
    if _SUPPORTED is None:
        try:
            from PySide6.QtWidgets import QSystemTrayIcon  # noqa: F401
        except ImportError:
            _SUPPORTED = False
        else:
            _SUPPORTED = True
    return _SUPPORTED


def icon(owner: Any = None):
    """创建/返回托盘图标对象，UI 层用它挂右键菜单。"""
    global _TRAY_ICON
    if not available():
        return None
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QSystemTrayIcon

    if _TRAY_ICON is None:
        pix = app_icon_pixmap()
        _TRAY_ICON = QSystemTrayIcon(QIcon(pix) if pix else QIcon(), owner)
        _TRAY_ICON.setToolTip("邻里圈")
    return _TRAY_ICON


def show_tray(owner: Any = None, menu: Any = None, tooltip: str = "邻里圈") -> bool:
    """显示托盘图标（含可选右键菜单），返回是否成功。"""
    tray = icon(owner)
    if tray is None:
        return False
    tray.setToolTip(tooltip)
    if menu is not None:
        tray.setContextMenu(menu)
    tray.show()
    return True


def hide_tray() -> None:
    """隐藏托盘图标。"""
    if _TRAY_ICON is not None:
        try:
            _TRAY_ICON.hide()
        except Exception:
            pass


def notify(title: str, message: str, timeout_ms: int = 8000) -> bool:
    """弹一条桌面通知。返回是否真的弹出来了。"""
    if not available():
        return False
    tray = icon()
    if tray is None:
        return False
    try:
        from PySide6.QtWidgets import QSystemTrayIcon

        tray.showMessage(title, message, QSystemTrayIcon.Information, timeout_ms)
        return True
    except Exception:
        return False


def app_icon_pixmap():
    """取一张能用的图标：优先 assets/app.ico|.png，没有就用程序自带窗口图标。"""
    try:
        from core import config

        for name in ("app.ico", "app.png", "icon.ico", "icon.png"):
            p = config.APP_ROOT / "assets" / name
            if p.exists():
                from PySide6.QtGui import QPixmap

                pm = QPixmap(str(p))
                if not pm.isNull():
                    return pm
    except Exception:
        pass
    return None
