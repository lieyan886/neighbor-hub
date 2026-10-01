"""邻里圈 · 社群运营工作台 —— 程序入口。

用法：
    python app.py                 启动界面
    python app.py --selftest      跑一遍核心链路自检（不出界面）

打包成 exe（--windowed）后没有控制台，自检默认弹窗展示报告；
设环境变量 NEIGHBORHUB_SELFTEST_FILE=<路径> 可改成写文件（CI / 无人值守用）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from core import config
from core.db import get_database


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    config.ensure_dirs()

    if "--selftest" in argv:
        from tools.selftest import run_selftest

        # 打包版（--windowed）没有控制台，报告只能走文件或弹窗
        out_file = os.environ.get("NEIGHBORHUB_SELFTEST_FILE", "")
        if sys.stdout is not None and not out_file:
            # 源码运行：正常打到控制台
            return run_selftest()

        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_selftest()

        if out_file:
            Path(out_file).write_text(buf.getvalue(), encoding="utf-8")
            return code

        from PySide6.QtWidgets import QApplication, QMessageBox, QTextEdit

        app = QApplication([sys.argv[0]])
        box = QMessageBox()
        box.setWindowTitle("邻里圈 · 自检报告")
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(buf.getvalue())
        view.setMinimumSize(720, 480)
        box.layout().addWidget(view)
        box.exec()
        return code

    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.theme import apply as apply_theme

    get_database().initialize()

    app = QApplication([sys.argv[0]] + [a for a in argv])
    app.setApplicationName("邻里圈 · 社群运营工作台")
    apply_theme(app)

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
