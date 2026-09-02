#!/usr/bin/env python3
"""Finding and vetting the console backends the WOLF RPG modes shell out to.

WOLF RPG archives are unpacked by ``UberWolfCli`` and its text is exported by
``WolfTL`` — both third-party MIT-licensed programs. They are meant to ship
inside the built EXE, which is why the search order starts with the places
PyInstaller puts bundled files:

1. ``sys._MEIPASS/tools`` — one-file build, unpacked to a temp folder at start;
2. ``tools`` next to the executable — one-dir / portable build;
3. ``tools`` next to this source file — running from a checkout;
4. the tool's environment variable (``UBERWOLF_CLI`` / ``WOLFTL_CLI``);
5. ``PATH``.

Every bundled binary is pinned by SHA-256: a tool that does not match the hash
this build was tested against is refused rather than executed, so a swapped or
truncated file cannot be run silently.

Standard library only.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TOOLS_DIRNAME = "tools"
LICENSES_DIRNAME = "licenses"

# Hashes of the exact builds this project was tested with. Override at runtime
# with <ENV>_SHA256 (for example UBERWOLF_CLI_SHA256) when using another build.
EXPECTED_SHA256: dict[str, str] = {
    # UberWolf v0.6.4 — verified against a real WOLF RPG game (12 archives).
    "uberwolfcli.exe": "0c9645733ae9544df11ee0c859a7f2cb51aa547d5d13f7935cb480bdab96fb3a",
    # WolfTL: fill in once a build has been tested; until then any file is
    # accepted and reported as unpinned.
}


@dataclass(frozen=True)
class ToolSpec:
    """One external backend: what it is called and how it is configured."""

    key: str
    names: tuple[str, ...]
    env_name: str
    purpose: str
    license_file: str


UBERWOLF = ToolSpec(
    key="uberwolf",
    names=("UberWolfCli.exe", "UberWolfCli"),
    env_name="UBERWOLF_CLI",
    purpose="распаковка архивов WOLF RPG",
    license_file="UberWolf-LICENSE.txt",
)

WOLFTL = ToolSpec(
    key="wolftl",
    names=("WolfTL.exe", "WolfTL"),
    env_name="WOLFTL_CLI",
    purpose="извлечение и импорт текста WOLF RPG",
    license_file="WolfTL-LICENSE.txt",
)

ALL_TOOLS = (UBERWOLF, WOLFTL)


@dataclass
class ToolStatus:
    """Where a backend was found and whether it is the build we pinned."""

    spec: ToolSpec
    path: Path | None = None
    source: str = "not found"
    sha256: str | None = None
    expected_sha256: str | None = None
    hash_ok: bool | None = None  # None when nothing is pinned for this tool

    @property
    def available(self) -> bool:
        return self.path is not None and self.hash_ok is not False

    def as_dict(self) -> dict[str, object]:
        return {
            "tool": self.spec.key,
            "path": str(self.path) if self.path else None,
            "source": self.source,
            "sha256": self.sha256,
            "pinned": self.expected_sha256 is not None,
            "hash_ok": self.hash_ok,
            "available": self.available,
        }


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def meipass_dir() -> Path | None:
    """Temp folder a PyInstaller one-file build unpacks itself into."""

    value = getattr(sys, "_MEIPASS", None)
    return Path(value) if value else None


def executable_dir() -> Path:
    return Path(sys.executable).resolve().parent


def source_dir() -> Path:
    return Path(__file__).resolve().parent


def tool_search_dirs() -> list[tuple[Path, str]]:
    """Folders to look in, most specific first, each with a label for the log."""

    candidates: list[tuple[Path, str]] = []
    bundled = meipass_dir()
    if bundled is not None:
        candidates.append((bundled / TOOLS_DIRNAME, "bundled (one-file)"))
    if is_frozen():
        candidates.append((executable_dir() / TOOLS_DIRNAME, "next to the executable"))
    candidates.append((source_dir() / TOOLS_DIRNAME, "project tools/"))
    seen: set[Path] = set()
    unique: list[tuple[Path, str]] = []
    for path, label in candidates:
        if path not in seen:
            seen.add(path)
            unique.append((path, label))
    return unique


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_hash_for(spec: ToolSpec, path: Path) -> str | None:
    """Pinned hash for this tool: the environment wins over the built-in one."""

    override = os.environ.get(f"{spec.env_name}_SHA256")
    if override:
        return override.strip().lower()
    return EXPECTED_SHA256.get(path.name.lower())


def locate_tool(spec: ToolSpec, explicit: Path | None = None) -> ToolStatus:
    status = ToolStatus(spec=spec)

    found: Path | None = None
    source = "not found"
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            found, source = candidate, "--explicit path"
    if found is None:
        for directory, label in tool_search_dirs():
            for name in spec.names:
                candidate = directory / name
                if candidate.is_file():
                    found, source = candidate.resolve(), label
                    break
            if found is not None:
                break
    if found is None:
        env_value = os.environ.get(spec.env_name)
        if env_value:
            candidate = Path(env_value).expanduser().resolve()
            if candidate.is_file():
                found, source = candidate, f"${spec.env_name}"
    if found is None:
        for name in spec.names:
            which = shutil.which(name)
            if which:
                found, source = Path(which).resolve(), "PATH"
                break

    if found is None:
        return status

    status.path = found
    status.source = source
    status.expected_sha256 = expected_hash_for(spec, found)
    if status.expected_sha256:
        try:
            status.sha256 = sha256_file(found)
        except OSError:
            status.sha256 = None
        status.hash_ok = (
            status.sha256 is not None and status.sha256.lower() == status.expected_sha256
        )
    return status


def require_tool(spec: ToolSpec, explicit: Path | None = None) -> tuple[Path | None, str | None]:
    """Resolve a backend for execution. Returns (path, error message)."""

    status = locate_tool(spec, explicit)
    if status.path is None:
        return None, missing_tool_message(spec)
    if status.hash_ok is False:
        return None, (
            f"{status.path.name}: SHA-256 не совпадает с проверенной версией "
            f"(ожидается {status.expected_sha256}, получено {status.sha256}). "
            f"Замените файл или задайте {spec.env_name}_SHA256 для своей сборки."
        )
    return status.path, None


def missing_tool_message(spec: ToolSpec) -> str:
    names = " / ".join(spec.names)
    return (
        f"{names} не найден ({spec.purpose}). Положите его в папку {TOOLS_DIRNAME} рядом с "
        f"программой или укажите путь в переменной окружения {spec.env_name}."
    )


def tool_report(explicit: dict[str, Path | None] | None = None) -> list[ToolStatus]:
    explicit = explicit or {}
    return [locate_tool(spec, explicit.get(spec.key)) for spec in ALL_TOOLS]


def license_dirs() -> list[Path]:
    dirs: list[Path] = []
    bundled = meipass_dir()
    if bundled is not None:
        dirs.append(bundled / LICENSES_DIRNAME)
    if is_frozen():
        dirs.append(executable_dir() / LICENSES_DIRNAME)
    dirs.append(source_dir() / LICENSES_DIRNAME)
    return dirs


def find_license(spec: ToolSpec) -> Path | None:
    for directory in license_dirs():
        candidate = directory / spec.license_file
        if candidate.is_file():
            return candidate
    return None


def no_window_kwargs() -> dict[str, object]:
    """subprocess kwargs that keep a console backend from flashing a window."""

    if os.name != "nt":
        return {}
    kwargs: dict[str, object] = {}
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    kwargs["creationflags"] = creation_flags
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
    kwargs["startupinfo"] = startupinfo
    return kwargs
