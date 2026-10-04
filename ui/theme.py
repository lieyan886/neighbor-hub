"""深色主题与统一视觉常量。

整个界面只用这一套调色板，改一处全跟着变。
"""
from __future__ import annotations

# —— 调色板 ——
BG = "#1B1D23"          # 窗口底色
SURFACE = "#23262E"     # 卡片/面板
SURFACE_2 = "#2C303A"   # 次级面板、输入控件
BORDER = "#343945"
TEXT = "#E6E8EB"
TEXT_MUTED = "#9AA1AD"
TEXT_FAINT = "#6B7280"
ACCENT = "#E8734A"      # 主强调色（暖橙）
ACCENT_HOVER = "#F28A63"
TEAL = "#1D9E75"
AMBER = "#EF9F27"
BLUE = "#378ADD"
RED = "#E24B4A"
GREEN = "#2FA36B"       # 下降/向好：跟国内行情习惯一致（涨红跌绿）
PURPLE = "#7F77DD"

KIND_COLORS = {
    "event": PURPLE,
    "groupbuy": ACCENT,
    "deal": BLUE,
}

STATUS_COLORS = {
    "draft": TEXT_FAINT,
    "active": TEAL,
    "ending": AMBER,
    "expired": RED,
    "archived": TEXT_MUTED,
}

QSS = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", sans-serif;
    font-size: 13px;
}}
QWidget#surface, QFrame#surface, QGroupBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QGroupBox {{
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QLabel {{
    background: transparent;
    color: {TEXT};
}}
QLabel[role="muted"] {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QLabel[role="hint"] {{
    color: {TEXT_FAINT};
    font-size: 12px;
}}
QLabel[role="title"] {{
    font-size: 16px;
    font-weight: 600;
}}
QLabel[role="stat-value"] {{
    font-size: 26px;
    font-weight: 700;
}}
QLabel[role="stat-title"] {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}

QPushButton {{
    background-color: {SURFACE_2};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 16px;
}}
QPushButton:hover {{ background-color: #343945; border-color: #4A5262; }}
QPushButton:pressed {{ background-color: #3A4150; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background-color: {SURFACE}; }}
QPushButton[accent="true"] {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: #1B1411;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background-color: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[variant="ghost"] {{
    background: transparent;
    border: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QPushButton[variant="ghost"]:hover {{ background-color: {SURFACE_2}; color: {TEXT}; }}
QPushButton[variant="danger"]:hover {{ background-color: #4A2226; border-color: {RED}; color: #FFB4B4; }}

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit {{
    background-color: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: {ACCENT};
    selection-color: #1B1411;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background-color: {SURFACE_2};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #1B1411;
}}

QTableWidget, QTableView, QListWidget, QTreeWidget {{
    background-color: {SURFACE};
    alternate-background-color: #262A33;
    border: 1px solid {BORDER};
    border-radius: 10px;
    gridline-color: {BORDER};
    outline: none;
}}
QTableView::item {{ padding: 6px 8px; }}
QTableView::item:selected, QListWidget::item:selected {{
    background-color: {ACCENT}; color: #1B1411;
}}
QListWidget::item {{
    padding: 10px 14px; border-radius: 8px; margin: 2px 6px;
}}
QListWidget::item:hover {{ background-color: {SURFACE_2}; }}

QHeaderView::section {{
    background-color: {SURFACE_2};
    color: {TEXT_MUTED};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 8px 10px;
    font-weight: 500;
}}

QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 4px;
}}
QScrollBar::handle:vertical {{
    background: #3A4150; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #4A5262; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 4px; }}
QScrollBar::handle:horizontal {{
    background: #3A4150; border-radius: 5px; min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}

QProgressBar {{
    background-color: {SURFACE_2}; border: 1px solid {BORDER};
    border-radius: 6px; height: 14px; text-align: center; color: {TEXT};
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 5px; }}

QStatusBar {{
    background-color: {SURFACE}; color: {TEXT_MUTED};
    border-top: 1px solid {BORDER};
}}
QToolTip {{
    background-color: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER}; padding: 4px 8px;
}}
QSplitter::handle {{ background-color: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {BORDER}; background-color: {SURFACE_2};
}}
QCheckBox::indicator:checked {{ background-color: {ACCENT}; border-color: {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_MUTED};
    padding: 8px 18px; border: none; font-size: 13px;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
"""


def apply(app) -> None:
    """把主题套到 QApplication 上。"""
    app.setStyleSheet(QSS)


def status_color(status: str) -> str:
    return STATUS_COLORS.get(status, TEXT_MUTED)


def kind_color(kind: str) -> str:
    return KIND_COLORS.get(kind, BLUE)


def color(value: str) -> "QColor":
    """把 #RRGGBB 转成 QColor，方便直接喂给 setForeground。"""
    from PySide6.QtGui import QColor

    return QColor(value)
