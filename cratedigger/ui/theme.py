"""Dark, dense, lime-accented theme (Rekordbox / Serato inspired)."""
ACCENT = "#B5F23D"
ACCENT_DIM = "#7FA82B"
BG = "#121314"
BG2 = "#1A1B1D"
BG3 = "#232528"
LINE = "#2E3135"
TEXT = "#E4E6E8"
MUTED = "#8B9096"
OK = "#B5F23D"
WARN = "#F2B33D"
BAD = "#F2553D"
INFO = "#4DB3F2"

STATUS_COLORS = {
    "new": MUTED, "queued": INFO, "searching": INFO, "review": WARN, "missing": BAD, "failed": BAD,
    "selected": "#C99BF2", "downloading": INFO, "downloaded": "#C99BF2", "in_library": OK,
}

QSS = f"""
* {{ font-family: ".AppleSystemUIFont", "Helvetica Neue"; font-size: 12px; color: {TEXT}; }}
QMainWindow, QWidget#central, QStackedWidget > QWidget {{ background: {BG}; }}
QWidget {{ background: {BG}; }}
QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 17px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#statValue {{ font-size: 26px; font-weight: 600; color: {ACCENT}; }}
QLabel#statLabel {{ color: {MUTED}; font-size: 11px; text-transform: uppercase; }}
QFrame#card {{ background: {BG2}; border: 1px solid {LINE}; border-radius: 6px; }}
QListWidget#nav {{ background: {BG2}; border: none; border-right: 1px solid {LINE}; padding-top: 8px; outline: 0; }}
QListWidget#nav::item {{ padding: 9px 14px; color: {MUTED}; border-left: 3px solid transparent; }}
QListWidget#nav::item:selected {{ color: {TEXT}; background: {BG3}; border-left: 3px solid {ACCENT}; }}
QListWidget#nav::item:hover {{ color: {TEXT}; }}
QTableView {{ background: {BG}; alternate-background-color: #16171A; gridline-color: {LINE};
             border: 1px solid {LINE}; selection-background-color: #2F3A1C; selection-color: {TEXT}; }}
QTableView::item {{ padding: 0 4px; }}
QHeaderView::section {{ background: {BG2}; color: {MUTED}; border: none; border-right: 1px solid {LINE};
                       border-bottom: 1px solid {LINE}; padding: 4px 6px; font-size: 11px; }}
QTableCornerButton::section {{ background: {BG2}; border: none; }}
QPushButton {{ background: {BG3}; border: 1px solid {LINE}; border-radius: 4px; padding: 5px 11px; }}
QPushButton:hover {{ border-color: {ACCENT_DIM}; }}
QPushButton:pressed {{ background: #2A2D31; }}
QPushButton:disabled {{ color: #555A60; border-color: #26292C; }}
QPushButton#primary {{ background: {ACCENT}; color: #101208; border: none; font-weight: 600; }}
QPushButton#primary:hover {{ background: #C8FF5A; }}
QPushButton#primary:disabled {{ background: #3A4520; color: #6B7550; }}
QPushButton#danger {{ color: {BAD}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {BG2}; border: 1px solid {LINE}; border-radius: 4px; padding: 4px 6px;
    selection-background-color: {ACCENT_DIM}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT_DIM}; }}
QComboBox QAbstractItemView {{ background: {BG2}; border: 1px solid {LINE}; selection-background-color: {BG3}; }}
QProgressBar {{ background: {BG3}; border: none; border-radius: 3px; height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QStatusBar {{ background: {BG2}; border-top: 1px solid {LINE}; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 0 6px; }}
QSplitter::handle {{ background: {LINE}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BG3}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BG3}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QToolTip {{ background: {BG3}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {LINE}; background: {BG2}; border-radius: 2px; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; }}
QGroupBox {{ border: 1px solid {LINE}; border-radius: 6px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {MUTED}; }}
QMenu {{ background: {BG2}; border: 1px solid {LINE}; }}
QMenu::item:selected {{ background: {BG3}; }}
"""
