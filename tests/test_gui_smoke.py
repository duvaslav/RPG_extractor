#!/usr/bin/env python3
"""GUI smoke tests. Skipped automatically when PyQt6 is not installed."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from PyQt6.QtWidgets import QApplication
except Exception as exc:  # pragma: no cover - depends on the environment
    QApplication = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(QApplication is None, f"PyQt6 unavailable: {_IMPORT_ERROR}")
class GuiSmokeTests(unittest.TestCase):
    app: object = None

    @classmethod
    def setUpClass(cls) -> None:
        import rpg_maker_gui

        cls.gui = rpg_maker_gui
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from PyQt6.QtCore import QSettings

        # The window remembers its last mode, so start every test from a clean slate.
        QSettings(self.gui.SETTINGS_ORG, self.gui.SETTINGS_APP).clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.game = Path(self._tmp.name) / "Game"
        (self.game / "www" / "img" / "pictures").mkdir(parents=True)
        self.window = self.gui.MainWindow()
        self.window.input_edit.setText(str(self.game))
        self.window.output_name_edit.setText("Game")
        self.window.output_parent_edit.setText(str(Path(self._tmp.name) / "out"))

    def tearDown(self) -> None:
        self.window.close()
        self._tmp.cleanup()

    def test_structure_switch_reaches_the_worker_params(self) -> None:
        self.window.structure_switch.setChecked(True)
        params = self.window._params()
        self.assertIsNotNone(params)
        self.assertTrue(params["preserve_structure"])

        self.window.structure_switch.setChecked(False)
        params = self.window._params()
        self.assertIsNotNone(params)
        self.assertFalse(params["preserve_structure"])

    def test_structure_preview_follows_the_switch(self) -> None:
        self.window.structure_switch.setChecked(True)
        self.assertIn("img/pictures/Actor1.png", self.window.structure_example.text())
        self.window.structure_switch.setChecked(False)
        self.assertIn("images/Actor1.png", self.window.structure_example.text())

    def test_theme_switch_keeps_the_log_readable(self) -> None:
        self.window.log_edit.append_line("Engine: rpgmaker-mz")
        first_theme = self.gui.THEME.name
        self.window._toggle_theme()
        self.assertNotEqual(self.gui.THEME.name, first_theme)
        self.assertIn("Engine: rpgmaker-mz", self.window.log_edit.toPlainText())
        self.window._toggle_theme()

    def test_project_mode_switches_the_run_target(self) -> None:
        self.window.mode_control.set_value(self.gui.MODE_PROJECT)
        self.assertTrue(self.window._is_project_mode())
        self.assertEqual(self.window.extract_all_button.text(), "Собрать проект")
        self.assertFalse(self.window.structure_switch.isEnabled())
        self.assertTrue(self.window.include_runtime_check.isEnabled())

        params = self.window._params()
        self.assertIsNotNone(params)
        self.assertEqual(params["mode"], self.gui.MODE_PROJECT)

        self.window.mode_control.set_value(self.gui.MODE_FILES)
        self.assertEqual(self.window.extract_all_button.text(), "Расшифровать файлы")
        self.assertTrue(self.window.structure_switch.isEnabled())

    def test_every_mode_has_a_label_a_hint_and_an_action(self) -> None:
        for mode in (self.gui.MODE_FILES, self.gui.MODE_EXTRACT, self.gui.MODE_PROJECT):
            with self.subTest(mode=mode):
                self.window.mode_control.set_value(mode)
                self.assertEqual(
                    self.window.extract_all_button.text(), self.gui.MODE_BUTTON_LABELS[mode]
                )
                self.assertIn("Что получится", self.window.mode_hint.text())
                params = self.window._params()
                self.assertIsNotNone(params)
                self.assertEqual(params["mode"], mode)

    def test_text_checkbox_only_applies_to_full_extraction(self) -> None:
        self.window.mode_control.set_value(self.gui.MODE_EXTRACT)
        self.assertTrue(self.window.text_check.isEnabled())
        self.window.mode_control.set_value(self.gui.MODE_FILES)
        self.assertFalse(self.window.text_check.isEnabled())

    def test_unity_engine_forces_full_extraction(self) -> None:
        self.window.mode_control.set_value(self.gui.MODE_FILES)
        self.window.engine_combo.setCurrentText(self.gui.ENGINE_UNITY)
        self.assertEqual(self.window._mode(), self.gui.MODE_EXTRACT)
        self.assertFalse(self.window.mode_control._buttons[self.gui.MODE_PROJECT].isEnabled())
        self.window.engine_combo.setCurrentText("auto")
        self.assertTrue(self.window.mode_control._buttons[self.gui.MODE_PROJECT].isEnabled())

    def test_project_mode_names_the_folder_after_the_game(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_project_mode import make_deployed_mv_game

        game = make_deployed_mv_game(Path(self._tmp.name))
        self.window.mode_control.set_value(self.gui.MODE_FILES)
        self.window.auto_name_check.setChecked(True)
        self.window.input_edit.setText(str(game / "www" / "img"))
        self.assertEqual(self.window.output_name_edit.text(), "Marie's Adventure")
        self.window.mode_control.set_value(self.gui.MODE_PROJECT)
        self.assertEqual(self.window.output_name_edit.text(), "Marie's Adventure_project")

    def test_both_themes_produce_a_stylesheet(self) -> None:
        for name in ("dark", "light"):
            self.gui.THEME.use(name)
            self.assertIn("QPushButton", self.gui.build_stylesheet())


if __name__ == "__main__":
    unittest.main()
