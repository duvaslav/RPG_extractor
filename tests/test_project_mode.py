#!/usr/bin/env python3
"""Tests for output naming from a subfolder and for the editable-project build."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpg_maker_tool import (  # noqa: E402
    MV_HEADER,
    ProjectBuildOptions,
    build_editable_project,
    default_output_folder_name,
    encrypt_for_self_test,
    find_game_root_upwards,
    find_keys_including_game_root,
    is_runtime_file,
    meaningful_folder_name,
    project_content_root,
)

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"picture"
OGG = b"OggS" + b"\x00" * 12 + b"sound"
KEY_HEX = "00112233445566778899aabbccddeeff"
KEY = bytes.fromhex(KEY_HEX)


def make_deployed_mv_game(parent: Path, title: str = "Marie's Adventure") -> Path:
    """A deployed MV game: everything under www/, plus the nw.js runtime."""

    root = parent / title
    www = root / "www"
    for folder in ("data", "img/pictures", "img/characters", "audio/bgm", "js/plugins", "icon"):
        (www / folder).mkdir(parents=True, exist_ok=True)
    (www / "data" / "System.json").write_text(
        json.dumps(
            {
                "gameTitle": title,
                "hasEncryptedImages": True,
                "hasEncryptedAudio": True,
                "encryptionKey": KEY_HEX,
            }
        ),
        encoding="utf-8",
    )
    (www / "data" / "Map001.json").write_text(json.dumps({"events": [None]}), encoding="utf-8")
    (www / "js" / "rpg_core.js").write_text("// core\n", encoding="utf-8")
    (www / "js" / "plugins" / "Plugin.js").write_text("// plugin\n", encoding="utf-8")
    (www / "img" / "pictures" / "Actor1.rpgmvp").write_bytes(
        encrypt_for_self_test(PNG, KEY, MV_HEADER)
    )
    (www / "img" / "characters" / "Actor1.rpgmvp").write_bytes(
        encrypt_for_self_test(PNG + b"2", KEY, MV_HEADER)
    )
    (www / "audio" / "bgm" / "Theme.rpgmvo").write_bytes(
        encrypt_for_self_test(OGG, KEY, MV_HEADER)
    )
    (www / "icon" / "icon.png").write_bytes(PNG)
    (www / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "game", "main": "www/index.html"}), encoding="utf-8"
    )
    # nw.js runtime that should not end up in the project.
    (root / "Game.exe").write_bytes(b"MZ\x00runtime")
    (root / "nw.dll").write_bytes(b"dll")
    (root / "locales").mkdir(exist_ok=True)
    (root / "locales" / "en-US.pak").write_bytes(b"pak")
    return root


class OutputNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_deployed_mv_game(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_game_root_is_found_from_a_subfolder(self) -> None:
        found = find_game_root_upwards(self.root / "www" / "img" / "pictures")
        self.assertEqual(found, self.root.resolve())

    def test_name_from_a_subfolder_uses_the_game_title(self) -> None:
        for folder in ("www", "www/img", "www/img/pictures", "www/audio/bgm"):
            with self.subTest(folder=folder):
                self.assertEqual(
                    default_output_folder_name(self.root / folder), "Marie's Adventure"
                )

    def test_name_from_the_game_root_is_unchanged(self) -> None:
        self.assertEqual(default_output_folder_name(self.root), "Marie's Adventure")

    def test_project_suffix(self) -> None:
        self.assertEqual(
            default_output_folder_name(self.root / "www" / "img", "_project"),
            "Marie's Adventure_project",
        )

    def test_untitled_game_falls_back_to_the_folder_above_the_subfolder(self) -> None:
        root = Path(self._tmp.name) / "Some Game"
        (root / "www" / "img" / "pictures").mkdir(parents=True)
        (root / "www" / "js").mkdir(parents=True)
        (root / "www" / "js" / "rpg_core.js").write_text("// core\n", encoding="utf-8")
        self.assertEqual(default_output_folder_name(root / "www" / "img"), "Some Game")

    def test_meaningful_folder_name_skips_game_subfolders(self) -> None:
        self.assertEqual(meaningful_folder_name(self.root / "www" / "img"), self.root.name)

    def test_key_is_found_from_a_subfolder(self) -> None:
        candidates = find_keys_including_game_root(self.root / "www" / "img" / "pictures")
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].key, KEY_HEX)


class ProjectBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_deployed_mv_game(Path(self._tmp.name))
        self.out = Path(self._tmp.name) / "Marie's Adventure_project"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, **kwargs: object) -> object:
        options = ProjectBuildOptions(source=self.root, output=self.out, **kwargs)  # type: ignore[arg-type]
        return build_editable_project(options)

    def test_www_is_flattened_into_the_project_root(self) -> None:
        self.build()
        for expected in ("data/System.json", "js/rpg_core.js", "index.html", "icon/icon.png"):
            with self.subTest(path=expected):
                self.assertTrue((self.out / expected).is_file())
        self.assertFalse((self.out / "www").exists())

    def test_assets_are_decrypted_in_place(self) -> None:
        result = self.build()
        self.assertEqual((self.out / "img" / "pictures" / "Actor1.png").read_bytes(), PNG)
        self.assertEqual((self.out / "audio" / "bgm" / "Theme.ogg").read_bytes(), OGG)
        self.assertFalse(list(self.out.rglob("*.rpgmvp")))
        self.assertEqual(result.files_decrypted, 3)
        self.assertEqual(result.errors, [])

    def test_encryption_flags_are_cleared(self) -> None:
        self.build()
        system = json.loads((self.out / "data" / "System.json").read_text(encoding="utf-8"))
        self.assertFalse(system["hasEncryptedImages"])
        self.assertFalse(system["hasEncryptedAudio"])
        self.assertNotIn("encryptionKey", system)

    def test_project_file_and_package_main(self) -> None:
        self.build()
        self.assertTrue((self.out / "Game.rpgproject").is_file())
        package = json.loads((self.out / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["main"], "index.html")

    def test_runtime_is_left_out_by_default(self) -> None:
        self.build()
        self.assertFalse((self.out / "Game.exe").exists())
        self.assertFalse((self.out / "nw.dll").exists())
        self.assertFalse((self.out / "locales").exists())

    def test_runtime_can_be_included(self) -> None:
        self.build(include_runtime=True)
        self.assertTrue((self.out / "Game.exe").is_file())

    def test_existing_project_file_is_not_replaced(self) -> None:
        (self.root / "Game.rpgproject").write_text("RPGMV 1.5.0\n", encoding="utf-8")
        self.build()
        self.assertEqual(
            (self.out / "Game.rpgproject").read_text(encoding="utf-8").strip(), "RPGMV 1.5.0"
        )

    def test_building_from_a_subfolder_still_builds_the_whole_game(self) -> None:
        options = ProjectBuildOptions(source=self.root / "www" / "img", output=self.out)
        result = build_editable_project(options)
        self.assertTrue((self.out / "data" / "System.json").is_file())
        self.assertEqual(result.manifest["source_root"], str(self.root.resolve()))

    def test_second_run_skips_existing_files_unless_overwriting(self) -> None:
        self.build()
        again = self.build()
        self.assertEqual(again.files_copied, 0)
        self.assertEqual(again.files_decrypted, 0)
        self.assertGreater(again.files_skipped, 0)
        overwritten = self.build(overwrite=True)
        self.assertGreater(overwritten.files_copied, 0)

    def test_manifest_is_written(self) -> None:
        self.build()
        manifest = json.loads((self.out / "project_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "project")
        self.assertEqual(manifest["engine"], "rpgmaker-mv")
        self.assertTrue(manifest["key_detected"])
        self.assertTrue(manifest["encryption_flags_cleared"])

    def test_non_rpgmaker_input_is_rejected(self) -> None:
        empty = Path(self._tmp.name) / "NotAGame"
        empty.mkdir()
        with self.assertRaises(ValueError):
            build_editable_project(
                ProjectBuildOptions(source=empty, output=Path(self._tmp.name) / "p2")
            )


class HelperTests(unittest.TestCase):
    def test_runtime_detection(self) -> None:
        self.assertTrue(is_runtime_file(Path("Game.exe")))
        self.assertTrue(is_runtime_file(Path("nw.dll")))
        self.assertTrue(is_runtime_file(Path("locales/en-US.pak")))
        self.assertTrue(is_runtime_file(Path("swiftshader/libGLESv2.dll")))
        self.assertFalse(is_runtime_file(Path("data/System.json")))
        self.assertFalse(is_runtime_file(Path("img/pictures/Actor1.png")))

    def test_content_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_deployed_mv_game(Path(tmp))
            self.assertEqual(project_content_root(root), root / "www")
            mz_root = Path(tmp) / "MzGame"
            (mz_root / "data").mkdir(parents=True)
            self.assertEqual(project_content_root(mz_root), mz_root)


if __name__ == "__main__":
    unittest.main()
