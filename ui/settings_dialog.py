"""设置对话框：小区身份、提醒策略、素材偏好。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import backup, config, privacy
from ui import theme
from ui.widgets import confirm, hint_label, info, set_placeholder, warn

_INTERVALS = (("15 分钟", 15), ("30 分钟", 30), ("1 小时", 60), ("2 小时", 120))


class SettingsDialog(QDialog):
    """改完立即生效，重启后依然保留。"""

    # 从备份恢复后库已经换了一份，所有面板都得重读 —— 主窗口接这个信号
    restored = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(540, 820)
        self.setStyleSheet(theme.QSS)
        self.cfg = dict(config.load_settings())
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        form = QFormLayout()
        form.setSpacing(12)

        self.community_edit = QLineEdit()
        set_placeholder(self.community_edit, "会印在卡片和文案上，建议用别名而非真实小区全名")
        self.operator_edit = QLineEdit()
        set_placeholder(self.operator_edit, "如「3栋小李」；接龙里的「我要2份」也会记到这个名字下")
        self.contact_edit = QLineEdit()
        set_placeholder(self.contact_edit, "微信号或手机号，会渲染到卡片底部")

        self.enable_box = QCheckBox("开启定时提醒")
        self.remind_spin = QSpinBox()
        self.remind_spin.setRange(1, 96)
        self.remind_spin.setSuffix(" 小时内算即将截止")
        self.interval_combo = QComboBox()
        for label, val in _INTERVALS:
            self.interval_combo.addItem(label, val)
        # 结算逾期跟「快截止了」不同：不处理就一直成立，只提醒一次等于放弃催收
        self.settle_spin = QSpinBox()
        self.settle_spin.setRange(0, 168)
        self.settle_spin.setSuffix(" 小时后重催一次（0 = 只提醒一次）")
        self.settle_spin.setToolTip(
            "拼单结束后还有人没结清时，每隔这么久再提醒一次。"
            "设为 0 表示只提醒一次，不再重复。")

        self.privacy_combo = QComboBox()
        for lv in privacy.LEVELS:
            self.privacy_combo.addItem(privacy.LEVEL_LABELS[lv], lv)

        self.cover_box = QCheckBox("采集时自动下载头图")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(3, 60)
        self.timeout_spin.setSuffix(" 秒")
        self.browser_box = QCheckBox("抓不到时用浏览器渲染再抓一次")
        from collectors import browser_parser

        ok, why = browser_parser.available()
        self.browser_box.setToolTip(
            f"Playwright 状态：{why}\n"
            "遇到淘宝/拼多多这类前端渲染的页面，httpx 只能拿到空壳，"
            "开真实浏览器把 JS 跑完再解析会准很多，代价是每条慢几秒。\n"
            "打包版不带浏览器内核，此开关自动失效。"
        )
        self.browser_box.setEnabled(ok)
        # —— v1.2.0：后台常驻与自动盯梢 ——
        # 注意：这些控件必须在 _build_ui 里建好；之前误放在 _load 里，
        # 而 _load 开头就要读它们，导致打开设置直接 AttributeError 崩溃。
        from core import notifier

        self.tray_box = QCheckBox("显示系统托盘图标")
        self.tray_box.setToolTip("关掉后软件缩到后台就找不回来了，不建议关。")
        self.tray_box.setEnabled(notifier.available())
        self.notify_box = QCheckBox("截止/盯梢有新动静时弹桌面通知")
        self.minimize_box = QCheckBox("点关闭按钮时缩到托盘而不是退出")
        self.watch_box = QCheckBox("开启后台自动盯梢")
        self.watch_spin = QSpinBox()
        self.watch_spin.setRange(1, 72)
        self.watch_spin.setSuffix(" 小时扫一轮")
        self.watch_import_box = QCheckBox("盯梢抓到更新就直接入库")
        self.watch_import_box.setToolTip(
            "默认只提醒不动数据。打开后，监控源第一次抓到会自动建一条草稿，"
            "之后价格/截止有变会同步更新那条内容。"
        )

        # —— v1.3.0：数据备份与恢复 ——
        backup_box = QWidget()
        backup_row = QHBoxLayout(backup_box)
        backup_row.setContentsMargins(0, 0, 0, 0)
        backup_row.setSpacing(8)
        self.backup_btn = QPushButton("导出备份…")
        self.backup_btn.setProperty("accent", True)
        self.backup_btn.clicked.connect(self._backup)
        self.restore_btn = QPushButton("从备份恢复…")
        self.restore_btn.setProperty("variant", "ghost")
        self.restore_btn.clicked.connect(self._restore)
        backup_row.addWidget(self.backup_btn)
        backup_row.addWidget(self.restore_btn)
        backup_row.addStretch(1)

        self.reset_btn = QPushButton("恢复默认设置")
        self.reset_btn.setProperty("variant", "danger")
        self.reset_btn.setToolTip("只是把默认值填回表单，点「保存」才生效")
        self.reset_btn.clicked.connect(self._reset)
        self._reset_hint = hint_label("")

        form.addRow("小区名称", self.community_edit)
        form.addRow("你的署名", self.operator_edit)
        form.addRow("联系方式", self.contact_edit)
        form.addRow("对外脱敏", self.privacy_combo)
        form.addRow("", self.enable_box)
        form.addRow("提醒窗口", self.remind_spin)
        form.addRow("扫描间隔", self.interval_combo)
        form.addRow("结算催款", self.settle_spin)
        form.addRow("", self.cover_box)
        form.addRow("", self.browser_box)
        form.addRow("抓取超时", self.timeout_spin)
        form.addRow(hint_label("以下为 v1.2.0 的后台能力"), QLabel(""))
        form.addRow("", self.tray_box)
        form.addRow("", self.notify_box)
        form.addRow("", self.minimize_box)
        form.addRow("", self.watch_box)
        form.addRow("盯梢间隔", self.watch_spin)
        form.addRow("", self.watch_import_box)
        form.addRow(hint_label("数据备份（v1.3.0）"), QLabel(""))
        form.addRow(backup_box)
        form.addRow(hint_label("含数据库、设置与封面图；恢复前会自动给当前数据留档"))
        form.addRow(self.reset_btn)
        layout.addLayout(form, 1)
        layout.addWidget(self._reset_hint)

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Save).setText("保存")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _load(self) -> None:
        c = self.cfg
        self.community_edit.setText(c.get("community_name", ""))
        self.operator_edit.setText(c.get("operator_name", ""))
        self.contact_edit.setText(c.get("contact_info", ""))
        pidx = self.privacy_combo.findData(
            c.get("privacy_level", privacy.LEVEL_MEDIUM))
        self.privacy_combo.setCurrentIndex(pidx if pidx >= 0 else 1)
        self.enable_box.setChecked(bool(c.get("scheduler_enabled", True)))
        self.remind_spin.setValue(int(c.get("remind_hours", 24)))
        idx = self.interval_combo.findData(int(c.get("scan_interval_minutes", 30)))
        self.interval_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.settle_spin.setValue(int(c.get("settle_remind_hours", 24)))
        self.cover_box.setChecked(bool(c.get("auto_download_cover", True)))
        self.browser_box.setChecked(bool(c.get("browser_fallback", True)))
        self.timeout_spin.setValue(int(c.get("request_timeout", 12)))
        self.tray_box.setChecked(bool(c.get("tray_enabled", True)))
        self.notify_box.setChecked(bool(c.get("notify_enabled", True)))
        self.minimize_box.setChecked(bool(c.get("minimize_to_tray", True)))
        self.watch_box.setChecked(bool(c.get("watch_enabled", True)))
        self.watch_spin.setValue(int(c.get("watch_interval_hours", 6)))
        self.watch_import_box.setChecked(bool(c.get("watch_auto_import", False)))

    def _save(self) -> None:
        config.save_settings({
            "community_name": self.community_edit.text().strip() or "我们小区",
            "operator_name": self.operator_edit.text().strip() or "团长",
            "contact_info": self.contact_edit.text().strip(),
            "privacy_level": self.privacy_combo.currentData(),
            "scheduler_enabled": self.enable_box.isChecked(),
            "remind_hours": int(self.remind_spin.value()),
            "scan_interval_minutes": int(self.interval_combo.currentData()),
            "settle_remind_hours": int(self.settle_spin.value()),
            "auto_download_cover": self.cover_box.isChecked(),
            "browser_fallback": self.browser_box.isChecked(),
            "request_timeout": int(self.timeout_spin.value()),
            "tray_enabled": self.tray_box.isChecked(),
            "notify_enabled": self.notify_box.isChecked(),
            "minimize_to_tray": self.minimize_box.isChecked(),
            "watch_enabled": self.watch_box.isChecked(),
            "watch_interval_hours": int(self.watch_spin.value()),
            "watch_auto_import": self.watch_import_box.isChecked(),
        })
        self.accept()

    def _backup(self) -> None:
        """导出一份完整备份（数据库一致性快照 + 设置 + 封面）。"""
        dest, _ = QFileDialog.getExistingDirectory(
            self, "选择备份保存到哪个文件夹", str(config.EXPORT_DIR))
        if not dest:
            return
        try:
            path = backup.create_backup(dest)
        except Exception as exc:
            warn(self, "备份失败", str(exc))
            return
        info(self, "备份完成", f"已保存到：\n{path}")

    def _restore(self) -> None:
        """从备份包恢复，覆盖前会给当前数据留一份档。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择备份文件", str(config.EXPORT_DIR), "ZIP 压缩包 (*.zip)")
        if not path:
            return
        manifest = backup.read_manifest(path)
        if manifest:
            counts = manifest.get("counts", {})
            desc = (f"备份时间：{manifest.get('created_at', '未知')}\n"
                    f"内容 {counts.get('items', 0)} 条 / 报名 {counts.get('signups', 0)} 条")
        else:
            desc = "（备份包里没有清单信息，无法预览内容）"
        if not confirm(self, "恢复备份",
                       f"将用这个备份覆盖当前数据：\n\n{desc}\n\n继续吗？",
                       ok_text="确认恢复"):
            return
        ok, msg = backup.restore_backup(path)
        if not ok:
            warn(self, "恢复失败", msg)
            return
        self.cfg = dict(config.reload_settings())
        self._load()
        self.restored.emit()
        info(self, "恢复完成", f"{msg}\n\n所有界面已重新载入。")

    def _reset(self) -> None:
        """恢复默认：只改内存里的副本，点「保存」才真的落盘。

        以前一点就立刻写进 settings.json，用户反悔也回不去。
        """
        self.cfg = dict(config.DEFAULT_SETTINGS)
        self._load()
        hint = getattr(self, "_reset_hint", None)
        if hint is not None:
            hint.setText("已载入默认设置，点「保存」才会生效；点「取消」可放弃。")
