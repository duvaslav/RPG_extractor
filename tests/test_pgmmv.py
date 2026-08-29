#!/usr/bin/env python3
"""Pixel Game Maker MV: detection, resource triage and project text."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pgmmv  # noqa: E402
from rpg_maker_tool import (  # noqa: E402
    ENGINE_PIXEL_GAME_MAKER_MV,
    ENGINE_UNKNOWN,
    ENGINE_WOLF_RPG,
    ExtractionOptions,
    copy_plain_assets,
    detect_engine,
    run_unified_extraction,
)

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"plain-picture"
OGG = b"OggS" + b"\x00" * 12 + b"plain-sound"
TTF = b"\x00\x01\x00\x00" + b"plain-font"
PROTECTED = b"enc" + bytes(range(32))

TEXT_LIST = [
    {
        "id": 1,
        "name": "greeting",
        "en_US": "Hello there",
        "zh_CN": "你好",
        "zh_TW": "你好嗎",
        "font": "default",
    },
    {
        "id": 2,
        "name": "sign",
        "en_US": "Push \\V[1] to open",
        "zh_CN": "",
        "zh_TW": "推開",
    },
]


def make_pgmmv_game(parent: Path, filler_mb: int = 0) -> Path:
    """A deployed Pixel Game Maker MV game with mixed protected/plain resources."""

    root = parent / "PGMMV Game"
    resources = root / "Resources"
    for folder in ("data", "img", "sound", "font", "plugins"):
        (resources / folder).mkdir(parents=True, exist_ok=True)

    project: dict[str, object] = {"version": "1.0.6", "projectName": "Test"}
    if filler_mb:
        project["filler"] = ["x" * 1024 for _ in range(filler_mb * 1024)]
    project["textList"] = TEXT_LIST
    (resources / "data" / "project.json").write_text(
        json.dumps(project, ensure_ascii=False), encoding="utf-8"
    )
    (resources / "data" / "info.json").write_text(
        json.dumps({"resourceEncryptKey": "abcdef0123456789", "format": 3}), encoding="utf-8"
    )
    (root / "Game.exe").write_bytes(
        b"MZ\x90\x00" + b"\x00" * 64 + b"Pixel Game Maker MV player 1.0.6.3\x00" + b"\x00" * 32
    )

    (resources / "img" / "plain.png").write_bytes(PNG)
    (resources / "img" / "hero.png").write_bytes(PROTECTED)
    (resources / "img" / "tiles.png").write_bytes(PROTECTED)
    (resources / "sound" / "bgm.ogg").write_bytes(OGG)
    (resources / "sound" / "voice.ogg").write_bytes(PROTECTED)
    (resources / "font" / "main.ttf").write_bytes(TTF)
    return root


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_pgmmv_game(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_engine_is_identified_with_versions(self) -> None:
        result = detect_engine(self.root)
        self.assertEqual(result.engine, ENGINE_PIXEL_GAME_MAKER_MV)
        self.assertGreaterEqual(result.confidence, 0.90)
        self.assertIn("project 1.0.6", result.edition or "")
        self.assertIn("player 1.0.6.3", result.edition or "")

    def test_not_reported_as_wolf_rpg(self) -> None:
        self.assertNotEqual(detect_engine(self.root).engine, ENGINE_WOLF_RPG)

    def test_protection_key_metadata_is_noticed_without_leaking_it(self) -> None:
        detection = pgmmv.detect_pixel_game_maker(self.root)
        self.assertTrue(detection.has_protection_key)
        self.assertNotIn("abcdef0123456789", " ".join(detection.evidence))

    def test_a_bare_game_exe_is_not_wolf_rpg(self) -> None:
        stranger = Path(self._tmp.name) / "Mystery"
        (stranger / "Data").mkdir(parents=True)
        (stranger / "Game.exe").write_bytes(b"MZ\x90\x00unknown engine")
        result = detect_engine(stranger)
        self.assertEqual(result.engine, ENGINE_UNKNOWN)
        self.assertTrue(result.warnings)
        self.assertNotIn("rpgmaker-mv,", " ".join(result.warnings))

    def test_manual_override_still_works(self) -> None:
        result = detect_engine(self.root, ENGINE_PIXEL_GAME_MAKER_MV)
        self.assertEqual(result.engine, ENGINE_PIXEL_GAME_MAKER_MV)


class ResourceTriageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_pgmmv_game(Path(self._tmp.name))
        self.resources = self.root / "Resources"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_protected_files_are_classified_by_content(self) -> None:
        state, kind, magic = pgmmv.classify_file(self.resources / "img" / "hero.png")
        self.assertEqual((state, kind, magic), ("protected", "image", "enc"))

    def test_plain_files_are_classified_by_content(self) -> None:
        self.assertEqual(
            pgmmv.classify_file(self.resources / "img" / "plain.png")[:2], ("plain", "image")
        )
        self.assertEqual(
            pgmmv.classify_file(self.resources / "font" / "main.ttf")[:2], ("plain", "font")
        )

    def test_scan_counts_protected_and_plain_separately(self) -> None:
        scan = pgmmv.scan_resources(self.resources)
        self.assertEqual(scan.protected, {"image": 2, "audio": 1})
        self.assertEqual(scan.plain, {"image": 1, "audio": 1, "font": 1})
        self.assertEqual(scan.protected_magics, {"enc": 3})

    def test_plain_copy_refuses_protected_files(self) -> None:
        out = Path(self._tmp.name) / "out"
        copied = copy_plain_assets(self.resources, out, {"image", "audio", "font"})
        self.assertEqual(copied["total"], 3)
        self.assertEqual(copied["protected"], 3)
        self.assertEqual(
            sorted(path.name for path in out.rglob("*") if path.is_file()),
            ["bgm.ogg", "main.ttf", "plain.png"],
        )


class ProjectTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_pgmmv_game(Path(self._tmp.name), filler_mb=2)
        self.project_json = self.root / "Resources" / "data" / "project.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_large_project_json_is_not_skipped(self) -> None:
        self.assertGreater(self.project_json.stat().st_size, 2 * 1024 * 1024)
        value = pgmmv.find_json_value(self.project_json, "textList")
        self.assertIsInstance(value, list)
        self.assertEqual(len(value), 2)

    def test_locales_are_exported_with_context_and_tags(self) -> None:
        records, counts, warnings = pgmmv.extract_project_text(self.project_json)
        self.assertEqual(warnings, [])
        self.assertEqual(counts, {"en_US": 2, "zh_CN": 2, "zh_TW": 2})
        by_locale = {(record.entry_id, record.locale): record for record in records}
        self.assertEqual(by_locale[("1", "en_US")].text, "Hello there")
        # Pixel Game Maker tags must survive untouched.
        self.assertEqual(by_locale[("2", "en_US")].text, "Push \\V[1] to open")
        self.assertTrue(by_locale[("1", "en_US")].context.startswith("textList"))

    def test_missing_text_list_is_reported_not_silent(self) -> None:
        empty = Path(self._tmp.name) / "empty.json"
        empty.write_text(json.dumps({"version": "1.0.6"}), encoding="utf-8")
        records, counts, warnings = pgmmv.extract_project_text(empty)
        self.assertEqual(records, [])
        self.assertEqual(counts, {})
        self.assertTrue(warnings)


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_pgmmv_game(Path(self._tmp.name))
        self.out = Path(self._tmp.name) / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_extraction_takes_plain_assets_and_text(self) -> None:
        result = run_unified_extraction(
            ExtractionOptions(
                source=self.root,
                output=self.out,
                images=True,
                text=True,
                resources=True,
                asset_kinds={"image", "audio", "font"},
            )
        )
        self.assertEqual(result.engine, ENGINE_PIXEL_GAME_MAKER_MV)
        written = sorted(
            path.name for path in (self.out / "extracted").rglob("*") if path.is_file()
        )
        self.assertEqual(written, ["bgm.ogg", "main.ttf", "plain.png"])
        for path in (self.out / "extracted").rglob("*"):
            if path.is_file():
                self.assertFalse(path.read_bytes().startswith(b"enc"))

        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["engine"], ENGINE_PIXEL_GAME_MAKER_MV)
        self.assertEqual(manifest["project_version"], "1.0.6")
        self.assertEqual(manifest["player_version"], "1.0.6.3")
        self.assertEqual(manifest["protected_by_kind"], {"audio": 1, "image": 2})
        self.assertEqual(manifest["plain_by_kind"], {"audio": 1, "font": 1, "image": 1})
        self.assertEqual(manifest["protected_total"], 3)
        self.assertEqual(manifest["locales"], {"en_US": 2, "zh_CN": 2, "zh_TW": 2})
        self.assertIn("duration_seconds", manifest)
        self.assertEqual(manifest["key_status"], "metadata present, format unsupported")
        self.assertTrue(any("not supported yet" in item for item in manifest["warnings"]))

        translation = (self.out / "translation" / "translation.jsonl").read_text(encoding="utf-8")
        self.assertIn("Hello there", translation)
        self.assertIn("\\V[1]", translation)

    def test_selected_kinds_are_respected(self) -> None:
        run_unified_extraction(
            ExtractionOptions(
                source=self.root,
                output=self.out,
                images=True,
                text=False,
                resources=True,
                asset_kinds={"image"},
            )
        )
        written = sorted(
            path.name for path in (self.out / "extracted").rglob("*") if path.is_file()
        )
        self.assertEqual(written, ["plain.png"])


if __name__ == "__main__":
    unittest.main()
