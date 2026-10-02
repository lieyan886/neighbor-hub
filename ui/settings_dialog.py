"""设置对话框：小区身份、提醒策略、素材偏好。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from core import config, privacy
from ui import theme
from ui.widgets import hint_label, set_placeholder

_INTERVALS = (("15 分钟", 15), ("30 分钟", 30), ("1 小时", 60), ("2 小时", 120))


class SettingsDialog(QDialog):
    """改完立即生效，重启后依然保留。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(520, 660)
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
        self.contact_edit = QLineEdit()
        set_placeholder(self.contact_edit, "微信号或手机号，会渲染到卡片底部")

        self.enable_box = QCheckBox("开启定时提醒")
        self.remind_spin = QSpinBox()
        self.remind_spin.setRange(1, 96)
        self.remind_spin.setSuffix(" 小时内算即将截止")
        self.interval_combo = QComboBox()
        for label, val in _INTERVALS:
            self.interval_combo.addItem(label, val)

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
        self.uuid_note = QPushButton("恢复默认设置")
        self.uuid_note.setProperty("variant", "danger")
        self.uuid_note.clicked.connect(self._reset)

        form.addRow("小区名称", self.community_edit)
        form.addRow("你的署名", self.operator_edit)
        form.addRow("联系方式", self.contact_edit)
        form.addRow("对外脱敏", self.privacy_combo)
        form.addRow("", self.enable_box)
        form.addRow("提醒窗口", self.remind_spin)
        form.addRow("扫描间隔", self.interval_combo)
        form.addRow("", self.cover_box)
        form.addRow("", self.browser_box)
        form.addRow("抓取超时", self.timeout_spin)
        layout.addLayout(form, 1)

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
        self.cover_box.setChecked(bool(c.get("auto_download_cover", True)))
        self.browser_box.setChecked(bool(c.get("browser_fallback", True)))
        self.timeout_spin.setValue(int(c.get("request_timeout", 12)))
        self.tray_box.setChecked(bool(c.get("tray_enabled", True)))
        self.notify_box.setChecked(bool(c.get("notify_enabled", True)))
        self.minimize_box.setChecked(bool(c.get("minimize_to_tray", True)))
        self.watch_box.setChecked(bool(c.get("watch_enabled", True)))
        self.watch_spin.setValue(int(c.get("watch_interval_hours", 6)))
        self.watch_import_box.setChecked(bool(c.get("watch_auto_import", False)))

        # —— v1.2.0：后台常驻与自动盯梢 ——
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

        form.addRow(hint_label("以下为 v1.2.0 的后台能力"), QLabel(""))
        form.addRow("", self.tray_box)
        form.addRow("", self.notify_box)
        form.addRow("", self.minimize_box)
        form.addRow("", self.watch_box)
        form.addRow("盯梢间隔", self.watch_spin)
        form.addRow("", self.watch_import_box)

    def _save(self) -> None:
        config.save_settings({
            "community_name": self.community_edit.text().strip() or "我们小区",
            "operator_name": self.operator_edit.text().strip() or "团长",
            "contact_info": self.contact_edit.text().strip(),
            "privacy_level": self.privacy_combo.currentData(),
            "scheduler_enabled": self.enable_box.isChecked(),
            "remind_hours": int(self.remind_spin.value()),
            "scan_interval_minutes": int(self.interval_combo.currentData()),
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

    def _reset(self) -> None:
        self.cfg = config.reset_settings()
        self._load()
