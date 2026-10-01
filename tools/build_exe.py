"""打包脚本：把邻里圈打成 dist/邻里圈/ 目录形态（启动快、免安装）。

用法：
    .venv/Scripts/python.exe tools/build_exe.py

产物：dist/邻里圈/邻里圈.exe
体积大是 PySide6 + matplotlib 的正常水平（~200MB），属预期。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "邻里圈"


def main() -> int:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ROOT / "assets" / "icon.ico"),
        "--collect-all", "matplotlib",
        "--exclude-module", "tkinter",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PyQt6",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        "--exclude-module", "setuptools",   # pyinstaller 自身依赖，运行时用不到
        str(ROOT / "app.py"),
    ]
    print("RUN:", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode == 0:
        exe = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe"
        size_mb = exe.stat().st_size / 1024 / 1024 if exe.exists() else 0
        print(f"\n打包完成：{exe}（主程序 {size_mb:.1f} MB）")
        print(f"整个 dist/{APP_NAME}/ 目录拷走即可用。")
    else:
        print(f"\n打包失败，exit={r.returncode}")
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
