#!/usr/bin/env python3
"""Finding, pinning and reporting the WOLF RPG console backends."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bundled_tools  # noqa: E402
from bundled_tools import UBERWOLF, WOLFTL  # noqa: E402
from rpg_maker_tool import ExtractionResult, find_executable  # noqa: E402

FAKE_EXE = b"fake UberWolfCli"
FAKE_SHA = "f1cb9c1b8c0e50d0d4b3f6a7d61cf2ec5b7a1b6f0b5a3c7e2e9f2ab0c1d2e3f4"


class SearchOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self._env = dict(os.environ)
        for spec in bundled_tools.ALL_TOOLS:
            os.environ.pop(spec.env_name, None)
            os.environ.pop(f"{spec.env_name}_SHA256", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _make_tool(self, folder: Path, name: str = "UberWolfCli.exe") -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(FAKE_EXE)
        return path

    def test_one_file_build_looks_in_meipass_first(self) -> None:
        bundled = self._make_tool(self.base / "meipass" / "tools")
        self._make_tool(self.base / "beside_exe" / "tools")
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=self.base / "meipass"), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=True), \
             mock.patch.object(bundled_tools, "executable_dir", return_value=self.base / "beside_exe"), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base / "src"):
            status = bundled_tools.locate_tool(UBERWOLF)
        self.assertEqual(status.path, bundled.resolve())
        self.assertEqual(status.source, "bundled (one-file)")

    def test_portable_build_looks_next_to_the_executable(self) -> None:
        beside = self._make_tool(self.base / "beside_exe" / "tools")
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=True), \
             mock.patch.object(bundled_tools, "executable_dir", return_value=self.base / "beside_exe"), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base / "src"):
            status = bundled_tools.locate_tool(UBERWOLF)
        self.assertEqual(status.path, beside.resolve())
        self.assertEqual(status.source, "next to the executable")

    def test_environment_variable_is_used_when_no_bundle_exists(self) -> None:
        loose = self._make_tool(self.base / "elsewhere")
        os.environ[UBERWOLF.env_name] = str(loose)
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=False), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base / "src"):
            status = bundled_tools.locate_tool(UBERWOLF)
        self.assertEqual(status.path, loose.resolve())
        self.assertEqual(status.source, f"${UBERWOLF.env_name}")

    def test_explicit_path_wins(self) -> None:
        self._make_tool(self.base / "meipass" / "tools")
        explicit = self._make_tool(self.base / "chosen")
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=self.base / "meipass"):
            status = bundled_tools.locate_tool(UBERWOLF, explicit)
        self.assertEqual(status.path, explicit.resolve())
        self.assertEqual(status.source, "--explicit path")

    def test_missing_tool_reports_where_to_put_it(self) -> None:
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=False), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base / "src"), \
             mock.patch.object(bundled_tools.shutil, "which", return_value=None):
            path, error = bundled_tools.require_tool(UBERWOLF)
        self.assertIsNone(path)
        self.assertIn("tools", error or "")
        self.assertIn(UBERWOLF.env_name, error or "")

    def test_legacy_find_executable_still_resolves(self) -> None:
        bundled = self._make_tool(self.base / "src" / "tools")
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=False), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base / "src"):
            found = find_executable(None, "UBERWOLF_CLI", ["UberWolfCli.exe"])
        self.assertEqual(found, bundled.resolve())


class HashPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.tool = self.base / "tools" / "UberWolfCli.exe"
        self.tool.parent.mkdir(parents=True)
        self.tool.write_bytes(FAKE_EXE)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _locate(self):
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=False), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base):
            return bundled_tools.locate_tool(UBERWOLF)

    def test_pinned_hash_mismatch_blocks_the_tool(self) -> None:
        status = self._locate()
        self.assertIsNotNone(status.path)
        self.assertFalse(status.hash_ok)  # fake bytes never match the pinned v0.6.4 hash
        self.assertFalse(status.available)
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=False), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base):
            path, error = bundled_tools.require_tool(UBERWOLF)
        self.assertIsNone(path)
        self.assertIn("SHA-256", error or "")

    def test_environment_override_accepts_another_build(self) -> None:
        os.environ[f"{UBERWOLF.env_name}_SHA256"] = bundled_tools.sha256_file(self.tool)
        status = self._locate()
        self.assertTrue(status.hash_ok)
        self.assertTrue(status.available)

    def test_unpinned_tool_is_allowed_and_marked_as_such(self) -> None:
        wolftl = self.base / "tools" / "WolfTL.exe"
        wolftl.write_bytes(b"fake WolfTL")
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=False), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base):
            status = bundled_tools.locate_tool(WOLFTL)
            path, error = bundled_tools.require_tool(WOLFTL)
        self.assertIsNone(status.hash_ok)
        self.assertIsNone(error)
        self.assertEqual(path, wolftl.resolve())

    def test_the_pinned_uberwolf_hash_is_the_tested_release(self) -> None:
        self.assertEqual(
            bundled_tools.EXPECTED_SHA256["uberwolfcli.exe"],
            "0c9645733ae9544df11ee0c859a7f2cb51aa547d5d13f7935cb480bdab96fb3a",
        )


class ConsoleWindowTests(unittest.TestCase):
    def test_no_window_flags_only_on_windows(self) -> None:
        with mock.patch.object(bundled_tools.os, "name", "posix"):
            self.assertEqual(bundled_tools.no_window_kwargs(), {})

    @unittest.skipUnless(os.name == "nt", "Windows-only flags")
    def test_no_window_flags_on_windows(self) -> None:
        kwargs = bundled_tools.no_window_kwargs()
        self.assertIn("creationflags", kwargs)


class LicenseTests(unittest.TestCase):
    def test_licenses_ship_with_the_project(self) -> None:
        for spec in bundled_tools.ALL_TOOLS:
            with self.subTest(tool=spec.key):
                path = bundled_tools.find_license(spec)
                self.assertIsNotNone(path, f"{spec.license_file} is missing")
                text = path.read_text(encoding="utf-8")
                self.assertIn("MIT License", text)
                self.assertIn("Sinflower", text)


class PartialSuccessTests(unittest.TestCase):
    def test_text_failures_are_separate_from_extraction_failures(self) -> None:
        result = ExtractionResult(
            engine="wolf-rpg",
            output=Path("."),
            manifest={"archives_processed": 12, "errors": []},
            text_errors=["WolfTL was not found."],
        )
        self.assertEqual(result.errors, [])
        self.assertTrue(result.text_errors)


if __name__ == "__main__":
    unittest.main()


class WolfEndToEndTests(unittest.TestCase):
    """A WOLF run where the unpacker works but the text backend is absent."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.game = self.base / "WolfGame"
        data = self.game / "Data"
        (data / "BasicData").mkdir(parents=True)
        (data / "BasicData" / "Game.dat").write_bytes(b"wolf data")
        (data / "BasicData.wolf").write_bytes(b"archive-1")
        (data / "MapData.wolf").write_bytes(b"archive-2")
        (self.game / "Game.exe").write_bytes(b"MZ wolf player")

        # Stand-in for UberWolfCli: writes the "unpacked" files into the
        # workspace it is handed, which is exactly what the real one does.
        self.shim = self.base / "UberWolfCli"
        self.shim.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "from pathlib import Path\n"
            # UberWolfCli is handed the game executable, not the folder.
            "root = Path(sys.argv[1])\n"
            "root = root.parent if root.is_file() else root\n"
            "target = root / 'Data' / 'BasicData'\n"
            "target.mkdir(parents=True, exist_ok=True)\n"
            "(target / 'hero.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\n\\x00\\x00\\x00\\rIHDRunpacked')\n"
            "print('Decryption Key: [redacted]')\n",
            encoding="utf-8",
        )
        self.shim.chmod(0o755)
        self._env = dict(os.environ)
        os.environ.pop(WOLFTL.env_name, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "the shim is a POSIX script")
    def test_resources_succeed_while_text_reports_the_missing_backend(self) -> None:
        from rpg_maker_tool import ENGINE_WOLF_RPG, ExtractionOptions, run_unified_extraction

        out = self.base / "out"
        with mock.patch.object(bundled_tools, "meipass_dir", return_value=None), \
             mock.patch.object(bundled_tools, "is_frozen", return_value=False), \
             mock.patch.object(bundled_tools, "source_dir", return_value=self.base / "no-tools"), \
             mock.patch.object(bundled_tools.shutil, "which", return_value=None):
            result = run_unified_extraction(
                ExtractionOptions(
                    source=self.game,
                    output=out,
                    engine=ENGINE_WOLF_RPG,
                    images=True,
                    text=True,
                    resources=True,
                    uberwolf_cli=self.shim,
                )
            )

        self.assertEqual(result.errors, [])
        self.assertTrue(result.manifest["resources_ok"])
        self.assertEqual(result.manifest["archives_processed"], 2)
        self.assertGreaterEqual(result.manifest["images_extracted"], 1)
        self.assertTrue(result.text_errors)
        self.assertIn("WolfTL", result.text_errors[0])
        self.assertEqual(result.manifest["errors"], [])
        self.assertEqual(result.manifest["text_errors"], result.text_errors)
        # The backend log must not leak a key even in the partial-success case.
        log = (out / "logs" / "uberwolf.log").read_text(encoding="utf-8")
        self.assertNotIn("Decryption Key: [", log.replace("[redacted]", ""))
