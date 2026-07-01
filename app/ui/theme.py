# SalvageMap — GUI wrapper over GNU ddrescue.
# Copyright (C) 2026 Champlin Guys Data Recovery
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Console / terminal theme: palette, fonts and a global Qt stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont

# --- palette --------------------------------------------------------------
BG = "#0d1117"          # window background (near black)
BG_ALT = "#0f141b"      # panels / docks
BG_RAISED = "#161b22"   # inputs, headers, raised surfaces
BG_HOVER = "#1c2430"
BORDER = "#222b36"
BORDER_BRIGHT = "#30363d"
FG = "#c9d1d9"          # primary text
FG_DIM = "#7d8794"      # secondary text
ACCENT = "#2dd4bf"      # teal
ACCENT_DIM = "#1f9488"
SELECTION_BG = "#143b38"
DANGER = "#f85149"
WARN = "#e3b341"

# Preferred monospace stack (first available wins).
MONO_FAMILIES = [
    "Cascadia Code", "JetBrains Mono", "Fira Code", "Hack",
    "Ubuntu Mono", "DejaVu Sans Mono", "Liberation Mono", "monospace",
]


def app_font(point_size: int = 10) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setPointSize(point_size)
    font.setStyleHint(QFont.TypeWriter)
    return font


def qcolor(hex_str: str) -> QColor:
    return QColor(hex_str)


def stylesheet() -> str:
    return f"""
    QWidget {{
        background: {BG};
        color: {FG};
        selection-background-color: {SELECTION_BG};
        selection-color: {ACCENT};
    }}
    QMainWindow, QDialog {{ background: {BG}; }}

    /* Menu bar */
    QMenuBar {{ background: {BG_ALT}; border-bottom: 1px solid {BORDER}; padding: 2px; }}
    QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 4px; }}
    QMenuBar::item:selected {{ background: {BG_HOVER}; color: {ACCENT}; }}
    QMenu {{ background: {BG_RAISED}; border: 1px solid {BORDER_BRIGHT}; padding: 4px; }}
    QMenu::item {{ padding: 5px 24px 5px 12px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {SELECTION_BG}; color: {ACCENT}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

    /* Docks */
    QDockWidget {{ titlebar-close-icon: none; color: {FG_DIM}; }}
    QDockWidget::title {{
        background: {BG_ALT}; padding: 5px 8px; border: 1px solid {BORDER};
        text-transform: uppercase; letter-spacing: 1px;
    }}
    QDockWidget > QWidget {{ border: 1px solid {BORDER}; }}

    /* Labels */
    QLabel {{ background: transparent; }}

    /* Text views */
    QPlainTextEdit, QTextEdit {{
        background: {BG_ALT}; border: 1px solid {BORDER};
        selection-background-color: {SELECTION_BG};
    }}

    /* Buttons */
    QPushButton {{
        background: {BG_RAISED}; border: 1px solid {BORDER_BRIGHT};
        padding: 6px 14px; border-radius: 6px; color: {FG};
    }}
    QPushButton:hover {{ border-color: {ACCENT_DIM}; color: {ACCENT}; }}
    QPushButton:pressed {{ background: {BG_HOVER}; }}
    QPushButton:default {{ border-color: {ACCENT}; color: {ACCENT}; }}
    QPushButton:disabled {{ color: {FG_DIM}; border-color: {BORDER}; }}

    /* Tree / tables (device picker) */
    QTreeWidget, QTreeView, QTableView {{
        background: {BG_ALT}; border: 1px solid {BORDER};
        alternate-background-color: {BG};
        outline: 0;
    }}
    QTreeView::item, QTreeWidget::item {{ padding: 4px 6px; }}
    QTreeView::item:selected, QTreeWidget::item:selected {{
        background: {SELECTION_BG}; color: {ACCENT};
    }}
    QHeaderView::section {{
        background: {BG_RAISED}; color: {FG_DIM}; padding: 5px 8px;
        border: none; border-right: 1px solid {BORDER};
        text-transform: uppercase; letter-spacing: 1px;
    }}

    /* Checkboxes */
    QCheckBox {{ spacing: 6px; }}
    QCheckBox::indicator {{
        width: 14px; height: 14px; border: 1px solid {BORDER_BRIGHT};
        border-radius: 3px; background: {BG_RAISED};
    }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

    /* Tabs (bottom docks) */
    QTabBar::tab {{
        background: {BG_ALT}; color: {FG_DIM}; padding: 6px 14px;
        border: 1px solid {BORDER}; border-bottom: none;
        border-top-left-radius: 6px; border-top-right-radius: 6px;
    }}
    QTabBar::tab:selected {{ background: {BG_RAISED}; color: {ACCENT}; }}

    /* Status bar */
    QStatusBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; color: {FG_DIM}; }}
    QStatusBar::item {{ border: none; }}

    /* Scrollbars */
    QScrollBar:vertical {{ background: {BG}; width: 12px; margin: 0; }}
    QScrollBar:horizontal {{ background: {BG}; height: 12px; margin: 0; }}
    QScrollBar::handle {{ background: {BORDER_BRIGHT}; border-radius: 6px; min-height: 24px; }}
    QScrollBar::handle:hover {{ background: {ACCENT_DIM}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* Tooltips */
    QToolTip {{
        background: {BG_RAISED}; color: {FG}; border: 1px solid {ACCENT_DIM};
        padding: 4px 6px;
    }}
    """
