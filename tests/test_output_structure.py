#!/usr/bin/env python3
"""Tests for the "keep folder structure" option.

Run with:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from output_structure import (  # noqa: E402
    STRUCTURE_FLATTEN,
    STRUCTURE_PRESERVE,
    FlatNameAllocator,
    describe_structure,
    plan_output_relative,
    preserve_from_mode,
    structure_mode,
)
from rpg_maker_tool import (  # noqa: E402
    MV_HEADER,
    collect_asset_jobs,
    decrypt_asset_job,
    encrypt_for_self_test,
    extract_images_from_tree,
)

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"body-a"
PNG_B = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"body-b"
KEY = bytes.fromhex("00112233445566778899aabbccddeeff")


class FlatNameAllocatorTests(unittest.TestCase):
    def test_unique_names_are_kept_as_is(self) -> None:
        allocator = FlatNameAllocator()
        self.assertEqual(allocator.allocate(Path("img/pictures/Actor1.png")).as_posix(), "Actor1.png")
        self.assertEqual(allocator.allocate(Path("audio/bgm/Theme.ogg")).as_posix(), "Theme.ogg")
        self.assertEqual(allocator.collisions, 0)

    def test_collision_is_qualified_with_the_parent_folder(self) -> None:
        allocator = FlatNameAllocator()
        first = allocator.allocate(Path("img/pictures/Actor1.png"))
        second = allocator.allocate(Path("img/characters/Actor1.png"))
        self.assertEqual(first.as_posix(), "Actor1.png")
        self.assertEqual(second.as_posix(), "characters_Actor1.png")
        self.assertEqual(allocator.collisions, 1)

    def test_second_collision_walks_further_up_then_counts(self) -> None:
        allocator = FlatNameAllocator()
        names = [
            allocator.allocate(Path("a/pictures/Actor1.png")).as_posix(),
            allocator.allocate(Path("b/pictures/Actor1.png")).as_posix(),
            allocator.allocate(Path("c/pictures/Actor1.png")).as_posix(),
        ]
        self.assertEqual(names[0], "Actor1.png")
        self.assertEqual(names[1], "pictures_Actor1.png")
        self.assertEqual(names[2], "c_pictures_Actor1.png")
        self.assertEqual(len(set(names)), 3)

    def test_same_path_always_maps_to_the_same_name(self) -> None:
        allocator = FlatNameAllocator()
        first = allocator.allocate("img/pictures/Actor1.png")
        allocator.allocate("img/characters/Actor1.png")
        again = allocator.allocate("img/pictures/Actor1.png")
        self.assertEqual(first, again)

    def test_case_insensitive_collisions_are_resolved(self) -> None:
        allocator = FlatNameAllocator()
        first = allocator.allocate(Path("img/a/Actor1.png"))
        second = allocator.allocate(Path("img/b/actor1.png"))
        self.assertNotEqual(first.as_posix().lower(), second.as_posix().lower())

    def test_long_names_stay_within_the_filesystem_limit(self) -> None:
        allocator = FlatNameAllocator()
        allocated = allocator.allocate(Path("img/" + "x" * 400 + ".png"))
        self.assertLessEqual(len(allocated.name), 180)
        self.assertTrue(allocated.name.endswith(".png"))

    def test_plan_output_relative_modes(self) -> None:
        relative = Path("img/pictures/Actor1.png")
        self.assertEqual(plan_output_relative(relative, True), relative)
        self.assertEqual(plan_output_relative(relative, False), Path("Actor1.png"))
        self.assertEqual(
            plan_output_relative(relative, False, FlatNameAllocator()), Path("Actor1.png")
        )

    def test_mode_helpers(self) -> None:
        self.assertEqual(structure_mode(True), STRUCTURE_PRESERVE)
        self.assertEqual(structure_mode(False), STRUCTURE_FLATTEN)
        self.assertTrue(preserve_from_mode(STRUCTURE_PRESERVE))
        self.assertFalse(preserve_from_mode(STRUCTURE_FLATTEN))
        with self.assertRaises(ValueError):
            preserve_from_mode("sideways")
        self.assertEqual(describe_structure(True, "img/pictures/A.png"), "img/pictures/A.png")
        self.assertEqual(describe_structure(False, "img/pictures/A.png"), "A.png")


class CollectAssetJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Game"
        (self.root / "www" / "img" / "pictures").mkdir(parents=True)
        (self.root / "www" / "img" / "characters").mkdir(parents=True)
        (self.root / "www" / "audio" / "bgm").mkdir(parents=True)
        (self.root / "www" / "img" / "pictures" / "Actor1.rpgmvp").write_bytes(
            encrypt_for_self_test(PNG, KEY, MV_HEADER)
        )
        (self.root / "www" / "img" / "characters" / "Actor1.rpgmvp").write_bytes(
            encrypt_for_self_test(PNG_B, KEY, MV_HEADER)
        )
        (self.root / "www" / "audio" / "bgm" / "Theme.rpgmvo").write_bytes(
            encrypt_for_self_test(b"OggS" + b"\x00" * 12 + b"body", KEY, MV_HEADER)
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_preserved_layout_mirrors_the_game_tree(self) -> None:
        out = Path(self._tmp.name) / "out"
        jobs = collect_asset_jobs(self.root, out, {"image"}, preserve_structure=True)
        relatives = sorted(job.output.relative_to(out).as_posix() for job in jobs)
        self.assertEqual(
            relatives,
            ["www/img/characters/Actor1.png", "www/img/pictures/Actor1.png"],
        )

    def test_flat_layout_has_no_subfolders(self) -> None:
        out = Path(self._tmp.name) / "flat"
        jobs = collect_asset_jobs(self.root, out, {"image", "audio"}, preserve_structure=False)
        relatives = sorted(job.output.relative_to(out).as_posix() for job in jobs)
        self.assertEqual(relatives, ["Actor1.png", "Theme.ogg", "pictures_Actor1.png"])
        for job in jobs:
            result = decrypt_asset_job(job, KEY, True, False, False, True, False)
            self.assertEqual(result.status, "ok", result.message)
        written = sorted(path.name for path in out.rglob("*") if path.is_file())
        self.assertEqual(written, ["Actor1.png", "Theme.ogg", "pictures_Actor1.png"])
        self.assertFalse([path for path in out.iterdir() if path.is_dir()])

    def test_flat_layout_keeps_both_files_intact(self) -> None:
        out = Path(self._tmp.name) / "flat2"
        jobs = collect_asset_jobs(self.root, out, {"image"}, preserve_structure=False)
        for job in jobs:
            decrypt_asset_job(job, KEY, True, False, False, True, False)
        contents = {path.read_bytes() for path in out.iterdir()}
        self.assertEqual(contents, {PNG, PNG_B})

    def test_in_place_decrypt_ignores_the_flat_request(self) -> None:
        jobs = collect_asset_jobs(self.root, None, {"image"}, preserve_structure=False)
        for job in jobs:
            self.assertEqual(job.output.parent, job.source.parent)


class ExtractImagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.source = Path(self._tmp.name) / "extracted"
        (self.source / "Graphics" / "Pictures").mkdir(parents=True)
        (self.source / "Graphics" / "Characters").mkdir(parents=True)
        (self.source / "Graphics" / "Pictures" / "Hero.png").write_bytes(PNG)
        (self.source / "Graphics" / "Characters" / "Hero.png").write_bytes(PNG_B)
        # Same bytes as the first picture, in a third folder.
        (self.source / "Graphics" / "Copy").mkdir(parents=True)
        (self.source / "Graphics" / "Copy" / "Hero.png").write_bytes(PNG)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_preserved_layout(self) -> None:
        images = Path(self._tmp.name) / "images"
        count, index, warnings = extract_images_from_tree(self.source, images, True)
        self.assertEqual(count, 3)
        self.assertEqual(warnings, [])
        self.assertTrue((images / "Graphics" / "Pictures" / "Hero.png").is_file())
        self.assertTrue((images / "Graphics" / "Characters" / "Hero.png").is_file())
        self.assertEqual(len(index), 3)
        written_index = json.loads((images / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(written_index), 3)

    def test_flat_layout_renames_collisions_and_drops_duplicates(self) -> None:
        images = Path(self._tmp.name) / "flat-images"
        count, _index, warnings = extract_images_from_tree(self.source, images, False)
        names = sorted(path.name for path in images.iterdir() if path.suffix == ".png")
        self.assertEqual(count, 2)
        self.assertEqual(names, ["Copy_Hero.png", "Hero.png"])
        self.assertTrue(any("duplicate" in warning for warning in warnings))
        self.assertFalse([path for path in images.iterdir() if path.is_dir()])


if __name__ == "__main__":
    unittest.main()
