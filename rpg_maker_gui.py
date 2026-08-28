#!/usr/bin/env python3
"""PyQt6 interface for rpg_maker_tool.py and unity_extractor.py.

The window is a single flow: pick a game, pick where the result goes, choose
whether the game's folder structure is kept, then run. Everything else lives in
a collapsed "advanced" card so the common case stays short.
"""

from __future__ import annotations

import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from output_structure import describe_structure

from unity_extractor import (
    ENGINE_UNITY,
    UnityExtractionOptions,
    collect_unity_sources,
    detect_unity_input,
    extract_unity,
)

from rpg_maker_tool import (
    ASSET_EXTENSIONS,
    HEADER_LENGTH,
    PLAIN_ASSET_EXTENSIONS,
    KeyCandidate,
    ProjectBuildOptions,
    build_editable_project,
    collect_asset_jobs,
    decrypt_asset_job,
    default_output_folder_name,
    detect_rpg_header,
    detect_engine,
    DEFAULT_OUTPUT_PARENT,
    ENGINE_AUTO,
    ENGINE_RPGMAKER_MV,
    ENGINE_RPGMAKER_MZ,
    ENGINE_RPGMAKER_VX_ACE,
    ENGINE_WOLF_RPG,
    ExtractionOptions,
    find_keys,
    find_keys_including_game_root,
    key_candidate_to_bytes,
    key_to_bytes,
    iter_files,
    normalize_path,
    preview_project_build,
    read_prefix,
    relpath,
    run_unified_extraction,
)

APP_NAME = "RPG Asset Studio"
APP_SUBTITLE = "RPG Maker · WOLF RPG · Unity"
SETTINGS_ORG = "RPGMakerExtractor"
SETTINGS_APP = "AssetStudio"

STRUCTURE_EXAMPLE = "img/pictures/Actor1.png"

MODE_FILES = "files"
MODE_EXTRACT = "extract"
MODE_PROJECT = "project"

MODE_BUTTON_LABELS = {
    MODE_FILES: "Расшифровать файлы",
    MODE_EXTRACT: "Извлечь всё",
    MODE_PROJECT: "Собрать проект",
}

MODE_OUTPUT_HINTS = {
    MODE_FILES: (
        "Что получится: выбранные типы файлов, расшифрованные, прямо в папке вывода. "
        "Ни текста, ни отчётов — просто файлы."
    ),
    MODE_EXTRACT: (
        "Что получится: extracted/ — все выбранные типы (расшифрованные), "
        "translation/translation.jsonl — текст для перевода, manifest.json — отчёт. "
        "Для Unity, WOLF RPG и VX Ace это единственный рабочий режим."
    ),
    MODE_PROJECT: (
        "Что получится: копия игры для редактора RPG Maker — папка www раскрывается в корень "
        "проекта, зашифрованные ассеты расшифровываются, флаги шифрования в System.json "
        "снимаются, при необходимости создаётся файл проекта. Только RPG Maker MV и MZ."
    ),
}

INPUT_FILE_FILTER = (
    "Архивы игр (*.rgss3a *.rgss2a *.rgssad *.apk *.xapk *.aab *.obb *.assets *.bundle "
    "*.unity3d *.assetbundle *.ab);;Все файлы (*)"
)

DARK_COLORS = {
    "bg": "#12141b",
    "surface": "#1a1d27",
    "surface_alt": "#212533",
    "border": "#2c3143",
    "text": "#e7eaf3",
    "muted": "#98a0b8",
    "accent": "#6c8cff",
    "accent_hover": "#7f9bff",
    "accent_text": "#0d1020",
    "ok": "#43d18d",
    "warn": "#ffb545",
    "error": "#ff6f6f",
    "track_off": "#333a4e",
    "knob": "#f2f4fb",
}

LIGHT_COLORS = {
    "bg": "#f3f5fa",
    "surface": "#ffffff",
    "surface_alt": "#eef1f8",
    "border": "#d7dcea",
    "text": "#1b1f2b",
    "muted": "#5f6880",
    "accent": "#3f63f5",
    "accent_hover": "#5375ff",
    "accent_text": "#ffffff",
    "ok": "#12a06a",
    "warn": "#b3720a",
    "error": "#d0342c",
    "track_off": "#c7cdde",
    "knob": "#ffffff",
}


class Theme:
    """Active colour set. Custom-painted widgets read it while painting."""

    def __init__(self) -> None:
        self.name = "dark"
        self.colors = dict(DARK_COLORS)

    def use(self, name: str) -> None:
        self.name = "light" if name == "light" else "dark"
        self.colors = dict(LIGHT_COLORS if self.name == "light" else DARK_COLORS)

    def color(self, key: str) -> QColor:
        return QColor(self.colors[key])


THEME = Theme()


def build_stylesheet() -> str:
    c = THEME.colors
    return f"""
    QWidget {{
        color: {c['text']};
        font-size: 13px;
    }}
    QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {{
        background: {c['bg']};
    }}
    QToolTip {{
        background: {c['surface_alt']};
        color: {c['text']};
        border: 1px solid {c['border']};
        padding: 6px 8px;
    }}
    #Header {{
        background: {c['surface']};
        border-bottom: 1px solid {c['border']};
    }}
    #AppTitle {{
        font-size: 18px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }}
    #AppSubtitle, .Hint, #Hint {{
        color: {c['muted']};
        font-size: 12px;
    }}
    #Card {{
        background: {c['surface']};
        border: 1px solid {c['border']};
        border-radius: 14px;
    }}
    #CardTitle {{
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.4px;
        text-transform: uppercase;
        color: {c['muted']};
    }}
    #CardHeader {{
        border: none;
        background: transparent;
    }}
    QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 9px;
        padding: 7px 10px;
        selection-background-color: {c['accent']};
        selection-color: {c['accent_text']};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
        border: 1px solid {c['accent']};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QCheckBox:disabled {{
        color: {c['muted']};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox QAbstractItemView {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        selection-background-color: {c['accent']};
        selection-color: {c['accent_text']};
        outline: none;
    }}
    QPushButton {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 9px;
        padding: 8px 14px;
    }}
    QPushButton:hover {{
        border-color: {c['accent']};
    }}
    QPushButton:pressed {{
        background: {c['border']};
    }}
    QPushButton:disabled {{
        color: {c['muted']};
        border-color: {c['border']};
    }}
    QPushButton#Primary {{
        background: {c['accent']};
        border: 1px solid {c['accent']};
        color: {c['accent_text']};
        font-weight: 600;
        padding: 9px 22px;
    }}
    QPushButton#Primary:hover {{
        background: {c['accent_hover']};
        border-color: {c['accent_hover']};
    }}
    QPushButton#Primary:disabled {{
        background: {c['surface_alt']};
        border-color: {c['border']};
        color: {c['muted']};
    }}
    QPushButton#Ghost {{
        background: transparent;
        border: 1px solid transparent;
        color: {c['muted']};
        padding: 6px 10px;
    }}
    QPushButton#Ghost:hover {{
        color: {c['text']};
        border-color: {c['border']};
    }}
    QPushButton#Segment {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        padding: 9px 16px;
        text-align: center;
    }}
    QPushButton#Segment:checked {{
        background: {c['accent']};
        border-color: {c['accent']};
        color: {c['accent_text']};
        font-weight: 600;
    }}
    QCheckBox {{
        spacing: 8px;
        padding: 2px 0;
    }}
    QCheckBox::indicator {{
        width: 17px;
        height: 17px;
        border-radius: 5px;
        border: 1px solid {c['border']};
        background: {c['surface_alt']};
    }}
    QCheckBox::indicator:hover {{
        border-color: {c['accent']};
    }}
    QCheckBox::indicator:checked {{
        background: {c['accent']};
        border-color: {c['accent']};
        image: none;
    }}
    QProgressBar {{
        background: {c['surface_alt']};
        border: 1px solid {c['border']};
        border-radius: 7px;
        height: 12px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background: {c['accent']};
        border-radius: 6px;
    }}
    QPlainTextEdit#Log {{
        border-radius: 12px;
        padding: 10px;
    }}
    QSplitter::handle {{
        background: transparent;
        height: 10px;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {c['border']};
        border-radius: 5px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c['muted']};
    }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none;
        border: none;
        height: 0;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
    }}
    QScrollBar::handle:horizontal {{
        background: {c['border']};
        border-radius: 5px;
        min-width: 30px;
    }}
    """


def app_icon_pixmap(size: int = 64) -> QPixmap:
    """Draw the app mark instead of shipping an image file."""

    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor("#6c8cff"))
    gradient.setColorAt(1.0, QColor("#a05cff"))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * 0.46))
    painter.setFont(font)
    painter.setPen(QColor("#0d1020"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "RA")
    painter.end()
    return pixmap


class ToggleSwitch(QAbstractButton):
    """Checkbox drawn as a sliding switch, for the one option that matters most."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(QSize(50, 28))
        self._position = 1.0 if self.isChecked() else 0.0
        self._animation = QPropertyAnimation(self, b"position", self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        if not self.isVisible():
            self.set_position(1.0 if checked else 0.0)
            return
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def get_position(self) -> float:
        return self._position

    def set_position(self, value: float) -> None:
        self._position = value
        self.update()

    position = pyqtProperty(float, fget=get_position, fset=set_position)

    def sizeHint(self) -> QSize:
        return QSize(50, 28)

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0, 3, self.width(), self.height() - 6)
        off_color = THEME.color("track_off")
        on_color = THEME.color("accent")
        blend = QColor(
            int(off_color.red() + (on_color.red() - off_color.red()) * self._position),
            int(off_color.green() + (on_color.green() - off_color.green()) * self._position),
            int(off_color.blue() + (on_color.blue() - off_color.blue()) * self._position),
        )
        if not self.isEnabled():
            blend.setAlpha(110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(blend)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)

        radius = self.height() / 2 - 5
        travel = self.width() - 2 * (radius + 4)
        center_x = radius + 4 + travel * self._position
        painter.setBrush(THEME.color("knob"))
        painter.drawEllipse(QPoint(int(center_x), self.height() // 2), int(radius), int(radius))
        painter.end()


class SegmentedControl(QWidget):
    """Two mutually exclusive buttons used to pick the run mode."""

    changed = pyqtSignal(str)

    def __init__(self, options: list[tuple[str, str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._buttons: dict[str, QPushButton] = {}
        for value, label, tooltip in options:
            button = QPushButton(label)
            button.setObjectName("Segment")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(tooltip)
            # The checked segment is bold, so reserve the bold width up front —
            # otherwise the label is clipped the moment it is selected.
            bold = QFont(button.font())
            bold.setBold(True)
            button.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(label) + 44)
            button.setMinimumHeight(QFontMetrics(bold).height() + 20)
            button.clicked.connect(lambda _checked, chosen=value: self.changed.emit(chosen))
            layout.addWidget(button)
            self._buttons[value] = button
        layout.addStretch(1)
        if options:
            self._buttons[options[0][0]].setChecked(True)

    def value(self) -> str:
        for value, button in self._buttons.items():
            if button.isChecked():
                return value
        return next(iter(self._buttons))

    def set_value(self, value: str) -> None:
        button = self._buttons.get(value)
        if button is not None and not button.isChecked():
            button.setChecked(True)
            self.changed.emit(value)

    def set_option_enabled(self, value: str, enabled: bool) -> None:
        button = self._buttons.get(value)
        if button is not None:
            button.setEnabled(enabled)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt naming
        super().setEnabled(enabled)
        for button in self._buttons.values():
            button.setEnabled(enabled)


class StatusPill(QLabel):
    """Small coloured capsule used for the engine detection status."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._state = "idle"
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.refresh()

    def set_state(self, text: str, state: str = "idle") -> None:
        self._state = state
        self.setText(text)
        self.refresh()

    def refresh(self) -> None:
        colors = {
            "idle": THEME.colors["muted"],
            "ok": THEME.colors["ok"],
            "warn": THEME.colors["warn"],
            "error": THEME.colors["error"],
        }
        color = colors.get(self._state, THEME.colors["muted"])
        self.setStyleSheet(
            f"border: 1px solid {color}; border-radius: 11px;"
            f"padding: 3px 12px; color: {color}; font-size: 12px; font-weight: 600;"
        )


class Card(QFrame):
    """Rounded panel with a title, optionally collapsible."""

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        collapsible: bool = False,
        expanded: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._collapsible = collapsible

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        header.addWidget(self.title_label)
        header.addStretch(1)

        self.toggle_button: QPushButton | None = None
        if collapsible:
            self.toggle_button = QPushButton("")
            self.toggle_button.setObjectName("Ghost")
            self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.toggle_button.clicked.connect(self.toggle)
            header.addWidget(self.toggle_button)
        outer.addLayout(header)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        outer.addWidget(self.body)

        self.set_expanded(expanded)

    def add_widget(self, widget: QWidget) -> None:
        self.body_layout.addWidget(widget)

    def add_layout(self, layout: Any) -> None:
        self.body_layout.addLayout(layout)

    def set_expanded(self, expanded: bool) -> None:
        self.body.setVisible(expanded or not self._collapsible)
        if self.toggle_button is not None:
            self.toggle_button.setText("Свернуть ▲" if expanded else "Развернуть ▼")

    def is_expanded(self) -> bool:
        return self.body.isVisible()

    def toggle(self) -> None:
        self.set_expanded(not self.is_expanded())


class LogView(QPlainTextEdit):
    """Read-only log that colours lines by severity."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Log")
        self.setReadOnly(True)
        self.setMaximumBlockCount(20000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(max(font.pointSize(), 10))
        self.setFont(font)
        self._lines: list[str] = []

    def clear(self) -> None:
        self._lines = []
        super().clear()

    def rerender(self) -> None:
        lines = list(self._lines)
        super().clear()
        self._lines = []
        for line in lines:
            self.append_line(line)

    def append_line(self, message: str) -> None:
        self._lines.append(message)
        lowered = message.lower()
        if lowered.startswith("error") or lowered.startswith("ошибка") or ": error" in lowered:
            color = THEME.colors["error"]
        elif lowered.startswith("warning") or lowered.startswith("внимание"):
            color = THEME.colors["warn"]
        elif lowered.startswith("ok:") or lowered.startswith("готово") or "self-test ok" in lowered:
            color = THEME.colors["ok"]
        elif message.startswith("—") or message.startswith("»"):
            color = THEME.colors["accent"]
        else:
            color = THEME.colors["text"]
        safe = (
            message.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace(" ", "&nbsp;")
        )
        self.appendHtml(f'<span style="color:{color};white-space:pre">{safe}</span>')
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


def display_key(candidate: KeyCandidate) -> str:
    prefix = "raw:" if candidate.key_format == "raw" else ""
    return f"{prefix}{candidate.key}"


class ToolWorker(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    result = pyqtSignal(object)
    finished = pyqtSignal(int)

    def __init__(self, action: str, params: dict[str, Any]) -> None:
        super().__init__()
        self.action = action
        self.params = params
        self.cancel_requested = False

    def cancel(self) -> None:
        self.cancel_requested = True
        self.log.emit("Cancel requested. Waiting for a safe stop point.")

    def run(self) -> None:
        try:
            if self.action == "check":
                code = self._run_check()
            elif self.action == "preview":
                code = self._run_preview()
            elif self.action == "files":
                code = self._run_decrypt(dry_run=False)
            elif self.action == "extract-unified":
                code = self._run_extract_unified()
            elif self.action == "project":
                code = self._run_project()
            else:
                raise RuntimeError(f"Unknown action: {self.action}")
        except Exception as exc:
            self.log.emit(f"ERROR: {exc}")
            code = 1
        self.finished.emit(code)

    def _use_unity_mode(self) -> bool:
        requested = self.params["engine"]
        if requested == ENGINE_UNITY:
            return True
        if requested != ENGINE_AUTO:
            return False
        detection = detect_unity_input(Path(self.params["input"]))
        return detection.confidence >= 0.65

    def _run_inspect(self) -> int:
        input_path = Path(self.params["input"])
        requested = self.params["engine"]
        unity_detection = detect_unity_input(input_path)

        if requested == ENGINE_UNITY or (
            requested == ENGINE_AUTO and unity_detection.confidence >= 0.65
        ):
            result = unity_detection
        else:
            root = normalize_path(input_path)
            result = detect_engine(root, requested)

        self.log.emit(f"Engine: {result.engine}")
        self.log.emit(f"Confidence: {result.confidence:.2f}")
        if result.edition:
            self.log.emit(f"Edition: {result.edition}")
        for item in result.evidence:
            self.log.emit(f"  - {item}")
        for item in result.warnings:
            self.log.emit(f"Warning: {item}")
        self.result.emit({"action": "inspect", "detection": result})
        return 0 if result.confidence >= 0.35 else 1

    def _run_extract_unity(self) -> int:
        options = UnityExtractionOptions(
            source=Path(self.params["input"]),
            output=Path(self.params["output"]),
            images=self.params["unity_images"],
            text=self.params["unity_text"],
            audio=self.params["unity_audio"],
            fonts=self.params["unity_fonts"],
            overwrite=self.params["overwrite"],
            verbose=self.params["verbose"],
            fallback_unity_version=self.params["unity_version"] or None,
            preserve_structure=self.params["preserve_structure"],
        )
        result = extract_unity(
            options,
            log=self.log.emit,
            progress=self.progress.emit,
            cancelled=lambda: self.cancel_requested,
        )
        manifest = result.manifest
        for item in manifest.get("warnings", []):
            self.log.emit(f"Warning: {item}")
        if manifest.get("errors"):
            self.log.emit(f"Errors: {len(manifest['errors'])}; see unity_manifest.json")
        self.result.emit(
            {"action": "extract-unified", "manifest": manifest, "output": str(result.output)}
        )
        if self.cancel_requested:
            return 130
        return 1 if manifest.get("errors") and manifest.get("archives_processed", 0) == 0 else 0

    def _run_extract_unified(self) -> int:
        if self.cancel_requested:
            self.log.emit("Cancelled before start.")
            return 130
        self.log.emit(
            "Структура папок: "
            + ("сохраняется" if self.params["preserve_structure"] else "не сохраняется (плоский вывод)")
        )
        if self._use_unity_mode():
            return self._run_extract_unity()
        options = ExtractionOptions(
            source=Path(self.params["input"]),
            output=Path(self.params["output"]),
            engine=self.params["engine"],
            images="image" in self.params["kinds"],
            text=self.params["text"],
            resources=bool(self.params["kinds"]),
            asset_kinds=set(self.params["kinds"]),
            key=self.params["key"],
            include_comments=self.params["comments"],
            show_keys=self.params["show_keys"],
            overwrite=self.params["overwrite"],
            strict=self.params["strict"],
            workers=self.params["workers"] or 0,
            preserve_structure=self.params["preserve_structure"],
        )
        result = run_unified_extraction(options)
        manifest = result.manifest
        self.log.emit(f"Engine: {manifest.get('engine')}")
        self.log.emit(f"Edition: {manifest.get('edition') or '-'}")
        self.log.emit(f"Output: {result.output}")
        self.log.emit(f"Archives processed: {manifest.get('archives_processed', 0)}")
        self.log.emit(f"Encryption key detected: {'yes' if manifest.get('key_detected') else 'no'}")
        self.log.emit(
            "Protection key detected: "
            + ("yes" if manifest.get("protection_key_detected") else "no")
        )
        self.log.emit(f"Images extracted: {manifest.get('images_extracted', 0)}")
        self.log.emit(f"Text entries extracted: {manifest.get('text_entries_extracted', 0)}")
        for item in manifest.get("warnings", []):
            self.log.emit(f"Warning: {item}")
        for item in manifest.get("errors", []):
            self.log.emit(f"Error: {item}")
        self.progress.emit(1, 1)
        self.result.emit(
            {"action": "extract-unified", "manifest": manifest, "output": str(result.output)}
        )
        return 1 if manifest.get("errors") else 0

    def _run_check(self) -> int:
        """One diagnostic pass: engine, encryption key, how much is encrypted."""

        code = self._run_inspect()
        if self._use_unity_mode():
            return code

        input_path = normalize_path(Path(self.params["input"]))
        jobs = collect_asset_jobs(input_path, None, {"image", "audio", "video"})
        self.log.emit("")
        self.log.emit(f"Зашифрованных/переименованных файлов: {len(jobs)}")
        by_kind = Counter(job.kind for job in jobs)
        for kind, count in sorted(by_kind.items()):
            self.log.emit(f"  {kind}: {count}")

        plain = Counter()
        for path in iter_files(input_path):
            kind = PLAIN_ASSET_EXTENSIONS.get(path.suffix.lower())
            if kind:
                plain[kind] += 1
        if plain:
            self.log.emit(
                "Незашифрованных ассетов: "
                + ", ".join(f"{kind}={count}" for kind, count in sorted(plain.items()))
            )

        self.log.emit("")
        keys_code = self._run_keys()
        if jobs and keys_code != 0:
            self.log.emit(
                "Ключ не найден, а зашифрованные файлы есть — впишите ключ вручную в «Дополнительно»."
            )
            return 1
        return code

    def _run_preview(self) -> int:
        mode = self.params.get("mode", MODE_FILES)
        if mode == MODE_PROJECT:
            return self._preview_project()
        if mode == MODE_EXTRACT:
            return self._preview_extract()
        return self._run_decrypt(dry_run=True)

    def _preview_project(self) -> int:
        options = ProjectBuildOptions(
            source=Path(self.params["input"]),
            output=Path(self.params["output"]),
            key=self.params["key"],
            engine=self.params["engine"],
            include_runtime=self.params["include_runtime"],
        )
        game_root, planned = preview_project_build(options)
        decrypted = sum(
            1 for source, _target in planned if source.suffix.lower() in ASSET_EXTENSIONS
        )
        self.log.emit(f"Игра: {game_root}")
        self.log.emit(f"Проект: {self.params['output']}")
        self.log.emit(f"Файлов всего: {len(planned)}, из них расшифровать: {decrypted}")
        self.log.emit("")
        for source, target in planned[:50]:
            self.log.emit(f"{source.name} -> {target}")
        if len(planned) > 50:
            self.log.emit(f"... и ещё {len(planned) - 50}")
        self.progress.emit(1, 1)
        return 0

    def _preview_extract(self) -> int:
        if self._use_unity_mode():
            sources = collect_unity_sources(Path(self.params["input"]))
            self.log.emit(f"Unity-архивов к обработке: {len(sources)}")
            for path in sources[:50]:
                self.log.emit(str(path))
            if len(sources) > 50:
                self.log.emit(f"... и ещё {len(sources) - 50}")
            self.progress.emit(1, 1)
            return 0

        input_path = normalize_path(Path(self.params["input"]))
        output = normalize_path(Path(self.params["output"]))
        extracted = output / "extracted"
        kinds = self.params["kinds"]
        jobs = collect_asset_jobs(
            input_path, extracted, kinds, self.params["preserve_structure"]
        )
        plain = 0
        for path in iter_files(input_path):
            suffix = path.suffix.lower()
            if suffix in ASSET_EXTENSIONS:
                continue
            if PLAIN_ASSET_EXTENSIONS.get(suffix) in kinds:
                plain += 1
        self.log.emit(f"Расшифровать в extracted/: {len(jobs)}")
        self.log.emit(f"Скопировать незашифрованных в extracted/: {plain}")
        if self.params["text"]:
            self.log.emit("Собрать текст в translation/translation.jsonl")
        self.log.emit("Записать отчёт manifest.json")
        self.log.emit("")
        for job in jobs[:50]:
            self.log.emit(f"{job.source} -> {job.output}")
        if len(jobs) > 50:
            self.log.emit(f"... и ещё {len(jobs) - 50}")
        self.progress.emit(1, 1)
        return 0

    def _run_project(self) -> int:
        if self.cancel_requested:
            self.log.emit("Cancelled before start.")
            return 130
        options = ProjectBuildOptions(
            source=Path(self.params["input"]),
            output=Path(self.params["output"]),
            key=self.params["key"],
            engine=self.params["engine"],
            overwrite=self.params["overwrite"],
            include_runtime=self.params["include_runtime"],
            strict=self.params["strict"],
            workers=self.params["workers"] or 0,
        )
        result = build_editable_project(
            options,
            log=self.log.emit,
            progress=self.progress.emit,
            cancelled=lambda: self.cancel_requested,
        )
        self.result.emit(
            {
                "action": "project",
                "manifest": result.manifest,
                "output": str(result.output),
            }
        )
        if result.cancelled:
            return 130
        return 1 if result.errors else 0

    def _run_keys(self) -> int:
        root = normalize_path(Path(self.params["input"]))
        self.log.emit(f"Searching keys in: {root}")
        candidates = find_keys(root, max_text_mb=self.params["max_text_mb"])
        if not candidates:
            self.log.emit("No key candidates found.")
            self.result.emit({"action": "keys", "candidates": []})
            return 1

        for index, candidate in enumerate(candidates[:30], start=1):
            reasons = ", ".join(sorted(candidate.reasons))
            sources = "; ".join(candidate.sources[:3])
            self.log.emit(
                f"{index}. {display_key(candidate)} "
                f"format={candidate.key_format} score={candidate.score}"
            )
            self.log.emit(f"   reasons: {reasons}")
            self.log.emit(f"   sources: {sources}")

        if len(candidates) > 30:
            self.log.emit(f"... and {len(candidates) - 30} more candidates")
        self.result.emit({"action": "keys", "candidates": candidates})
        return 0

    def _resolve_key(self, input_path: Path, jobs: list[Any]) -> bytes | None:
        key_text = self.params["key"].strip()
        if key_text and key_text.lower() != "auto":
            self.log.emit("Using manual key.")
            return key_to_bytes(key_text)

        candidates = find_keys_including_game_root(
            input_path, max_text_mb=self.params["max_text_mb"]
        )
        if candidates:
            selected = candidates[0]
            self.log.emit(
                f"Using auto key: {display_key(selected)} "
                f"format={selected.key_format} score={selected.score}"
            )
            self.result.emit({"action": "keys", "candidates": candidates})
            return key_candidate_to_bytes(selected)

        needs_key = any(
            detect_rpg_header(read_prefix(job.source, HEADER_LENGTH))
            for job in jobs[: min(len(jobs), 50)]
        )
        if needs_key:
            raise RuntimeError("No key found. Enter a manual key or inspect plugins.")
        return None

    def _run_decrypt(self, dry_run: bool) -> int:
        input_path = normalize_path(Path(self.params["input"]))
        output_path = normalize_path(Path(self.params["output"]))
        kinds = self.params["kinds"]
        preserve_structure = self.params["preserve_structure"]
        jobs = collect_asset_jobs(input_path, output_path, kinds, preserve_structure)
        if not jobs:
            self.log.emit("No matching assets found.")
            return 1

        self.log.emit(f"Input: {input_path}")
        self.log.emit(f"Output: {output_path}")
        self.log.emit(
            "Структура папок: "
            + ("сохраняется" if preserve_structure else "не сохраняется (плоский вывод)")
        )
        self.log.emit(f"Assets: {len(jobs)}")

        if dry_run:
            for job in jobs[:50]:
                self.log.emit(f"{job.source} -> {job.output}")
            if len(jobs) > 50:
                self.log.emit(f"... and {len(jobs) - 50} more")
            self.progress.emit(len(jobs), len(jobs))
            return 0

        key = self._resolve_key(input_path, jobs)
        total = len(jobs)
        done = 0
        results = []
        workers = self.params["workers"]

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    decrypt_asset_job,
                    job,
                    key,
                    self.params["overwrite"],
                    False,
                    self.params["force_xor"],
                    self.params["strict"],
                    self.params["preserve_time"],
                )
                for job in jobs
            ]
            for future in as_completed(futures):
                if self.cancel_requested:
                    for pending in futures:
                        pending.cancel()
                    self.log.emit(
                        "Cancelled. Already written output files were left in the selected output folder."
                    )
                    return 130
                result = future.result()
                results.append(result)
                done += 1
                self.progress.emit(done, total)
                if result.status == "error" or self.params["verbose"]:
                    source = relpath(
                        result.source, input_path if input_path.is_dir() else input_path.parent
                    )
                    message = f" ({result.message})" if result.message else ""
                    self.log.emit(f"{result.status}: {source} -> {result.output}{message}")

        by_status = Counter(result.status for result in results)
        self.log.emit(
            "Done: "
            + ", ".join(f"{status}={count}" for status, count in sorted(by_status.items()))
        )
        self.result.emit({"action": "files", "output": str(output_path)})
        return 1 if by_status.get("error") else 0


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.worker_thread: QThread | None = None
        self.worker: ToolWorker | None = None
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.last_output: Path | None = None
        self.elapsed = QElapsedTimer()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(500)
        self.elapsed_timer.timeout.connect(self._update_elapsed)

        self.setWindowTitle(f"{APP_NAME} — {APP_SUBTITLE}")
        self.setWindowIcon(QIcon(app_icon_pixmap()))
        self.setAcceptDrops(True)
        self._build_ui()
        self._install_shortcuts()
        self._load_settings()
        self._apply_theme(THEME.name)
        self._set_busy(False)

    # ---------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_settings_area())
        splitter.addWidget(self._build_log_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([620, 230])
        self.splitter = splitter

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 14, 18, 14)
        body_layout.setSpacing(12)
        body_layout.addWidget(splitter, stretch=1)
        body_layout.addWidget(self._build_action_bar())
        root_layout.addWidget(body, stretch=1)

        self.setCentralWidget(central)
        self._apply_mode()
        self._update_structure_preview()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(12)

        logo = QLabel()
        logo.setPixmap(app_icon_pixmap(38))
        layout.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("AppSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        layout.addLayout(titles)
        layout.addStretch(1)

        self.engine_pill = StatusPill("Движок не определён")
        layout.addWidget(self.engine_pill)

        self.theme_button = QPushButton("Светлая тема")
        self.theme_button.setObjectName("Ghost")
        self.theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_button.clicked.connect(self._toggle_theme)
        layout.addWidget(self.theme_button)
        return header

    def _build_settings_area(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_source_card())
        layout.addWidget(self._build_mode_card())
        layout.addWidget(self._build_output_card())
        layout.addWidget(self._build_structure_card())
        layout.addWidget(self._build_content_card())
        layout.addWidget(self._build_advanced_card())
        layout.addStretch(1)

        area.setWidget(content)
        return area

    def _build_source_card(self) -> QWidget:
        card = Card("Игра")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Папка игры, APK или файл ассетов")
        self.input_edit.setClearButtonEnabled(True)
        self.input_button = QPushButton("Папка…")
        self.input_button.clicked.connect(self._choose_input)
        self.input_file_button = QPushButton("Файл…")
        self.input_file_button.clicked.connect(self._choose_input_file)
        row.addWidget(self.input_edit, stretch=1)
        row.addWidget(self.input_button)
        row.addWidget(self.input_file_button)
        card.add_layout(row)

        engine_row = QHBoxLayout()
        engine_row.setSpacing(8)
        self.engine_combo = QComboBox()
        self.engine_combo.addItems(
            [
                ENGINE_AUTO,
                ENGINE_RPGMAKER_MV,
                ENGINE_RPGMAKER_MZ,
                ENGINE_RPGMAKER_VX_ACE,
                ENGINE_WOLF_RPG,
                ENGINE_UNITY,
            ]
        )
        self.engine_combo.currentTextChanged.connect(self._apply_engine_mode)
        self.check_button = QPushButton("Проверить игру")
        self.check_button.setToolTip(
            "Определить движок, найти ключ шифрования и посчитать, сколько файлов зашифровано.\n"
            "Ничего не записывает."
        )
        self.check_button.clicked.connect(lambda: self._start_worker("check"))
        engine_row.addWidget(QLabel("Движок"))
        engine_row.addWidget(self.engine_combo, stretch=1)
        engine_row.addWidget(self.check_button)
        card.add_layout(engine_row)

        hint = QLabel("Папку или файл можно перетащить прямо в окно.")
        hint.setObjectName("Hint")
        card.add_widget(hint)

        self.input_edit.textChanged.connect(self._input_changed)
        return card

    def _build_mode_card(self) -> QWidget:
        card = Card("Что делаем")
        self.mode_control = SegmentedControl(
            [
                (
                    MODE_FILES,
                    "Только файлы",
                    "Расшифровать выбранные типы прямо в папку вывода",
                ),
                (
                    MODE_EXTRACT,
                    "Полное извлечение",
                    "Ассеты + текст для перевода + отчёт; единственный режим для Unity, WOLF и VX Ace",
                ),
                (
                    MODE_PROJECT,
                    "Проект для редактора",
                    "Собрать копию игры, которую открывает редактор RPG Maker",
                ),
            ]
        )
        self.mode_control.changed.connect(self._apply_mode)
        card.add_widget(self.mode_control)

        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("Hint")
        self.mode_hint.setWordWrap(True)
        card.add_widget(self.mode_hint)
        return card

    def _build_output_card(self) -> QWidget:
        card = Card("Куда сохранить")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        self.output_parent_edit = QLineEdit()
        self.output_parent_edit.setText(str(DEFAULT_OUTPUT_PARENT))
        self.output_parent_button = QPushButton("Выбрать…")
        self.output_parent_button.clicked.connect(self._choose_output_parent)
        grid.addWidget(QLabel("Папка"), 0, 0)
        grid.addWidget(self.output_parent_edit, 0, 1)
        grid.addWidget(self.output_parent_button, 0, 2)

        self.output_name_edit = QLineEdit()
        self.output_name_edit.setPlaceholderText("Название папки вывода")
        self.auto_name_check = QCheckBox("Авто по названию игры")
        self.auto_name_check.setChecked(True)
        self.auto_name_check.toggled.connect(self._update_auto_output_name)
        self.output_name_edit.textChanged.connect(self._update_output_preview)
        grid.addWidget(QLabel("Имя"), 1, 0)
        grid.addWidget(self.output_name_edit, 1, 1)
        grid.addWidget(self.auto_name_check, 1, 2)

        card.add_layout(grid)

        self.output_preview = QLabel("")
        self.output_preview.setObjectName("Hint")
        self.output_preview.setWordWrap(True)
        card.add_widget(self.output_preview)

        self.output_parent_edit.textChanged.connect(self._update_output_preview)
        return card

    def _build_structure_card(self) -> QWidget:
        card = Card("Структура папок")
        self.structure_card = card

        row = QHBoxLayout()
        row.setSpacing(12)
        self.structure_switch = ToggleSwitch()
        self.structure_switch.setChecked(True)
        self.structure_switch.toggled.connect(self._update_structure_preview)
        row.addWidget(self.structure_switch)

        labels = QVBoxLayout()
        labels.setSpacing(2)
        self.structure_title = QLabel("Сохранять структуру папок")
        self.structure_title.setStyleSheet("font-weight: 600;")
        self.structure_hint = QLabel("")
        self.structure_hint.setObjectName("Hint")
        self.structure_hint.setWordWrap(True)
        labels.addWidget(self.structure_title)
        labels.addWidget(self.structure_hint)
        row.addLayout(labels, stretch=1)
        card.add_layout(row)

        self.structure_example = QLabel("")
        self.structure_example.setObjectName("Hint")
        self.structure_example.setTextFormat(Qt.TextFormat.RichText)
        self.structure_example.setWordWrap(True)
        card.add_widget(self.structure_example)
        return card

    def _build_content_card(self) -> QWidget:
        card = Card("Что извлекать")
        row = QHBoxLayout()
        row.setSpacing(14)
        self.images_check = QCheckBox("Картинки")
        self.images_check.setChecked(True)
        self.text_check = QCheckBox("Текст")
        self.text_check.setChecked(True)
        self.audio_check = QCheckBox("Аудио")
        self.video_check = QCheckBox("Видео RPG")
        self.fonts_check = QCheckBox("Шрифты Unity")
        self.fonts_check.setChecked(True)
        for widget in (
            self.images_check,
            self.text_check,
            self.audio_check,
            self.video_check,
            self.fonts_check,
        ):
            row.addWidget(widget)
        row.addStretch(1)
        card.add_layout(row)
        return card

    def _build_advanced_card(self) -> QWidget:
        card = Card("Дополнительно", collapsible=True, expanded=False)
        self.advanced_card = card

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        self.key_label = QLabel("Ключ RPG Maker")
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("auto, 32-hex или raw:showDefault:eval")
        grid.addWidget(self.key_label, 0, 0)
        grid.addWidget(self.key_edit, 0, 1, 1, 3)

        self.unity_version_label = QLabel("Версия Unity")
        self.unity_version_edit = QLineEdit()
        self.unity_version_edit.setPlaceholderText("необязательно, например 2021.3.15f1")
        grid.addWidget(self.unity_version_label, 1, 0)
        grid.addWidget(self.unity_version_edit, 1, 1, 1, 3)

        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(0, 128)
        self.workers_spin.setSpecialValueText("auto")
        self.workers_spin.setValue(0)
        grid.addWidget(QLabel("Потоки"), 2, 0)
        grid.addWidget(self.workers_spin, 2, 1)
        card.add_layout(grid)

        self.overwrite_check = QCheckBox("Перезаписывать существующие файлы")
        self.strict_check = QCheckBox("Строгая проверка сигнатур")
        self.strict_check.setChecked(True)
        self.force_xor_check = QCheckBox("Force XOR (файлы без заголовка)")
        self.preserve_time_check = QCheckBox("Сохранять даты файлов")
        self.verbose_check = QCheckBox("Подробный лог")
        self.comments_check = QCheckBox("Извлекать комментарии")
        self.show_keys_check = QCheckBox("Диагностика с ключами")
        self.include_runtime_check = QCheckBox("Копировать движок игры (exe, dll) в проект")

        flags = QGridLayout()
        flags.setHorizontalSpacing(14)
        flags.setVerticalSpacing(6)
        for index, widget in enumerate(
            (
                self.overwrite_check,
                self.strict_check,
                self.force_xor_check,
                self.preserve_time_check,
                self.verbose_check,
                self.comments_check,
                self.show_keys_check,
                self.include_runtime_check,
            )
        ):
            flags.addWidget(widget, index // 2, index % 2)
        card.add_layout(flags)
        return card

    def _build_log_panel(self) -> QWidget:
        card = Card("Журнал")
        header = QHBoxLayout()
        header.addStretch(1)
        self.copy_log_button = QPushButton("Копировать")
        self.copy_log_button.setObjectName("Ghost")
        self.copy_log_button.clicked.connect(self._copy_log)
        self.clear_log_button = QPushButton("Очистить")
        self.clear_log_button.setObjectName("Ghost")
        self.clear_log_button.clicked.connect(lambda: self.log_edit.clear())
        header.addWidget(self.copy_log_button)
        header.addWidget(self.clear_log_button)
        card.add_layout(header)

        self.log_edit = LogView()
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card.add_widget(self.log_edit)
        return card

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("Готово к работе")
        self.status_label.setObjectName("Hint")
        self.status_label.setMinimumWidth(210)
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        status_row.addWidget(self.progress_bar, stretch=1)
        status_row.addWidget(self.status_label)
        layout.addLayout(status_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.preview_button = QPushButton("Предпросмотр")
        self.preview_button.setToolTip(
            "Показать, что именно будет сделано в текущем режиме. Ничего не записывает."
        )
        self.preview_button.clicked.connect(lambda: self._start_worker("preview"))
        self.open_output_button = QPushButton("Открыть результат")
        self.open_output_button.setToolTip("Открыть папку с результатом в проводнике")
        self.open_output_button.clicked.connect(self._open_output)
        self.open_output_button.setEnabled(False)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setToolTip("Остановить текущую операцию (Esc)")
        self.cancel_button.clicked.connect(self._cancel_worker)
        self.extract_all_button = QPushButton(MODE_BUTTON_LABELS[MODE_FILES])
        self.extract_all_button.setObjectName("Primary")
        self.extract_all_button.clicked.connect(self._run_primary)

        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.open_output_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.extract_all_button)
        layout.addLayout(buttons)
        return bar

    def _install_shortcuts(self) -> None:
        open_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        open_shortcut.activated.connect(self._choose_input)
        for sequence in ("Ctrl+Return", "Ctrl+Enter"):
            run_shortcut = QShortcut(QKeySequence(sequence), self)
            run_shortcut.activated.connect(self._run_primary)
        cancel_shortcut = QShortcut(QKeySequence("Esc"), self)
        cancel_shortcut.activated.connect(self._cancel_worker)

    # ----------------------------------------------------------------- theme

    def _apply_theme(self, name: str) -> None:
        THEME.use(name)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet())
        self.theme_button.setText("Светлая тема" if THEME.name == "dark" else "Тёмная тема")
        self.engine_pill.refresh()
        self._update_structure_preview()
        self.log_edit.rerender()
        for widget in self.findChildren(QWidget):
            widget.update()
        self.settings.setValue("theme", THEME.name)

    def _toggle_theme(self) -> None:
        self._apply_theme("light" if THEME.name == "dark" else "dark")

    # ------------------------------------------------------------ drag&drop

    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: Any) -> None:
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                self.input_edit.setText(local)
                self._update_auto_output_name()
                event.acceptProposedAction()
                return

    # ----------------------------------------------------------------- paths

    def _choose_input(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Выберите папку игры", self.input_edit.text().strip()
        )
        if not folder:
            return
        self.input_edit.setText(folder)
        if not self.output_parent_edit.text():
            self.output_parent_edit.setText(str(DEFAULT_OUTPUT_PARENT))
        self._update_auto_output_name()

    def _choose_input_file(self) -> None:
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Выберите Unity-архив или файл ассетов",
            self.input_edit.text().strip(),
            INPUT_FILE_FILTER,
        )
        if not filename:
            return
        self.input_edit.setText(filename)
        if not self.output_parent_edit.text():
            self.output_parent_edit.setText(str(DEFAULT_OUTPUT_PARENT))
        self._update_auto_output_name()

    def _choose_output_parent(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Выберите папку для результата", self.output_parent_edit.text().strip()
        )
        if folder:
            self.output_parent_edit.setText(folder)

    def _input_changed(self) -> None:
        if self.auto_name_check.isChecked():
            self._update_auto_output_name()
        self._update_output_preview()

    def _update_auto_output_name(self) -> None:
        if not self.auto_name_check.isChecked():
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        input_path = Path(text)
        suffix = "_project" if self._is_project_mode() else ""
        if input_path.is_file():
            self.output_name_edit.setText(f"{input_path.stem}{suffix or '_decoded'}")
        else:
            try:
                self.output_name_edit.setText(default_output_folder_name(input_path, suffix))
            except (OSError, ValueError):
                self.output_name_edit.setText(f"{input_path.name}{suffix or '_decoded'}")
        self._update_output_preview()

    def _output_path(self) -> Path:
        parent_text = self.output_parent_edit.text().strip() or str(DEFAULT_OUTPUT_PARENT)
        name_text = self.output_name_edit.text().strip()
        return Path(parent_text) / name_text

    def _update_output_preview(self) -> None:
        name_text = self.output_name_edit.text().strip()
        self.output_preview.setText(f"Итоговый путь: {self._output_path()}" if name_text else "")
        self.output_preview.setVisible(bool(name_text))
        self._update_structure_preview()

    def _update_structure_preview(self) -> None:
        preserve = self.structure_switch.isChecked()
        if preserve:
            self.structure_hint.setText(
                "Файлы раскладываются так же, как в игре: img/pictures, img/characters и т. д."
            )
        else:
            self.structure_hint.setText(
                "Все файлы складываются в одну папку — удобно, когда нужны просто картинки. "
                "Совпадающие имена дополняются названием исходной папки, дубликаты пропускаются."
            )
        accent = THEME.colors["accent"]
        muted = THEME.colors["muted"]
        example = describe_structure(preserve, STRUCTURE_EXAMPLE)
        self.structure_example.setText(
            f'<span style="color:{muted}">Пример: {STRUCTURE_EXAMPLE} →</span> '
            f'<span style="color:{accent}">images/{example}</span>'
        )

    # ----------------------------------------------------------------- state

    def _mode(self) -> str:
        return self.mode_control.value()

    def _is_project_mode(self) -> bool:
        return self._mode() == MODE_PROJECT

    def _run_primary(self) -> None:
        actions = {
            MODE_FILES: "files",
            MODE_EXTRACT: "extract-unified",
            MODE_PROJECT: "project",
        }
        self._start_worker(actions[self._mode()])

    def _apply_mode(self, _value: str | None = None) -> None:
        mode = self._mode()
        self.extract_all_button.setText(MODE_BUTTON_LABELS[mode])
        self.extract_all_button.setToolTip(MODE_OUTPUT_HINTS[mode])
        self.mode_hint.setText(MODE_OUTPUT_HINTS[mode])
        self._apply_engine_mode()
        self._update_auto_output_name()
        self._update_output_preview()

    def _apply_engine_mode(self) -> None:
        """Enable exactly the controls the current engine and mode can use."""

        engine = self.engine_combo.currentText()
        is_unity = engine == ENGINE_UNITY
        is_auto = engine == ENGINE_AUTO
        mode = self._mode()
        project = mode == MODE_PROJECT
        files_only = mode == MODE_FILES

        # Unity games can only be handled by the full extraction pipeline.
        self.mode_control.set_option_enabled(MODE_FILES, not is_unity)
        self.mode_control.set_option_enabled(MODE_PROJECT, not is_unity)
        if is_unity and mode != MODE_EXTRACT:
            self.mode_control.set_value(MODE_EXTRACT)
            return

        for widget in (
            self.key_label,
            self.key_edit,
            self.strict_check,
            self.force_xor_check,
        ):
            widget.setEnabled(not is_unity)
        self.preserve_time_check.setEnabled(not is_unity and not project)
        for widget in (self.comments_check, self.show_keys_check):
            widget.setEnabled(not is_unity and not project)

        unity_fields = (is_unity or is_auto) and not project
        self.unity_version_label.setEnabled(unity_fields)
        self.unity_version_edit.setEnabled(unity_fields)

        # A project is a full copy of the game, so the asset filters and the
        # flat layout have nothing to act on. "Only files" writes no text.
        self.images_check.setEnabled(not project)
        self.audio_check.setEnabled(not project)
        self.video_check.setEnabled(not is_unity and not project)
        self.text_check.setEnabled(not project and not files_only)
        self.fonts_check.setEnabled((is_unity or is_auto) and not project and not files_only)

        self.structure_card.setEnabled(not project)
        self.structure_switch.setEnabled(not project)
        self.include_runtime_check.setEnabled(project)

        if self.worker is None:
            self.preview_button.setEnabled(True)

    def _selected_kinds(self) -> set[str]:
        kinds: set[str] = set()
        if self.images_check.isChecked():
            kinds.add("image")
        if self.audio_check.isChecked():
            kinds.add("audio")
        if self.video_check.isChecked():
            kinds.add("video")
        return kinds

    def _params(self) -> dict[str, Any] | None:
        input_text = self.input_edit.text().strip()
        if not input_text or not Path(input_text).exists():
            QMessageBox.warning(self, "Папка игры", "Выберите существующую папку игры.")
            return None
        output_name = self.output_name_edit.text().strip()
        if not output_name:
            QMessageBox.warning(self, "Папка вывода", "Укажите название папки вывода.")
            return None
        kinds = self._selected_kinds()
        requested_engine = self.engine_combo.currentText()
        unity_detected = detect_unity_input(Path(input_text)).confidence >= 0.65
        unity_mode = requested_engine == ENGINE_UNITY or (
            requested_engine == ENGINE_AUTO and unity_detected
        )
        if self._is_project_mode():
            if Path(input_text).is_file():
                QMessageBox.warning(
                    self,
                    "Режим проекта",
                    "Выберите папку игры: собрать проект из одного файла нельзя.",
                )
                return None
            if requested_engine == ENGINE_UNITY:
                QMessageBox.warning(
                    self,
                    "Режим проекта",
                    "Режим проекта работает только с RPG Maker MV и MZ.",
                )
                return None
        elif unity_mode:
            if not any(
                (
                    self.images_check.isChecked(),
                    self.text_check.isChecked(),
                    self.audio_check.isChecked(),
                    self.fonts_check.isChecked(),
                )
            ):
                QMessageBox.warning(self, "Типы", "Выберите хотя бы один тип Unity-ассетов.")
                return None
        elif not kinds:
            QMessageBox.warning(self, "Типы", "Выберите хотя бы один тип ассетов.")
            return None

        return {
            "input": input_text,
            "output": str(self._output_path()),
            "engine": requested_engine,
            "key": self.key_edit.text().strip() or "auto",
            "kinds": kinds,
            "workers": self.workers_spin.value() or None,
            "overwrite": self.overwrite_check.isChecked(),
            "strict": self.strict_check.isChecked(),
            "force_xor": self.force_xor_check.isChecked(),
            "preserve_time": self.preserve_time_check.isChecked(),
            "preserve_structure": self.structure_switch.isChecked(),
            "mode": self._mode(),
            "text": self.text_check.isChecked(),
            "include_runtime": self.include_runtime_check.isChecked(),
            "verbose": self.verbose_check.isChecked(),
            "comments": self.comments_check.isChecked(),
            "show_keys": self.show_keys_check.isChecked(),
            "max_text_mb": 25,
            "unity_images": self.images_check.isChecked(),
            "unity_text": self.text_check.isChecked(),
            "unity_audio": self.audio_check.isChecked(),
            "unity_fonts": self.fonts_check.isChecked(),
            "unity_version": self.unity_version_edit.text().strip(),
        }

    # ---------------------------------------------------------------- worker

    def _start_worker(self, action: str) -> None:
        if self.worker is not None:
            return
        params = self._params()
        if params is None:
            return
        unity_mode = params["engine"] == ENGINE_UNITY or (
            params["engine"] == ENGINE_AUTO
            and detect_unity_input(Path(params["input"])).confidence >= 0.65
        )
        if unity_mode and action in {"files", "project"}:
            QMessageBox.information(
                self,
                "Unity",
                "Для Unity доступен только режим «Полное извлечение».",
            )
            return
        self.log_edit.clear()
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("Работаем…")
        self.elapsed.restart()
        self.elapsed_timer.start()
        self._set_busy(True)

        self.worker_thread = QThread()
        self.worker = ToolWorker(action, params)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self._append_log)
        self.worker.progress.connect(self._set_progress)
        self.worker.result.connect(self._handle_worker_result)
        self.worker.finished.connect(self._worker_finished)
        self.worker.finished.connect(lambda _code: self.worker_thread.quit())
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self._thread_finished)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    def _cancel_worker(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.status_label.setText("Отмена…")

    def _set_progress(self, done: int, total: int) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        percent = int(done * 100 / total) if total else 0
        self.status_label.setText(f"{done} / {total} · {percent}% · {self._elapsed_text()}")

    def _elapsed_text(self) -> str:
        if not self.elapsed.isValid():
            return "00:00"
        seconds = self.elapsed.elapsed() // 1000
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _update_elapsed(self) -> None:
        if self.worker is None:
            return
        if self.progress_bar.maximum() == 0:
            self.status_label.setText(f"Работаем… {self._elapsed_text()}")

    def _append_log(self, message: str) -> None:
        self.log_edit.append_line(message)

    def _handle_worker_result(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        action = result.get("action")
        if action == "inspect":
            detection = result.get("detection")
            if detection is not None:
                edition = f", {detection.edition}" if detection.edition else ""
                state = "ok" if detection.confidence >= 0.65 else (
                    "warn" if detection.confidence >= 0.35 else "error"
                )
                self.engine_pill.set_state(
                    f"{detection.engine} · {detection.confidence:.2f}{edition}", state
                )
            return
        if action in {"extract-unified", "files", "project"}:
            output = result.get("output")
            if output:
                self.last_output = Path(output)
                self.open_output_button.setEnabled(True)
            manifest = result.get("manifest") or {}
            if manifest:
                edition = f", {manifest.get('edition')}" if manifest.get("edition") else ""
                confidence = manifest.get("detection_confidence", 0) or 0
                self.engine_pill.set_state(
                    f"{manifest.get('engine')} · {confidence:.2f}{edition}",
                    "ok" if not manifest.get("errors") else "warn",
                )
            return
        if action != "keys":
            return
        candidates = result.get("candidates") or []
        if candidates:
            self.key_edit.setText(display_key(candidates[0]))
            if self.advanced_card is not None and not self.advanced_card.is_expanded():
                self.advanced_card.set_expanded(True)

    def _worker_finished(self, code: int) -> None:
        self.elapsed_timer.stop()
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
        labels = {0: "Готово", 130: "Отменено"}
        self.status_label.setText(f"{labels.get(code, f'Код {code}')} · {self._elapsed_text()}")
        self._append_log(f"Finished with code {code}.")
        self._set_busy(False)

    def _thread_finished(self) -> None:
        self.worker = None
        self.worker_thread = None
        self._apply_engine_mode()

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.input_button,
            self.input_file_button,
            self.output_parent_button,
            self.check_button,
            self.preview_button,
            self.extract_all_button,
            self.structure_switch,
            self.mode_control,
        ):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if not busy:
            self._apply_engine_mode()

    # --------------------------------------------------------------- helpers

    def _copy_log(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.log_edit.toPlainText())

    def _open_output(self) -> None:
        target = self.last_output or self._output_path()
        if not target.exists():
            QMessageBox.information(self, "Результат", f"Папка ещё не создана:\n{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # -------------------------------------------------------------- settings

    def _load_settings(self) -> None:
        settings = self.settings
        THEME.use(str(settings.value("theme", "dark")))
        geometry = settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.input_edit.setText(str(settings.value("input", "")))
        self.output_parent_edit.setText(
            str(settings.value("output_parent", str(DEFAULT_OUTPUT_PARENT)))
        )
        self.engine_combo.setCurrentText(str(settings.value("engine", ENGINE_AUTO)))
        self.mode_control.set_value(str(settings.value("mode", MODE_FILES)))
        for key, widget in self._persisted_checks().items():
            stored = settings.value(key)
            if stored is not None:
                widget.setChecked(str(stored).lower() in {"true", "1"})
        self.workers_spin.setValue(int(settings.value("workers", 0)))
        self._update_structure_preview()

    def _save_settings(self) -> None:
        settings = self.settings
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("theme", THEME.name)
        settings.setValue("input", self.input_edit.text().strip())
        settings.setValue("output_parent", self.output_parent_edit.text().strip())
        settings.setValue("engine", self.engine_combo.currentText())
        settings.setValue("mode", self._mode())
        settings.setValue("workers", self.workers_spin.value())
        for key, widget in self._persisted_checks().items():
            settings.setValue(key, widget.isChecked())

    def _persisted_checks(self) -> dict[str, Any]:
        return {
            "preserve_structure": self.structure_switch,
            "auto_name": self.auto_name_check,
            "images": self.images_check,
            "text": self.text_check,
            "audio": self.audio_check,
            "video": self.video_check,
            "fonts": self.fonts_check,
            "overwrite": self.overwrite_check,
            "strict": self.strict_check,
            "force_xor": self.force_xor_check,
            "preserve_time": self.preserve_time_check,
            "verbose": self.verbose_check,
            "comments": self.comments_check,
            "show_keys": self.show_keys_check,
            "include_runtime": self.include_runtime_check,
        }

    def closeEvent(self, event: Any) -> None:
        self._save_settings()
        if self.worker is not None:
            self.worker.cancel()
        if self.worker_thread is not None:
            self.worker_thread.quit()
            self.worker_thread.wait(3000)
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(SETTINGS_ORG)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(app_icon_pixmap()))
    window = MainWindow()
    window.resize(1040, 780)
    window.setMinimumSize(820, 620)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
