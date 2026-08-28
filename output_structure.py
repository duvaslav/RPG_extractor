#!/usr/bin/env python3
"""Output layout helpers shared by the RPG Maker/WOLF and Unity extractors.

Every extractor can write its results in one of two layouts:

``preserve``
    Keep the folder tree the game uses, so ``img/pictures/Actor1.png`` stays
    ``images/img/pictures/Actor1.png``. This is the default and is what you want
    when the extracted files have to stay recognizable or be put back.

``flatten``
    Drop every file straight into one folder per asset kind, so the same file
    becomes ``images/Actor1.png``. This is what you want when you only care
    about the pictures themselves.

Flattening makes name collisions possible: two folders can both hold
``Actor1.png``. :class:`FlatNameAllocator` resolves them predictably by first
qualifying the name with its parent folders and only then falling back to a
numeric suffix. The mapping is remembered, so asking for the same relative path
twice always returns the same flat name.

Standard library only, so the CLI keeps working without extra dependencies.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Iterator

STRUCTURE_PRESERVE = "preserve"
STRUCTURE_FLATTEN = "flatten"
STRUCTURE_MODES = (STRUCTURE_PRESERVE, STRUCTURE_FLATTEN)

_INVALID_NAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_FLAT_NAME_LENGTH = 180


def structure_mode(preserve_structure: bool) -> str:
    return STRUCTURE_PRESERVE if preserve_structure else STRUCTURE_FLATTEN


def preserve_from_mode(mode: str) -> bool:
    if mode not in STRUCTURE_MODES:
        raise ValueError(f"unknown folder structure mode: {mode}")
    return mode == STRUCTURE_PRESERVE


def sanitize_name_part(value: str, fallback: str = "file") -> str:
    cleaned = _INVALID_NAME_CHARS_RE.sub("_", value).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or fallback


class FlatNameAllocator:
    """Maps nested relative paths onto unique names inside a single folder."""

    def __init__(self) -> None:
        self._assigned: dict[str, str] = {}
        self._used: set[str] = set()
        self._lock = threading.Lock()
        self.collisions = 0

    def allocate(self, relative: Path | str) -> Path:
        relative_path = Path(relative)
        key = relative_path.as_posix().lower()
        with self._lock:
            assigned = self._assigned.get(key)
            if assigned is not None:
                return Path(assigned)
            for index, candidate in enumerate(self._candidate_names(relative_path)):
                if candidate.lower() in self._used:
                    continue
                if index:
                    self.collisions += 1
                self._used.add(candidate.lower())
                self._assigned[key] = candidate
                return Path(candidate)
        raise RuntimeError(f"could not allocate a flat name for {relative_path}")

    @staticmethod
    def _candidate_names(relative: Path) -> Iterator[str]:
        suffix = relative.suffix
        stem = sanitize_name_part(relative.stem or relative.name, "file")
        parents = [
            sanitize_name_part(part, "")
            for part in relative.parent.parts
            if part not in {"", ".", "/", "\\"}
        ]
        parents = [part for part in parents if part]

        yield _trim_name(stem, suffix)
        for depth in range(1, len(parents) + 1):
            prefix = "_".join(parents[-depth:])
            yield _trim_name(f"{prefix}_{stem}", suffix)
        counter = 2
        while True:
            yield _trim_name(f"{stem}_{counter}", suffix)
            counter += 1


def _trim_name(stem: str, suffix: str) -> str:
    budget = max(_MAX_FLAT_NAME_LENGTH - len(suffix), 1)
    return f"{stem[:budget]}{suffix}"


def plan_output_relative(
    relative: Path | str,
    preserve_structure: bool,
    allocator: FlatNameAllocator | None = None,
) -> Path:
    """Return the relative output path for one extracted file.

    With ``preserve_structure`` the path is returned unchanged. Without it the
    file is placed directly in the output folder, using ``allocator`` to keep
    names unique; passing no allocator falls back to the bare file name.
    """

    relative_path = Path(relative)
    if preserve_structure:
        return relative_path
    if allocator is None:
        return Path(relative_path.name)
    return allocator.allocate(relative_path)


def describe_structure(preserve_structure: bool, example: str = "img/pictures/Actor1.png") -> str:
    """One-line human-readable example of what the chosen layout produces."""

    relative = Path(example)
    if preserve_structure:
        return relative.as_posix()
    return relative.name
