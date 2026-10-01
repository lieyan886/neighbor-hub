"""JS 渲染兜底采集。

httpx 只能拿到服务端直出的 HTML，遇到淘宝 / 拼多多 / 美团这类前端渲染的页面，
价格和标题都藏在 JS 里，抓出来是空壳。这里用 Playwright 开一个真实浏览器把页面
跑完，再交回 link_parser 复用同一套解析逻辑。

设计约束：
- **可选依赖**：没装 playwright、或设置在里关掉，全部函数安全返回失败，不抛异常。
- **不打包**：PyInstaller 产物里不带浏览器内核（体积 200MB+），此功能只在源码
  运行时可用；打包版自动降级为纯 httpx。
- **只读**：不登录、不填表、不点按钮，只 goto + 等网络空闲 + 取 HTML。
"""
from __future__ import annotations

import threading
from typing import Callable

# 一个进程内共用一把锁，避免并发开多个浏览器互相拖慢
_LOCK = threading.Lock()
_CACHE: dict[str, str] = {}

_IMPORT_ERROR = ""
try:  # pragma: no cover - 依赖可选
    from playwright.sync_api import sync_playwright  # type: ignore
except Exception as exc:  # noqa: BLE001 - 任何导入失败都当作"不可用"
    sync_playwright = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def available() -> tuple[bool, str]:
    """返回 (是否可用, 说明)。说明会直接显示到界面上。"""
    if sync_playwright is None:
        return False, f"未安装 playwright（{_IMPORT_ERROR[:60]}）"
    return True, "已就绪"


def _find_browser(pw, launch_timeout_ms: int) -> tuple[object | None, str]:
    """优先用 Chromium，退而用本机 Edge/Chrome 的 channel。

    每次 launch 都必须带 timeout：浏览器二进制缺失时（playwright 版本与
    ms-playwright 里的内核对不上最常见），不带超时会卡很久才失败。
    """
    for name in ("chromium", "chrome", "msedge"):
        try:
            if name == "chromium":
                return pw.chromium.launch(headless=True, timeout=launch_timeout_ms), "chromium"
            return pw.chromium.launch(headless=True, channel=name,
                                      timeout=launch_timeout_ms), name
        except Exception:  # noqa: BLE001 - 换下一个内核
            continue
    return None, ""


def render_html(
    url: str,
    timeout_ms: int = 25_000,
    progress: Callable[[str], None] | None = None,
    launch_timeout_ms: int = 15_000,
) -> tuple[str, str]:
    """打开页面并等待渲染，返回 (html, 引擎名)。失败时 html 为空串。

    :param progress: 可选的进度回调，用于把「启动浏览器 / 加载中」打到界面上。
    :param launch_timeout_ms: 单个内核的启动上限，三个内核都试一遍也不会拖太久。
    """
    ok, why = available()
    if not ok:
        return "", why

    cached = _CACHE.get(url)
    if cached:
        return cached, "cache"

    def emit(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001 - 回调失败不影响主流程
                pass

    with _LOCK:
        try:
            emit("启动浏览器…")
            with sync_playwright() as pw:  # type: ignore[misc]
                browser, engine = _find_browser(pw, launch_timeout_ms)
                if browser is None:
                    return "", "没有可用的浏览器内核（chromium/chrome/msedge 都没起来）"
                try:
                    emit(f"用 {engine} 加载页面…")
                    page = browser.new_page(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"
                        ),
                        locale="zh-CN",
                    )
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:  # noqa: BLE001 - 长轮询页面会一直不空闲，忽略
                        pass
                    page.wait_for_timeout(1200)  # 给前端再渲染一拍
                    emit("读取页面内容…")
                    html = page.content() or ""
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001 - 浏览器世界里的失败太杂，一律降级
            return "", f"浏览器渲染失败：{type(exc).__name__}: {exc}"[:160]

    if html:
        _CACHE[url] = html
    return html, engine


def clear_cache() -> None:
    _CACHE.clear()
