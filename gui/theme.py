"""Fluent 2 inspired light theme with a blue brand color for the desktop controller."""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Fluent 2 web light tokens.
BRAND = "#0F6CBD"
BRAND_HOVER = "#115EA3"
BRAND_PRESSED = "#0F548C"
BRAND_TINT = "#EBF3FC"
FG1, FG2, FG3, FG_DISABLED = "#242424", "#424242", "#616161", "#BDBDBD"
BG_APP, BG_SUBTLE, BG_CARD, BG_DISABLED = "#F5F5F5", "#FAFAFA", "#FFFFFF", "#F0F0F0"
STROKE, STROKE_SOFT, STROKE_STRONG = "#D1D1D1", "#E0E0E0", "#616161"

# tone -> (background, foreground)
TONES = {
    "neutral": (BG_DISABLED, FG2),
    "info": (BRAND_TINT, BRAND),
    "success": ("#DFF6DD", "#0E700E"),
    "warning": ("#FFF4CE", "#7A5800"),
    "danger": ("#FDE7E9", "#B10E1C"),
}

FONT_UI = '"Segoe UI Variable Text", "Segoe UI", sans-serif'
FONT_MONO = '"Cascadia Mono", Consolas, "Courier New", monospace'


def _pill_rules() -> str:
    return "\n".join(
        f'QLabel#pill[tone="{tone}"] {{ background: {bg}; color: {fg}; }}'
        for tone, (bg, fg) in TONES.items()
    )


STYLESHEET = f"""
QWidget {{ font-family: {FONT_UI}; font-size: 13px; color: {FG1}; }}
QMainWindow, QDialog {{ background: {BG_APP}; }}
QDialog QLabel, QMessageBox QLabel {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}

QFrame#card {{ background: {BG_CARD}; border: 1px solid {STROKE_SOFT}; border-radius: 8px; }}
QFrame#card QLabel {{ background: transparent; }}
QFrame#divider {{ background: {STROKE_SOFT}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#accent {{ background: {BRAND}; border-radius: 2px; }}
QLabel#appTitle {{ font-size: 24px; font-weight: 600; }}
QLabel#cardTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#rowName {{ font-weight: 600; }}
QLabel[muted="true"] {{ color: {FG3}; }}

QLabel#pill {{ border-radius: 10px; padding: 2px 10px; font-size: 12px; font-weight: 600; }}
{_pill_rules()}

QPushButton {{
    background: {BG_CARD}; color: {FG1}; border: 1px solid {STROKE}; border-radius: 4px;
    padding: 5px 14px; min-height: 20px;
}}
QPushButton:hover {{ background: {BG_APP}; border-color: #C7C7C7; }}
QPushButton:pressed {{ background: {STROKE_SOFT}; }}
QPushButton:focus {{ border-color: {BRAND}; }}
QPushButton:disabled {{
    background: {BG_DISABLED}; color: {FG_DISABLED}; border-color: {STROKE_SOFT};
}}
QPushButton[variant="primary"] {{
    background: {BRAND}; color: #FFFFFF; border: 1px solid {BRAND}; font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background: {BRAND_HOVER}; border-color: {BRAND_HOVER}; }}
QPushButton[variant="primary"]:pressed {{
    background: {BRAND_PRESSED}; border-color: {BRAND_PRESSED};
}}
QPushButton[variant="primary"]:disabled {{
    background: {BG_DISABLED}; color: {FG_DISABLED}; border-color: {STROKE_SOFT};
}}

QLineEdit {{
    background: {BG_CARD}; border: 1px solid {STROKE}; border-bottom: 1px solid {STROKE_STRONG};
    border-radius: 4px; padding: 5px 8px; selection-background-color: {BRAND};
    selection-color: #FFFFFF;
}}
QLineEdit:focus {{ border-bottom: 2px solid {BRAND}; }}
QLineEdit:disabled {{ background: {BG_DISABLED}; color: {FG_DISABLED}; }}

QPlainTextEdit {{
    background: {BG_SUBTLE}; border: 1px solid {STROKE_SOFT}; border-radius: 4px; padding: 6px;
    font-family: {FONT_MONO}; font-size: 12px; selection-background-color: {BRAND};
    selection-color: #FFFFFF;
}}

QTableWidget {{
    background: {BG_CARD}; border: 1px solid {STROKE_SOFT}; border-radius: 4px;
    gridline-color: transparent; selection-background-color: {BRAND_TINT}; selection-color: {FG1};
}}
QTableWidget::item {{ padding: 4px 8px; border-bottom: 1px solid {BG_DISABLED}; }}
QHeaderView::section {{
    background: {BG_SUBTLE}; color: {FG2}; border: none; border-bottom: 1px solid {STROKE_SOFT};
    padding: 6px 8px; font-weight: 600;
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #C7C7C7; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #A0A0A0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #C7C7C7; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{ background: {BG_CARD}; color: {FG1}; border: 1px solid {STROKE}; padding: 4px 8px; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")  # Predictable base; the stylesheet supplies the Fluent look.
    app.setFont(QFont("Segoe UI Variable Text", 10))
    app.setStyleSheet(STYLESHEET)


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Pill(QLabel):
    """Rounded status badge whose colors come from a tone: neutral/info/success/warning/danger."""

    def __init__(self, text: str = "", tone: str = "neutral") -> None:
        super().__init__(text)
        self.setObjectName("pill")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setProperty("tone", tone)

    def set_state(self, text: str, tone: str) -> None:
        self.setText(text)
        if self.property("tone") != tone:
            self.setProperty("tone", tone)
            repolish(self)


class Card(QFrame):
    """White rounded surface with a title row. Add content to `body`."""

    def __init__(self, title: str, caption: str = "") -> None:
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 16, 20, 16)
        self.body.setSpacing(12)
        self.header = QHBoxLayout()
        self.header.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        self.header.addWidget(heading)
        if caption:
            note = QLabel(caption)
            note.setProperty("muted", True)
            self.header.addWidget(note)
        self.header.addStretch()
        self.body.addLayout(self.header)


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line
