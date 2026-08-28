#!/usr/bin/env python3
"""
RPG Maker MV/MZ project helper.

Features:
- find RPG Maker encryption keys in game files and plugins;
- decrypt encrypted MV/MZ assets in bulk;
- export/decode translatable strings from data files;
- decode one-off escaped/encoded strings.

The CLI implementation intentionally uses only Python's standard library.
The optional GUI is in rpg_maker_gui.py and uses PyQt6.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol
from urllib.parse import unquote

from output_structure import (
    STRUCTURE_FLATTEN,
    STRUCTURE_PRESERVE,
    FlatNameAllocator,
    plan_output_relative,
    structure_mode,
)


HEADER_LENGTH = 16
MV_HEADER = bytes.fromhex("5250474d560000000003010000000000")
MZ_HEADER = bytes.fromhex("5250474d5a0000000003010000000000")
KNOWN_HEADERS = (MV_HEADER, MZ_HEADER)

ASSET_EXTENSIONS: dict[str, tuple[str, str]] = {
    ".rpgmvp": (".png", "image"),
    ".png_": (".png", "image"),
    ".jpg_": (".jpg", "image"),
    ".jpeg_": (".jpeg", "image"),
    ".webp_": (".webp", "image"),
    ".rpgmvo": (".ogg", "audio"),
    ".ogg_": (".ogg", "audio"),
    ".rpgmvm": (".m4a", "audio"),
    ".m4a_": (".m4a", "audio"),
    ".mp3_": (".mp3", "audio"),
    ".wav_": (".wav", "audio"),
    ".webm_": (".webm", "video"),
    ".mp4_": (".mp4", "video"),
}

KNOWN_PLAINTEXT_PREFIXES: dict[str, bytes] = {
    ".png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
}

ENGINE_AUTO = "auto"
ENGINE_RPGMAKER_MV = "rpgmaker-mv"
ENGINE_RPGMAKER_MZ = "rpgmaker-mz"
ENGINE_RPGMAKER_VX_ACE = "rpgmaker-vx-ace"
ENGINE_WOLF_RPG = "wolf-rpg"
def _default_output_parent() -> Path:
    """Where extractions land unless the user picks something else.

    Windows keeps the historical D:\\0RPG folder when that drive exists; every
    other case falls back to a folder in the user's home directory, so the
    default path is always writable.
    """

    if os.name == "nt":
        windows_default = Path(r"D:\0RPG")
        if windows_default.drive and Path(windows_default.drive + "\\").exists():
            return windows_default
    return Path.home() / "RPG-Extracted"


DEFAULT_OUTPUT_PARENT = _default_output_parent()
SUPPORTED_ENGINES = {
    ENGINE_AUTO,
    ENGINE_RPGMAKER_MV,
    ENGINE_RPGMAKER_MZ,
    ENGINE_RPGMAKER_VX_ACE,
    ENGINE_WOLF_RPG,
}
RGSS_ARCHIVE_EXTENSIONS = {".rgssad", ".rgss2a", ".rgss3a"}
WOLF_ARCHIVE_EXTENSIONS = {
    ".wolf",
    ".data",
    ".pak",
    ".bin",
    ".assets",
    ".content",
    ".res",
    ".resource",
}
WOLF_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}

GAME_ROOT_MARKERS: tuple[tuple[str, ...], ...] = (
    ("Game.rpgproject",),
    ("Game.rmmzproject",),
    ("www", "data", "System.json"),
    ("data", "System.json"),
    ("www", "js", "rpg_core.js"),
    ("js", "rpg_core.js"),
    ("www", "js", "rmmz_core.js"),
    ("js", "rmmz_core.js"),
    ("Data", "System.rvdata2"),
)

# Folder names that describe a game's insides rather than the game itself, so a
# selected subfolder never becomes the output name.
GENERIC_GAME_FOLDER_NAMES = {
    "animations", "audio", "battlebacks1", "battlebacks2", "bgm", "bgs",
    "characters", "data", "effects", "enemies", "extracted", "faces", "fonts",
    "game", "icon", "img", "js", "libs", "me", "movies", "parallaxes",
    "pictures", "plugins", "save", "se", "sv_actors", "sv_enemies", "system",
    "tilesets", "titles1", "titles2", "www",
}

TEXT_EXTENSIONS = {
    ".json",
    ".js",
    ".html",
    ".txt",
    ".rpgproject",
    ".rmmzproject",
}

KEY_CONTEXT_RE = re.compile(
    r"(?is)\b(?:encryptionKey|_encryptionKey|setEncryptionInfo|decrypt(?:ion)?Key)\b"
    r".{0,160}?\b([0-9a-f]{32})\b"
)
JSON_KEY_RE = re.compile(
    r"(?is)[\"']encryptionKey[\"']\s*[:=]\s*[\"']([0-9a-f]{32})[\"']"
)
ANY_HEX_KEY_RE = re.compile(r"(?i)\b[0-9a-f]{32}\b")
SUSPICIOUS_KEY_CONTEXT_RE = re.compile(
    r"(?i)(encrypt|decrypt|decrypter|encryptionKey|setEncryptionInfo|rpgmv|rpgmz|"
    r"rpgmvp|rpgmvo|rpgmvm|png_|ogg_|m4a_|eval|showDefault|xor|decode)"
)
COLON_KEY_TOKEN_RE = re.compile(
    r"\b[A-Za-z_$][A-Za-z0-9_$]{1,24}:[A-Za-z_$][A-Za-z0-9_$]{1,24}\b"
)
INVALID_FOLDER_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
KEY_PLUGIN_FILENAME_RE = re.compile(
    r"(?i)(key|encrypt|decrypt|decrypter|mask|obfus|protect|crypto|decode)"
)
SECRET_LINE_RE = re.compile(r"(?i)(encryption|protection|decrypt(?:ion)?).*key")
HEX_SECRET_RE = re.compile(r"(?i)\b[0-9a-f]{16,}\b")

TRANSLATABLE_KEYS = {
    "battleBgm",
    "battleback1Name",
    "battleback2Name",
    "currencyUnit",
    "description",
    "displayName",
    "gameTitle",
    "help",
    "message1",
    "message2",
    "message3",
    "message4",
    "name",
    "nickname",
    "note",
    "profile",
    "text",
}

EVENT_TEXT_PARAM_INDEXES: dict[int, tuple[int, ...]] = {
    401: (0,),  # Show Text line
    405: (0,),  # Scroll Text line
    320: (1,),  # Change Name
    324: (1,),  # Change Nickname
    325: (1,),  # Change Profile
}

EVENT_CHOICE_CODES = {102}
EVENT_COMMENT_CODES = {108, 408}

LANGUAGE_RE = re.compile(r"[A-Za-zА-Яа-яЁё\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
FILE_REF_RE = re.compile(
    r"^[\w .@()+\-\[\]/\\]+"
    r"\.(?:png|jpg|jpeg|webp|ogg|m4a|mp3|wav|webm|mp4|json|js)$",
    re.IGNORECASE,
)
PERCENT_ESCAPE_RE = re.compile(r"%(?:[0-9a-fA-F]{2})")
BASE64_RE = re.compile(r"^[A-Za-z0-9+/_=-]+$")
JS_STRING_RE = re.compile(
    r"(?P<quote>['\"`])(?P<body>(?:\\.|(?! (?P=quote)).)*)(?P=quote)",
    re.DOTALL | re.VERBOSE,
)


@dataclass
class KeyCandidate:
    key: str
    score: int
    key_format: str = "hex"
    sources: list[str] = field(default_factory=list)
    reasons: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class AssetJob:
    source: Path
    output: Path
    kind: str
    target_ext: str


@dataclass(frozen=True)
class AssetResult:
    source: Path
    output: Path
    kind: str
    status: str
    message: str = ""
    encrypted: bool = False


@dataclass(frozen=True)
class TextRecord:
    file: str
    path: str
    kind: str
    text: str
    decoded: str
    decoders: tuple[str, ...]


@dataclass
class DetectionResult:
    engine: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    edition: str | None = None
    root: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "confidence": round(self.confidence, 4),
            "edition": self.edition,
            "root": str(self.root) if self.root else None,
            "evidence": self.evidence,
            "warnings": self.warnings,
        }


@dataclass
class ValidationResult:
    ok: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ExtractionOptions:
    source: Path
    output: Path
    engine: str = ENGINE_AUTO
    images: bool = False
    text: bool = False
    resources: bool = True
    # Exact asset kinds to extract ("image", "audio", "video"). Empty means
    # "decide from the images/resources flags", which is what the CLI does.
    asset_kinds: set[str] = field(default_factory=set)
    key: str = "auto"
    include_comments: bool = False
    show_keys: bool = False
    overwrite: bool = False
    strict: bool = True
    workers: int = 0
    preserve_structure: bool = True
    uberwolf_cli: Path | None = None
    wolftl_cli: Path | None = None


@dataclass
class ExtractionResult:
    engine: str
    output: Path
    manifest: dict[str, Any]
    images: int = 0
    text_entries: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class TextExtractionResult:
    count: int
    output: Path | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class PatchResult:
    ok: bool
    output: Path | None = None
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class EngineAdapter(Protocol):
    engine_id: str

    def probe(self, path: Path) -> DetectionResult:
        ...

    def validate(self, path: Path) -> ValidationResult:
        ...

    def extract_resources(self, options: ExtractionOptions) -> ExtractionResult:
        ...

    def extract_text(self, options: ExtractionOptions) -> TextExtractionResult:
        ...

    def apply_translation(self, options: Any) -> PatchResult:
        ...


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def normalize_path(path: Path) -> Path:
    return path.expanduser().resolve()


def relpath(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def resolve_game_root(path: Path) -> Path:
    path = normalize_path(path)
    if path.is_file():
        if path.name.lower() in {"game.exe", "gamepro.exe"}:
            return path.parent
        if path.suffix.lower() in RGSS_ARCHIVE_EXTENSIONS:
            return path.parent
        if path.suffix.lower() in WOLF_ARCHIVE_EXTENSIONS:
            if path.parent.name.lower() == "data":
                return path.parent.parent
            return path.parent
        return path.parent
    if path.name.lower() == "data" and path.parent.exists():
        return path.parent
    return path


def ensure_within_directory(base: Path, candidate: Path) -> Path:
    base = normalize_path(base)
    candidate = normalize_path(candidate)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"unsafe output path escapes target directory: {candidate}") from exc
    return candidate


def safe_output_path(base: Path, relative: Path | str) -> Path:
    if isinstance(relative, str):
        relative_path = Path(relative)
    else:
        relative_path = relative
    if relative_path.is_absolute():
        raise ValueError(f"absolute extracted path is not allowed: {relative_path}")
    parts = [part for part in relative_path.parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError(f"path traversal is not allowed: {relative_path}")
    return ensure_within_directory(base, base.joinpath(*parts))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def sanitize_sensitive_log(text: str, show_keys: bool = False) -> str:
    if show_keys:
        return text
    sanitized_lines: list[str] = []
    for line in text.splitlines():
        if SECRET_LINE_RE.search(line):
            sanitized_lines.append(SECRET_LINE_RE.sub(lambda m: m.group(0), line))
            sanitized_lines[-1] = HEX_SECRET_RE.sub("[redacted]", sanitized_lines[-1])
        else:
            sanitized_lines.append(HEX_SECRET_RE.sub("[redacted]", line))
    return "\n".join(sanitized_lines)


def image_extension_from_signature(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def normalize_image_output_relative(relative: Path, actual_ext: str) -> Path:
    if relative.suffix.lower() != actual_ext:
        return relative.with_suffix(actual_ext)
    return relative


def is_probable_key_file(path: Path, root: Path, deep: bool = False) -> bool:
    suffix = path.suffix.lower()
    if suffix not in TEXT_EXTENSIONS:
        return False
    if deep:
        return True

    rel = relpath(path, root).lower().replace("\\", "/")
    name = path.name.lower()

    if suffix == ".js":
        return (
            name
            in {
                "plugins.js",
                "rpg_core.js",
                "rpg_managers.js",
                "rmmz_core.js",
                "rmmz_managers.js",
                "main.js",
            }
            or KEY_PLUGIN_FILENAME_RE.search(name) is not None
        )
    if suffix == ".json":
        return (
            name in {"system.json", "plugins.json", "package.json"}
            or "/js/" in f"/{rel}"
            or "plugin" in rel
            or rel.endswith("/data/system.json")
            or rel == "data/system.json"
        )
    if suffix == ".html":
        return name == "index.html"
    if suffix in {".rpgproject", ".rmmzproject"}:
        return True
    if suffix == ".txt":
        return any(token in rel for token in ("key", "encrypt", "decrypt", "plugin"))
    return False


def iter_key_search_files(root: Path, deep: bool = False) -> Iterator[Path]:
    root = normalize_path(root)
    for path in iter_files(root):
        if is_probable_key_file(path, root, deep=deep):
            yield path


def iter_js_string_literal_spans(text: str) -> Iterator[tuple[str, int, int]]:
    index = 0
    length = len(text)
    while index < length:
        quote = text[index]
        if quote not in {"'", '"', "`"}:
            index += 1
            continue

        start = index
        index += 1
        body: list[str] = []
        while index < length:
            char = text[index]
            if char == "\\":
                if index + 1 < length:
                    body.append(text[index : index + 2])
                    index += 2
                else:
                    body.append(char)
                    index += 1
                continue
            if char == quote:
                index += 1
                yield "".join(body), start, index
                break
            body.append(char)
            index += 1
        else:
            return


def validate_hex_key(raw: str) -> str:
    key = raw.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise ValueError("encryption key must be exactly 32 hex characters")
    return key


def normalize_key_value(raw: str, allow_raw: bool = False) -> tuple[str, str] | None:
    key = raw.strip()
    if not key:
        return None

    lower = key.lower()
    if lower.startswith("hex:"):
        key = key[4:].strip()
        lower = key.lower()
    elif lower.startswith("raw:"):
        key = key[4:]
        allow_raw = True
        lower = key.lower()

    if re.fullmatch(r"[0-9a-fA-F]{32}", key):
        return ("hex", lower)

    if allow_raw:
        try:
            raw_bytes = key.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if len(raw_bytes) == HEADER_LENGTH and all(char.isprintable() for char in key):
            return ("raw", key)

    return None


def key_to_bytes(key: str) -> bytes:
    normalized = normalize_key_value(key, allow_raw=True)
    if normalized is None:
        raise ValueError(
            "key must be 32 hex characters or a 16-byte raw key such as raw:showDefault:eval"
        )
    key_format, value = normalized
    if key_format == "hex":
        return bytes.fromhex(validate_hex_key(value))
    return value.encode("utf-8")


def key_candidate_to_bytes(candidate: KeyCandidate) -> bytes:
    if candidate.key_format == "hex":
        return bytes.fromhex(validate_hex_key(candidate.key))
    if candidate.key_format == "raw":
        return candidate.key.encode("utf-8")
    raise ValueError(f"unsupported key format: {candidate.key_format}")


def candidate_identity(key_format: str, key: str) -> str:
    return f"{key_format}:{key}"


def read_text_lossy(path: Path, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        raw = path.read_bytes()
    except OSError:
        return None

    if b"\x00" in raw[:4096]:
        return None

    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_prefix(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def sanitize_folder_name(name: str, fallback: str = "decoded") -> str:
    cleaned = INVALID_FOLDER_CHARS_RE.sub("_", name).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or fallback


def find_game_title(root: Path, max_text_mb: int = 5) -> str | None:
    root = normalize_path(root)
    candidates: list[Path] = []
    if root.is_file():
        root = root.parent
    candidates.extend(
        [
            root / "www" / "data" / "System.json",
            root / "data" / "System.json",
        ]
    )
    for path in root.rglob("System.json"):
        if path not in candidates:
            candidates.append(path)

    max_bytes = max_text_mb * 1024 * 1024
    for path in candidates:
        text = read_text_lossy(path, max_bytes=max_bytes)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        title = data.get("gameTitle") if isinstance(data, dict) else None
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def find_game_root_upwards(path: Path, max_levels: int = 8) -> Path | None:
    """Walk up from ``path`` until a folder looks like the game's own root.

    Users often point the tool at a subfolder — ``…/Marie's Adventure/www/img``
    when they only want the pictures. The game title, the encryption key and the
    project layout all live further up, so the root has to be found from there.
    Returns ``None`` when nothing above the path looks like a game.
    """

    current = normalize_path(path)
    if current.is_file():
        current = current.parent
    for _ in range(max_levels + 1):
        # www itself matches the data/ and js/ markers, but the game — and its
        # title, key and project file — is the folder holding www.
        if current.name.lower() != "www" and any(
            current.joinpath(*marker).exists() for marker in GAME_ROOT_MARKERS
        ):
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def meaningful_folder_name(path: Path, max_levels: int = 6) -> str:
    """First folder name above ``path`` that names something, not a game subfolder.

    ``…/Marie's Adventure/www/img`` is named after the game, not after ``img``.
    """

    current = normalize_path(path)
    if current.is_file():
        current = current.parent
    fallback = current.name
    for _ in range(max_levels + 1):
        if current.name and current.name.lower() not in GENERIC_GAME_FOLDER_NAMES:
            return current.name
        if current.parent == current:
            break
        current = current.parent
    return fallback


def default_output_folder_name(input_path: Path, suffix: str = "") -> str:
    input_path = normalize_path(input_path)
    game_root = find_game_root_upwards(input_path)
    title = find_game_title(game_root or input_path)
    if title:
        base_name = title
    elif input_path.is_file():
        base_name = input_path.stem
    elif game_root is not None and game_root != input_path:
        base_name = game_root.name
    else:
        base_name = meaningful_folder_name(input_path)
    return sanitize_folder_name(base_name) + suffix


def has_file(root: Path, *parts: str) -> bool:
    return root.joinpath(*parts).is_file()


def has_dir(root: Path, *parts: str) -> bool:
    return root.joinpath(*parts).is_dir()


def count_files_by_suffix(root: Path, suffixes: set[str], limit: int = 50) -> int:
    count = 0
    if root.is_file():
        return 1 if root.suffix.lower() in suffixes else 0
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in suffixes:
            count += 1
            if count >= limit:
                return count
    return count


def discover_rgss_archives(root: Path) -> list[Path]:
    root = normalize_path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in RGSS_ARCHIVE_EXTENSIONS else []
    archives: list[Path] = []
    for name in ("Game.rgss3a", "Game.rgss2a", "Game.rgssad"):
        path = root / name
        if path.is_file():
            archives.append(path)
    for path in root.glob("*.rgss*a"):
        if path.is_file() and path not in archives and path.suffix.lower() in RGSS_ARCHIVE_EXTENSIONS:
            archives.append(path)
    return archives


def probe_rpgmaker_vx_ace(path: Path) -> DetectionResult:
    root = resolve_game_root(path)
    evidence: list[str] = []
    score = 0.0
    edition: str | None = "vx-ace"

    archives = discover_rgss_archives(path if normalize_path(path).is_file() else root)
    archive_names = {archive.name.lower() for archive in archives}
    if "game.rgss3a" in archive_names:
        score += 0.55
        evidence.append("Game.rgss3a found")
    elif "game.rgss2a" in archive_names:
        score += 0.48
        edition = "vx"
        evidence.append("Game.rgss2a found")
    elif "game.rgssad" in archive_names:
        score += 0.45
        edition = "xp"
        evidence.append("Game.rgssad found")
    elif archives:
        score += 0.4
        evidence.append(f"RGSS archive found: {archives[0].name}")

    if has_file(root, "Game.ini"):
        score += 0.16
        evidence.append("Game.ini found")
    if has_file(root, "Game.exe"):
        score += 0.12
        evidence.append("Game.exe found")
    if has_dir(root, "Audio"):
        score += 0.08
        evidence.append("Audio folder found")
    if has_dir(root, "Graphics"):
        score += 0.08
        evidence.append("Graphics folder found")
    if has_dir(root, "Data"):
        score += 0.06
        evidence.append("Data folder found")
    if count_files_by_suffix(root, {".rvdata2"}, limit=1):
        score += 0.18
        evidence.append("VX Ace .rvdata2 files found")
    elif count_files_by_suffix(root, {".rvdata"}, limit=1):
        score += 0.15
        evidence.append("VX .rvdata files found")

    return DetectionResult(
        ENGINE_RPGMAKER_VX_ACE,
        min(score, 0.99),
        evidence,
        edition=edition if score else None,
        root=root,
    )


def probe_rpgmaker(path: Path, target_engine: str | None = None) -> DetectionResult:
    root = resolve_game_root(path)
    evidence: list[str] = []
    mv_score = 0.0
    mz_score = 0.0

    if has_file(root, "www", "js", "rpg_core.js") or has_file(root, "js", "rpg_core.js"):
        mv_score += 0.35
        evidence.append("rpg_core.js found")
    if has_file(root, "www", "js", "rmmz_core.js") or has_file(root, "js", "rmmz_core.js"):
        mz_score += 0.35
        evidence.append("rmmz_core.js found")
    if has_file(root, "Game.rpgproject"):
        mv_score += 0.25
        evidence.append("Game.rpgproject found")
    if has_file(root, "Game.rmmzproject"):
        mz_score += 0.25
        evidence.append("Game.rmmzproject found")
    if has_file(root, "www", "data", "System.json") or has_file(root, "data", "System.json"):
        mv_score += 0.12
        mz_score += 0.12
        evidence.append("System.json found")

    mv_assets = count_files_by_suffix(root, {".rpgmvp", ".rpgmvo", ".rpgmvm"}, limit=10)
    if mv_assets:
        mv_score += min(0.18, mv_assets * 0.03)
        evidence.append(f"MV encrypted asset extensions found: {mv_assets}")

    mz_assets = count_files_by_suffix(root, {".png_", ".jpg_", ".jpeg_", ".webp_", ".ogg_", ".m4a_"}, limit=10)
    if mz_assets:
        mz_score += min(0.16, mz_assets * 0.025)
        evidence.append(f"MZ-style encrypted/renamed assets found: {mz_assets}")

    mv_headers = 0
    mz_headers = 0
    for path_item in iter_files(root):
        if path_item.suffix.lower() not in ASSET_EXTENSIONS:
            continue
        header = detect_rpg_header(read_prefix(path_item, HEADER_LENGTH))
        if header and header.startswith("MV"):
            mv_headers += 1
        elif header and header.startswith("MZ"):
            mz_headers += 1
        if mv_headers + mz_headers >= 10:
            break
    if mv_headers:
        mv_score += min(0.2, mv_headers * 0.04)
        evidence.append(f"MV encrypted headers found: {mv_headers}")
    if mz_headers:
        mz_score += min(0.2, mz_headers * 0.04)
        evidence.append(f"MZ encrypted headers found: {mz_headers}")

    if target_engine == ENGINE_RPGMAKER_MV:
        return DetectionResult(ENGINE_RPGMAKER_MV, min(mv_score, 0.99), evidence, root=root)
    if target_engine == ENGINE_RPGMAKER_MZ:
        return DetectionResult(ENGINE_RPGMAKER_MZ, min(mz_score, 0.99), evidence, root=root)

    if mz_score > mv_score:
        return DetectionResult(ENGINE_RPGMAKER_MZ, min(mz_score, 0.99), evidence, root=root)
    return DetectionResult(ENGINE_RPGMAKER_MV, min(mv_score, 0.99), evidence, root=root)


def probe_wolf_rpg(path: Path) -> DetectionResult:
    root = resolve_game_root(path)
    evidence: list[str] = []
    warnings: list[str] = []
    score = 0.0
    edition = "standard"

    if path.is_file() and path.suffix.lower() in WOLF_ARCHIVE_EXTENSIONS:
        score += 0.35
        evidence.append(f"WOLF archive input found: {path.name}")

    if has_file(root, "GamePro.exe"):
        score += 0.28
        edition = "pro"
        evidence.append("GamePro.exe found")
    if has_file(root, "Game.exe"):
        score += 0.22
        evidence.append("Game.exe found")
    if has_file(root, "Data.wolf"):
        score += 0.24
        evidence.append("Data.wolf found")
    if has_dir(root, "Data"):
        score += 0.14
        evidence.append("Data folder found")

    data_root = root / "Data"
    has_game_dat = has_file(data_root, "Game.dat") or has_file(root, "Game.dat")
    has_basic_data = has_dir(data_root, "BasicData") or has_file(data_root, "BasicData.wolf")
    has_map_data = has_dir(data_root, "MapData") or has_file(data_root, "MapData.wolf")

    archive_paths = discover_wolf_archives(root)
    wolf_archive_count = sum(1 for item in archive_paths if item.suffix.lower() == ".wolf")
    generic_archive_count = len(archive_paths) - wolf_archive_count
    if wolf_archive_count:
        score += min(0.2, 0.06 + wolf_archive_count * 0.015)
        evidence.append(f"WOLF .wolf archives found: {wolf_archive_count}")
    if generic_archive_count and (has_game_dat or has_basic_data or has_map_data or has_dir(root, "Data")):
        score += min(0.12, 0.03 + generic_archive_count * 0.008)
        evidence.append(f"WOLF generic archive files found: {generic_archive_count}")

    if has_game_dat:
        score += 0.12
        evidence.append("Game.dat found")

    if has_basic_data:
        score += 0.12
        evidence.append("BasicData structures found")
    if has_map_data:
        score += 0.12
        evidence.append("MapData structures found")

    mps_count = count_files_by_suffix(root, {".mps"}, limit=30)
    dat_count = count_files_by_suffix(root, {".dat"}, limit=30)
    if mps_count:
        score += min(0.12, mps_count * 0.02)
        evidence.append(f"WOLF map files found: {mps_count}")
    if dat_count:
        score += min(0.08, dat_count * 0.01)
        evidence.append(f"WOLF dat files found: {dat_count}")

    if score >= 0.45 and not (has_file(root, "Game.exe") or has_file(root, "GamePro.exe")):
        warnings.append(
            "WOLF RPG detected, but Game.exe was not found. Automatic key detection may be unavailable."
        )

    return DetectionResult(
        ENGINE_WOLF_RPG,
        min(score, 0.99),
        evidence,
        edition=edition if score else None,
        root=root,
        warnings=warnings,
    )


def detect_engine(path: Path, override: str = ENGINE_AUTO) -> DetectionResult:
    if override not in SUPPORTED_ENGINES:
        raise ValueError(f"unsupported engine override: {override}")
    root = resolve_game_root(path)
    if not root.exists() and not normalize_path(path).exists():
        return DetectionResult(
            ENGINE_AUTO,
            0.0,
            root=root,
            warnings=[f"Path does not exist: {path}"],
        )

    if override == ENGINE_RPGMAKER_MV:
        result = probe_rpgmaker(path, ENGINE_RPGMAKER_MV)
        result.engine = ENGINE_RPGMAKER_MV
        result.confidence = max(result.confidence, 0.51 if result.evidence else 0.0)
        result.evidence.append("manual override: rpgmaker-mv")
        return result
    if override == ENGINE_RPGMAKER_MZ:
        result = probe_rpgmaker(path, ENGINE_RPGMAKER_MZ)
        result.engine = ENGINE_RPGMAKER_MZ
        result.confidence = max(result.confidence, 0.51 if result.evidence else 0.0)
        result.evidence.append("manual override: rpgmaker-mz")
        return result
    if override == ENGINE_RPGMAKER_VX_ACE:
        result = probe_rpgmaker_vx_ace(path)
        result.confidence = max(result.confidence, 0.51 if result.evidence else 0.0)
        result.evidence.append("manual override: rpgmaker-vx-ace")
        return result
    if override == ENGINE_WOLF_RPG:
        result = probe_wolf_rpg(path)
        result.confidence = max(result.confidence, 0.51 if result.evidence else 0.0)
        result.evidence.append("manual override: wolf-rpg")
        return result

    candidates = [probe_rpgmaker(path), probe_rpgmaker_vx_ace(path), probe_wolf_rpg(path)]
    candidates.sort(key=lambda item: item.confidence, reverse=True)
    best = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    if best.confidence < 0.35:
        best.warnings.append(
            "Engine confidence is too low. Use --engine rpgmaker-mv, rpgmaker-mz, rpgmaker-vx-ace, or wolf-rpg."
        )
    elif second and second.confidence >= 0.30 and best.confidence - second.confidence < 0.30:
        best.warnings.append(
            f"Conflicting engine evidence: {best.engine}={best.confidence:.2f}, "
            f"{second.engine}={second.confidence:.2f}. Use --engine to override."
        )
    return best


def register_key(
    found: dict[str, KeyCandidate],
    key: str,
    score: int,
    source: str,
    reason: str,
    allow_raw: bool = False,
) -> None:
    normalized = normalize_key_value(key, allow_raw=allow_raw)
    if normalized is None:
        return
    key_format, key_value = normalized
    if key_format == "hex" and key_value == "0" * 32:
        return

    identity = candidate_identity(key_format, key_value)
    candidate = found.get(identity)
    if candidate is None:
        candidate = KeyCandidate(key=key_value, score=score, key_format=key_format)
        found[identity] = candidate
    candidate.score = max(candidate.score, score)
    if source not in candidate.sources:
        candidate.sources.append(source)
    candidate.reasons.add(reason)


def find_keys_in_json(
    value: Any,
    found: dict[str, KeyCandidate],
    source_name: str,
    json_path: str = "$",
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{json_path}.{key}"
            if key == "encryptionKey" and isinstance(child, str):
                score = 120 if source_name.lower().endswith("system.json") else 100
                register_key(found, child, score, source_name, f"JSON {child_path}")
            find_keys_in_json(child, found, source_name, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            find_keys_in_json(child, found, source_name, f"{json_path}[{index}]")


def suspicious_key_context(text: str, source_name: str) -> bool:
    del source_name
    return bool(SUSPICIOUS_KEY_CONTEXT_RE.search(text))


def key_text_variants(text: str, try_base64_decode: bool = True) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if value is None:
            return
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        variants.append(value)

    add(text)
    decoded, _ = decode_text_layers(text, try_base64_decode=try_base64_decode)
    add(decoded)

    backslash_decoded, _ = decode_backslash_escapes(text)
    add(backslash_decoded)

    if try_base64_decode:
        add(try_decode_base64(text))

    return variants


def could_be_key_text(text: str, allow_embedded_hex: bool = True) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32}", stripped):
        return True
    if allow_embedded_hex and ANY_HEX_KEY_RE.search(stripped):
        return True
    try:
        return len(stripped.encode("utf-8")) == HEADER_LENGTH
    except UnicodeEncodeError:
        return False


def register_key_variants(
    found: dict[str, KeyCandidate],
    text: str,
    score: int,
    source_name: str,
    reason: str,
    allow_raw: bool = False,
) -> None:
    for variant in key_text_variants(text):
        register_key(found, variant, score, source_name, reason, allow_raw=allow_raw)
        for match in ANY_HEX_KEY_RE.finditer(variant):
            register_key(found, match.group(0), score, source_name, reason)


def find_masked_keys_in_text(
    text: str,
    found: dict[str, KeyCandidate],
    source_name: str,
) -> None:
    literals: list[tuple[str, int, int]] = []
    source_is_plugin = "/js/plugins/" in source_name.lower().replace("\\", "/")

    for match in ANY_HEX_KEY_RE.finditer(text):
        start, end = match.span()
        context = text[max(0, start - 220) : min(len(text), end + 220)]
        if suspicious_key_context(context, source_name):
            register_key(
                found,
                match.group(0),
                55 if source_is_plugin else 45,
                source_name,
                "32-hex string in suspicious/plugin context",
            )

    for match in COLON_KEY_TOKEN_RE.finditer(text):
        start, end = match.span()
        value = match.group(0)
        context = text[max(0, start - 220) : min(len(text), end + 220)]
        if suspicious_key_context(context, source_name):
            register_key(
                found,
                value,
                40,
                source_name,
                "16-byte colon token in suspicious/plugin context",
                allow_raw=True,
            )

    for raw_body, start, end in iter_js_string_literal_spans(text):
        decoded_body, _ = decode_backslash_escapes(raw_body)
        literals.append((decoded_body, start, end))

        context = text[max(0, start - 220) : min(len(text), end + 220)]
        allow_raw = suspicious_key_context(context, source_name)
        score = 60 if allow_raw else 25
        stripped = decoded_body.strip()
        looks_encoded = (
            "\\" in raw_body
            or PERCENT_ESCAPE_RE.search(stripped) is not None
            or (8 <= len(stripped) <= 200 and BASE64_RE.fullmatch(stripped) is not None)
        )
        if allow_raw or ANY_HEX_KEY_RE.search(stripped) or looks_encoded:
            register_key_variants(
                found,
                decoded_body,
                score,
                source_name,
                "decoded JS/plugin string literal",
                allow_raw=allow_raw,
            )

        if "reverse" in context.lower():
            register_key_variants(
                found,
                decoded_body[::-1],
                35,
                source_name,
                "reversed JS/plugin string literal",
                allow_raw=allow_raw,
            )

    max_window = 8
    for start_index in range(len(literals)):
        for end_index in range(start_index + 2, min(len(literals), start_index + max_window) + 1):
            parts = [item[0] for item in literals[start_index:end_index]]
            window_start = literals[start_index][1]
            window_end = literals[end_index - 1][2]
            context = text[max(0, window_start - 220) : min(len(text), window_end + 220)]
            if not suspicious_key_context(context, source_name):
                continue

            joined_values = ["".join(parts)]
            if len(parts) == 2:
                first, second = parts
                joined_values.extend(
                    [
                        f"{first}:{second}",
                        f"{second}:{first}",
                        f"{first}_{second}",
                        f"{first}-{second}",
                        f"{first}.{second}",
                    ]
                )

            for joined in joined_values:
                if not could_be_key_text(joined):
                    continue
                register_key_variants(
                    found,
                    joined,
                    45,
                    source_name,
                    "joined JS/plugin string literals",
                    allow_raw=True,
                )


def derive_keys_from_known_plaintext(
    root: Path,
    found: dict[str, KeyCandidate],
    max_samples: int = 24,
) -> None:
    root = normalize_path(root)
    samples = 0
    for path in iter_files(root):
        mapped = ASSET_EXTENSIONS.get(path.suffix.lower())
        if mapped is None:
            continue
        target_ext, _kind = mapped
        known_prefix = KNOWN_PLAINTEXT_PREFIXES.get(target_ext.lower())
        if not known_prefix:
            continue
        prefix = read_prefix(path, HEADER_LENGTH + HEADER_LENGTH)
        if not detect_rpg_header(prefix):
            continue
        if len(known_prefix) < HEADER_LENGTH or len(prefix) < HEADER_LENGTH + HEADER_LENGTH:
            continue

        encrypted_block = prefix[HEADER_LENGTH : HEADER_LENGTH + HEADER_LENGTH]
        key_bytes = bytes(
            encrypted_byte ^ clear_byte
            for encrypted_byte, clear_byte in zip(encrypted_block, known_prefix[:HEADER_LENGTH])
        )
        register_key(
            found,
            key_bytes.hex(),
            700,
            relpath(path, root),
            f"derived from known {target_ext} plaintext",
        )
        samples += 1
        if samples >= max_samples:
            return


def find_keys(
    root: Path,
    max_text_mb: int = 25,
    validate_assets: bool = True,
    deep: bool = False,
) -> list[KeyCandidate]:
    root = normalize_path(root)
    max_bytes = max_text_mb * 1024 * 1024
    found: dict[str, KeyCandidate] = {}

    for path in iter_key_search_files(root, deep=deep):
        text = read_text_lossy(path, max_bytes=max_bytes)
        if text is None:
            continue

        source_name = relpath(path, root)
        if path.suffix.lower() == ".json":
            try:
                find_keys_in_json(json.loads(text), found, source_name)
            except json.JSONDecodeError:
                pass

        for match in JSON_KEY_RE.finditer(text):
            score = 110 if source_name.lower().endswith("system.json") else 90
            register_key(found, match.group(1), score, source_name, "encryptionKey literal")

        for match in KEY_CONTEXT_RE.finditer(text):
            register_key(found, match.group(1), 75, source_name, "near encryption/decrypt code")

        find_masked_keys_in_text(text, found, source_name)

        lower_name = source_name.lower().replace("\\", "/")
        if "/data/system.json" in f"/{lower_name}" or lower_name.endswith("system.json"):
            for match in ANY_HEX_KEY_RE.finditer(text):
                register_key(found, match.group(0), 50, source_name, "32-hex string in System.json")

    candidates = list(found.values())
    derive_keys_from_known_plaintext(root, found)
    candidates = list(found.values())
    if validate_assets:
        score_key_candidates_against_assets(root, candidates)
    return sorted(candidates, key=lambda c: (-c.score, c.key_format, c.key))


def detect_rpg_header(data: bytes) -> str | None:
    if len(data) < HEADER_LENGTH:
        return None
    if data[:HEADER_LENGTH] == MV_HEADER:
        return "MV"
    if data[:HEADER_LENGTH] == MZ_HEADER:
        return "MZ"
    if data[:5] == b"RPGMV":
        return "MV-like"
    if data[:5] == b"RPGMZ":
        return "MZ-like"
    return None


def xor_first_16_bytes(payload: bytes, key: bytes) -> bytes:
    output = bytearray(payload)
    for index, key_byte in enumerate(key[:HEADER_LENGTH]):
        if index >= len(output):
            break
        output[index] ^= key_byte
    return bytes(output)


def decrypt_rpg_payload(encrypted: bytes, key: bytes) -> bytes:
    return xor_first_16_bytes(encrypted[HEADER_LENGTH:], key)


def sniff_file_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) >= 16 and data[8:16] != b"\x00\x00\x00\rIHDR":
            return None
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"OggS"):
        return "ogg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4/m4a"
    if data.startswith(b"ID3") or data[:2] == b"\xff\xfb":
        return "mp3"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    return None


def expected_kind_for_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".ogg", ".mp3", ".wav"}:
        return ext.lstrip(".").replace("jpeg", "jpg")
    if ext in {".m4a", ".mp4", ".webm"}:
        return "mp4/m4a" if ext in {".m4a", ".mp4"} else None
    return None


def iter_encrypted_asset_samples(
    root: Path,
    max_samples: int = 24,
) -> Iterator[tuple[Path, str, bytes]]:
    root = normalize_path(root)
    samples = 0
    for path in iter_files(root):
        mapped = ASSET_EXTENSIONS.get(path.suffix.lower())
        if mapped is None:
            continue
        target_ext, _kind = mapped
        prefix = read_prefix(path, HEADER_LENGTH + 32)
        if not detect_rpg_header(prefix):
            continue
        yield path, target_ext, prefix
        samples += 1
        if samples >= max_samples:
            return


def score_key_candidates_against_assets(
    root: Path,
    candidates: list[KeyCandidate],
    max_samples: int = 24,
) -> None:
    samples = list(iter_encrypted_asset_samples(root, max_samples=max_samples))
    if not samples:
        return

    for candidate in candidates:
        try:
            key_bytes = key_candidate_to_bytes(candidate)
        except ValueError:
            continue

        hits = 0
        first_hit: str | None = None
        for path, target_ext, prefix in samples:
            decoded = xor_first_16_bytes(prefix[HEADER_LENGTH:], key_bytes)
            actual_kind = sniff_file_kind(decoded)
            expected_kind = expected_kind_for_ext(target_ext)
            if not actual_kind:
                continue
            if expected_kind and actual_kind != expected_kind:
                continue
            hits += 1
            if first_hit is None:
                first_hit = relpath(path, root)

        if hits:
            candidate.score += 500 + min(hits, 5) * 25
            candidate.reasons.add(f"validated against {hits} encrypted asset(s)")
            if first_hit:
                source = f"asset validation: {first_hit}"
                if source not in candidate.sources:
                    candidate.sources.append(source)


PLAIN_ASSET_EXTENSIONS: dict[str, str] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".bmp": "image",
    ".ogg": "audio",
    ".m4a": "audio",
    ".mp3": "audio",
    ".wav": "audio",
    ".webm": "video",
    ".mp4": "video",
}


def copy_plain_assets(
    input_path: Path,
    output_root: Path,
    kinds: set[str],
    preserve_structure: bool = True,
    allocator: FlatNameAllocator | None = None,
    overwrite: bool = False,
) -> Counter[str]:
    """Copy assets the game never encrypted into the same output tree.

    Most games encrypt only part of their assets. Without this, extracting a
    partly-encrypted game would silently skip every plain .png next to the
    encrypted ones.
    """

    input_path = normalize_path(input_path)
    output_root = normalize_path(output_root)
    copied: Counter[str] = Counter()
    if not kinds:
        return copied

    for source in sorted(iter_files(input_path)):
        suffix = source.suffix.lower()
        if suffix in ASSET_EXTENSIONS:
            continue
        kind = PLAIN_ASSET_EXTENSIONS.get(suffix)
        if kind is None or kind not in kinds:
            continue
        try:
            source.relative_to(output_root)
            continue  # never re-copy our own output
        except ValueError:
            pass
        relative = Path(source.name) if input_path.is_file() else source.relative_to(input_path)
        target = safe_output_path(
            output_root, plan_output_relative(relative, preserve_structure, allocator)
        )
        if target.exists() and not overwrite:
            copied["skipped"] += 1
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied[kind] += 1
            copied["total"] += 1
        except OSError:
            copied["failed"] += 1
    return copied


def collect_asset_jobs(
    input_path: Path,
    output_root: Path | None,
    kinds: set[str],
    preserve_structure: bool = True,
    allocator: FlatNameAllocator | None = None,
) -> list[AssetJob]:
    """Plan one decrypt job per encrypted asset.

    With ``preserve_structure`` the game's folder tree is mirrored under
    ``output_root``; without it every asset lands directly in ``output_root``
    under a unique name. Decrypting in place (``output_root is None``) always
    keeps files where they are, since there is no folder to flatten into.
    """

    input_path = normalize_path(input_path)
    output_root = normalize_path(output_root) if output_root else None
    files = sorted(iter_files(input_path))
    jobs: list[AssetJob] = []
    if not preserve_structure and allocator is None:
        allocator = FlatNameAllocator()

    for source in files:
        mapped = ASSET_EXTENSIONS.get(source.suffix.lower())
        if mapped is None:
            continue
        target_ext, kind = mapped
        if kind not in kinds:
            continue
        if output_root is None:
            output = source.with_suffix(target_ext)
        else:
            if input_path.is_file():
                relative = Path(source.with_suffix(target_ext).name)
            else:
                relative = source.relative_to(input_path).with_suffix(target_ext)
            output = safe_output_path(
                output_root,
                plan_output_relative(relative, preserve_structure, allocator),
            )
        jobs.append(AssetJob(source=source, output=output, kind=kind, target_ext=target_ext))

    return jobs


def decrypt_asset_job(
    job: AssetJob,
    key: bytes | None,
    overwrite: bool,
    dry_run: bool,
    force_xor: bool,
    strict: bool,
    preserve_time: bool,
) -> AssetResult:
    if dry_run:
        return AssetResult(job.source, job.output, job.kind, "dry-run")
    if job.output.exists() and not overwrite:
        return AssetResult(job.source, job.output, job.kind, "skipped", "target exists")

    try:
        encrypted = job.source.read_bytes()
    except OSError as exc:
        return AssetResult(job.source, job.output, job.kind, "error", f"read failed: {exc}")

    header = detect_rpg_header(encrypted)
    already_clear = sniff_file_kind(encrypted) is not None

    if header:
        if key is None:
            return AssetResult(job.source, job.output, job.kind, "error", "no encryption key")
        decoded = decrypt_rpg_payload(encrypted, key)
        was_encrypted = True
    elif already_clear:
        decoded = encrypted
        was_encrypted = False
    elif force_xor:
        if key is None:
            return AssetResult(job.source, job.output, job.kind, "error", "no encryption key")
        decoded = xor_first_16_bytes(encrypted, key)
        was_encrypted = True
    else:
        return AssetResult(
            job.source,
            job.output,
            job.kind,
            "error",
            "missing RPG Maker header; use --force-xor only if you know this file is headerless",
        )

    actual_kind = sniff_file_kind(decoded)
    expected_kind = expected_kind_for_ext(job.target_ext)
    if strict and expected_kind and actual_kind != expected_kind:
        return AssetResult(
            job.source,
            job.output,
            job.kind,
            "error",
            f"decoded signature is {actual_kind or 'unknown'}, expected {expected_kind}",
            was_encrypted,
        )

    try:
        job.output.parent.mkdir(parents=True, exist_ok=True)
        tmp_output = job.output.with_name(job.output.name + ".tmp")
        tmp_output.write_bytes(decoded)
        tmp_output.replace(job.output)
        if preserve_time:
            shutil.copystat(job.source, job.output)
    except OSError as exc:
        return AssetResult(job.source, job.output, job.kind, "error", f"write failed: {exc}")

    message = ""
    if expected_kind and actual_kind != expected_kind:
        message = f"warning: decoded signature is {actual_kind or 'unknown'}"
    return AssetResult(job.source, job.output, job.kind, "ok", message, was_encrypted)


def find_executable(explicit: Path | None, env_name: str, names: list[str]) -> Path | None:
    if explicit:
        path = normalize_path(explicit)
        return path if path.is_file() else None
    env_value = os.environ.get(env_name)
    if env_value:
        path = normalize_path(Path(env_value))
        if path.is_file():
            return path
    local_tools = Path(__file__).resolve().parent / "tools"
    for name in names:
        candidate = local_tools / name
        if candidate.is_file():
            return candidate
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def verify_optional_sha256(path: Path, env_name: str) -> None:
    expected = os.environ.get(env_name)
    if not expected:
        return
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise ValueError(
            f"SHA-256 mismatch for {path}. Expected {expected}, got {actual}."
        )


def run_backend_command(
    command: list[str],
    log_path: Path,
    show_keys: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(sanitize_sensitive_log(completed.stdout, show_keys), encoding="utf-8")
    return completed


def copy_tree_safe(source: Path, destination: Path) -> int:
    source = normalize_path(source)
    destination = normalize_path(destination)
    copied = 0
    for path in iter_files(source):
        relative = path.relative_to(source)
        target = safe_output_path(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def mirror_wolf_inputs_to_workspace(source: Path, workspace: Path) -> Path:
    source = normalize_path(source)
    root = resolve_game_root(source)
    workspace = normalize_path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    if source.is_file() and source.suffix.lower() in WOLF_ARCHIVE_EXTENSIONS:
        target = safe_output_path(workspace, source.name)
        shutil.copy2(source, target)
        return target

    for exe_name in ("Game.exe", "GamePro.exe"):
        exe = root / exe_name
        if exe.is_file():
            shutil.copy2(exe, workspace / exe_name)

    for archive in iter_files(root):
        if archive.suffix.lower() not in WOLF_ARCHIVE_EXTENSIONS:
            continue
        relative = archive.relative_to(root)
        target = safe_output_path(workspace, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, target)

    data_dir = root / "Data"
    if data_dir.is_dir():
        copy_tree_safe(data_dir, workspace / "Data")

    if (workspace / "GamePro.exe").is_file():
        return workspace / "GamePro.exe"
    if (workspace / "Game.exe").is_file():
        return workspace / "Game.exe"
    if (workspace / "Data").is_dir():
        return workspace / "Data"
    return workspace


def discover_wolf_archives(root: Path) -> list[Path]:
    root = resolve_game_root(root)
    archives: list[Path] = []
    if root.is_file() and root.suffix.lower() in WOLF_ARCHIVE_EXTENSIONS:
        return [root]
    for path in iter_files(root):
        if path.suffix.lower() in WOLF_ARCHIVE_EXTENSIONS:
            archives.append(path)
    return archives


def collect_new_unpacked_files(workspace: Path, extracted_dir: Path) -> int:
    copied = 0
    skipped_ext = WOLF_ARCHIVE_EXTENSIONS | {".exe", ".dll"}
    for path in iter_files(workspace):
        if path.suffix.lower() in skipped_ext:
            continue
        if ".tmp" in {part.lower() for part in path.parts}:
            continue
        relative = path.relative_to(workspace)
        target = safe_output_path(extracted_dir, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def copy_unencrypted_wolf_data(source_root: Path, extracted_dir: Path) -> int:
    copied = 0
    data_dir = resolve_game_root(source_root) / "Data"
    if data_dir.is_dir():
        for path in iter_files(data_dir):
            if path.suffix.lower() in WOLF_ARCHIVE_EXTENSIONS:
                continue
            relative = path.relative_to(data_dir)
            target = safe_output_path(extracted_dir / "Data", relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    return copied


def extract_wolf_archives(
    options: ExtractionOptions,
    detection: DetectionResult,
    manifest: dict[str, Any],
    warnings: list[str],
    errors: list[str],
) -> Path:
    output = normalize_path(options.output)
    extracted_dir = output / "extracted"
    logs_dir = output / "logs"
    source = normalize_path(options.source)
    archives = discover_wolf_archives(source)
    manifest["archives_processed"] = len(archives)
    manifest["encrypted"] = bool(archives)

    copied_plain = copy_unencrypted_wolf_data(source, extracted_dir)
    if copied_plain:
        warnings.append("Unpacked Data folder copied before archive extraction.")

    if not archives:
        if copied_plain == 0:
            errors.append("WOLF RPG detected, but no Data folder or supported archives were found.")
        return extracted_dir

    uberwolf = find_executable(
        options.uberwolf_cli,
        "UBERWOLF_CLI",
        ["UberWolfCli.exe", "UberWolfCli"],
    )
    if uberwolf is None:
        errors.append(
            "UberWolfCli was not found. Set --uberwolf-cli or UBERWOLF_CLI to unpack encrypted WOLF archives."
        )
        return extracted_dir
    verify_optional_sha256(uberwolf, "UBERWOLF_CLI_SHA256")

    temp_parent = Path(tempfile.mkdtemp(prefix="rpg_wolf_"))
    try:
        local_uberwolf = temp_parent / "UberWolfCli.exe"
        shutil.copy2(uberwolf, local_uberwolf)
        temp_dir = temp_parent / "work"
        temp_dir.mkdir(parents=True, exist_ok=True)
        backend_input = mirror_wolf_inputs_to_workspace(source, temp_dir / "game")
        command = [str(local_uberwolf), str(backend_input)]
        completed = run_backend_command(
            command,
            logs_dir / "uberwolf.log",
            show_keys=options.show_keys,
            cwd=temp_parent,
        )
        if completed.returncode != 0:
            errors.append(
                "Archive format is supported, but UberWolfCli failed. See output/logs/uberwolf.log."
            )
            return extracted_dir
        collect_new_unpacked_files(temp_dir / "game", extracted_dir)
        manifest["key_detected"] = "key" in completed.stdout.lower()
        manifest["protection_key_detected"] = (
            "protection" in completed.stdout.lower() and "key" in completed.stdout.lower()
        )
        if detection.edition == "pro" and not manifest["protection_key_detected"]:
            warnings.append(
                "WOLF RPG Pro detected, but Protection Key detection was not confirmed by backend output."
            )
    finally:
        if temp_parent.exists():
            shutil.rmtree(temp_parent, ignore_errors=True)
    return extracted_dir


def rgss_next_key(key: int) -> int:
    return (key * 7 + 3) & 0xFFFFFFFF


def rgss_xor_repeating_u32(data: bytes, key: int) -> bytes:
    key_bytes = struct.pack("<I", key)
    return bytes(byte ^ key_bytes[index % 4] for index, byte in enumerate(data))


def rgss_xor_stream(data: bytes, key: int) -> bytes:
    output = bytearray(data)
    pos = 0
    while pos < len(output):
        key_bytes = struct.pack("<I", key)
        for offset, key_byte in enumerate(key_bytes):
            index = pos + offset
            if index >= len(output):
                break
            output[index] ^= key_byte
        key = rgss_next_key(key)
        pos += 4
    return bytes(output)


def decode_rgss_path(raw: bytes) -> Path:
    for encoding in ("utf-8", "cp932", "shift_jis", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\\", "/").strip().lstrip("/")
    return Path(text)


@dataclass(frozen=True)
class RgssArchiveEntry:
    relative_path: Path
    offset: int
    size: int
    key: int


def read_rgss3a_entries(archive: Path) -> list[RgssArchiveEntry]:
    entries: list[RgssArchiveEntry] = []
    with archive.open("rb") as handle:
        header = handle.read(8)
        if header != b"RGSSAD\x00\x03":
            raise ValueError(f"unsupported RGSS archive header in {archive.name}")
        seed_raw = handle.read(4)
        if len(seed_raw) != 4:
            raise ValueError(f"truncated RGSS archive header in {archive.name}")
        key = (struct.unpack("<I", seed_raw)[0] * 9 + 3) & 0xFFFFFFFF
        archive_size = archive.stat().st_size
        while handle.tell() < archive_size:
            raw_entry = handle.read(16)
            if len(raw_entry) < 16:
                break
            offset_raw, size_raw, file_key_raw, name_size_raw = struct.unpack("<IIII", raw_entry)
            offset = offset_raw ^ key
            if offset == 0:
                break
            size = size_raw ^ key
            file_key = file_key_raw ^ key
            name_size = name_size_raw ^ key
            if name_size <= 0 or name_size > 4096:
                raise ValueError(f"invalid RGSS filename length in {archive.name}: {name_size}")
            encrypted_name = handle.read(name_size)
            if len(encrypted_name) != name_size:
                raise ValueError(f"truncated RGSS filename table in {archive.name}")
            name = decode_rgss_path(rgss_xor_repeating_u32(encrypted_name, key))
            if offset < 0 or size < 0 or offset + size > archive_size:
                raise ValueError(f"invalid RGSS file bounds for {name.as_posix()}")
            entries.append(RgssArchiveEntry(name, offset, size, file_key))
    return entries


def extract_rgss3a_archive(
    archive: Path,
    extracted_dir: Path,
    overwrite: bool = False,
    preserve_structure: bool = True,
    allocator: FlatNameAllocator | None = None,
) -> int:
    entries = read_rgss3a_entries(archive)
    written = 0
    if not preserve_structure and allocator is None:
        allocator = FlatNameAllocator()
    with archive.open("rb") as handle:
        for entry in entries:
            target = safe_output_path(
                extracted_dir,
                plan_output_relative(entry.relative_path, preserve_structure, allocator),
            )
            if target.exists() and not overwrite:
                continue
            handle.seek(entry.offset)
            encrypted = handle.read(entry.size)
            if len(encrypted) != entry.size:
                raise ValueError(f"truncated RGSS file payload: {entry.relative_path.as_posix()}")
            decoded = rgss_xor_stream(encrypted, entry.key)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_output = target.with_name(target.name + ".tmp")
            tmp_output.write_bytes(decoded)
            tmp_output.replace(target)
            written += 1
    return written


def copy_rgss_sidecar_files(
    source_root: Path,
    extracted_dir: Path,
    overwrite: bool = False,
    preserve_structure: bool = True,
    allocator: FlatNameAllocator | None = None,
) -> int:
    copied = 0
    if not preserve_structure and allocator is None:
        allocator = FlatNameAllocator()
    for folder_name in ("Audio", "Data", "Fonts", "Graphics", "Movies", "System"):
        source = source_root / folder_name
        if not source.is_dir():
            continue
        for path in sorted(iter_files(source)):
            relative = path.relative_to(source_root)
            target = safe_output_path(
                extracted_dir,
                plan_output_relative(relative, preserve_structure, allocator),
            )
            if target.exists() and not overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    return copied


def extract_images_from_tree(
    source_dir: Path,
    output_images_dir: Path,
    preserve_structure: bool = True,
) -> tuple[int, list[dict[str, Any]], list[str]]:
    """Copy every image found under ``source_dir`` into ``output_images_dir``.

    With ``preserve_structure`` the source tree is mirrored. Without it the
    images are collected into one flat folder, and images whose content is
    already present are skipped instead of being written twice under a
    disambiguated name.
    """

    source_dir = normalize_path(source_dir)
    output_images_dir = normalize_path(output_images_dir)
    index: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_hash_paths: set[tuple[str, str]] = set()
    seen_digests: set[str] = set()
    duplicates_skipped = 0
    allocator = None if preserve_structure else FlatNameAllocator()

    if not source_dir.exists():
        return 0, index, [f"Image source directory does not exist: {source_dir}"]

    for path in sorted(iter_files(source_dir)):
        ext = path.suffix.lower()
        if ext not in WOLF_IMAGE_EXTENSIONS:
            prefix = read_prefix(path, 16)
            actual_ext = image_extension_from_signature(prefix)
            if actual_ext is None:
                continue
        else:
            actual_ext = image_extension_from_signature(read_prefix(path, 16))
            if actual_ext is None:
                warnings.append(f"Skipped image with unknown signature: {path}")
                continue

        try:
            relative = path.relative_to(source_dir)
        except ValueError:
            continue
        source_relative = normalize_image_output_relative(relative, actual_ext)
        digest = sha256_file(path)
        dedupe_key = (source_relative.as_posix().lower(), digest)
        if dedupe_key in seen_hash_paths:
            continue
        seen_hash_paths.add(dedupe_key)
        if not preserve_structure:
            if digest in seen_digests:
                duplicates_skipped += 1
                continue
            seen_digests.add(digest)

        output_relative = plan_output_relative(source_relative, preserve_structure, allocator)
        target = safe_output_path(output_images_dir, output_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or sha256_file(target) != digest:
            shutil.copy2(path, target)
        index.append(
            {
                "source_path": relative.as_posix(),
                "output_path": output_relative.as_posix(),
                "sha256": digest,
                "format": actual_ext.lstrip("."),
            }
        )

    if duplicates_skipped:
        warnings.append(
            f"Flat output: skipped {duplicates_skipped} duplicate image(s) with identical content."
        )
    atomic_write_json(output_images_dir / "index.json", index)
    return len(index), index, warnings


def stable_text_id(engine: str, source_file: str, context: str, original: str) -> str:
    payload = f"{engine}\0{source_file}\0{context}\0{original}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def find_control_codes(text: str) -> list[str]:
    patterns = [
        r"\\[A-Za-z]+\[[^\]]*\]",
        r"\\[A-Za-z]+",
        r"\$\{[^}]+\}",
        r"%\d+",
    ]
    codes: list[str] = []
    for pattern in patterns:
        codes.extend(match.group(0) for match in re.finditer(pattern, text))
    return codes


def iter_strings_from_json_like(
    data: Any,
    source_file: str,
    path: str = "$",
    include_comments: bool = False,
) -> Iterator[dict[str, Any]]:
    if isinstance(data, dict):
        for key, value in data.items():
            child_path = f"{path}.{key}"
            if isinstance(value, str):
                key_lower = key.lower()
                if not include_comments and "comment" in key_lower:
                    continue
                if should_keep_text(value, include_all=False):
                    yield {
                        "category": "wolf-text",
                        "source_file": source_file,
                        "context": child_path,
                        "original": value,
                        "metadata": {"field": key},
                    }
            else:
                yield from iter_strings_from_json_like(value, source_file, child_path, include_comments)
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from iter_strings_from_json_like(value, source_file, f"{path}[{index}]", include_comments)


def load_wolftl_dump_entries(dump_dir: Path, include_comments: bool = False) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not dump_dir.exists():
        return entries
    for path in iter_files(dump_dir):
        if path.suffix.lower() != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        source_file = path.relative_to(dump_dir).as_posix()
        for raw in iter_strings_from_json_like(data, source_file, include_comments=include_comments):
            original = raw["original"]
            context = raw["context"]
            entries.append(
                {
                    "id": stable_text_id(ENGINE_WOLF_RPG, source_file, context, original),
                    "engine": ENGINE_WOLF_RPG,
                    "category": raw["category"],
                    "source_file": source_file,
                    "context": context,
                    "original": original,
                    "translation": "",
                    "control_codes": find_control_codes(original),
                    "metadata": raw["metadata"],
                }
            )
    entries.sort(key=lambda item: (item["source_file"], item["context"], item["id"]))
    return entries


def extract_wolf_text_with_wolftl(
    options: ExtractionOptions,
    extracted_dir: Path,
    manifest: dict[str, Any],
    warnings: list[str],
    errors: list[str],
) -> TextExtractionResult:
    output = normalize_path(options.output)
    translation_dir = output / "translation"
    logs_dir = output / "logs"
    translation_dir.mkdir(parents=True, exist_ok=True)

    wolftl = find_executable(options.wolftl_cli, "WOLFTL_CLI", ["WolfTL.exe", "WolfTL"])
    if wolftl is None:
        error = "WolfTL was not found. Set --wolftl-cli or WOLFTL_CLI to extract WOLF text."
        errors.append(error)
        return TextExtractionResult(0, warnings=warnings, errors=[error])
    verify_optional_sha256(wolftl, "WOLFTL_CLI_SHA256")

    data_folder = extracted_dir / "Data"
    if not data_folder.exists():
        data_folder = extracted_dir
    work_dir = output / "translation" / "wolftl"
    work_dir.mkdir(parents=True, exist_ok=True)
    command = [str(wolftl), str(data_folder), str(work_dir), "create"]
    completed = run_backend_command(command, logs_dir / "wolftl.log", cwd=output)
    if completed.returncode != 0:
        error = "The game was unpacked successfully, but MapData could not be parsed by WolfTL."
        errors.append(error)
        return TextExtractionResult(0, output=translation_dir, warnings=warnings, errors=[error])

    entries = load_wolftl_dump_entries(work_dir / "dump", include_comments=options.include_comments)
    jsonl_path = translation_dir / "wolf_translation.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    manifest["text_entries_extracted"] = len(entries)
    return TextExtractionResult(len(entries), jsonl_path, warnings=warnings, errors=errors)


def decode_backslash_escapes(text: str) -> tuple[str, bool]:
    changed = False

    def repl_u_braced(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        codepoint = int(match.group(1), 16)
        return chr(codepoint)

    def repl_u(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return chr(int(match.group(1), 16))

    def repl_x(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return chr(int(match.group(1), 16))

    decoded = re.sub(r"\\u\{([0-9a-fA-F]{1,6})\}", repl_u_braced, text)
    decoded = re.sub(r"\\u([0-9a-fA-F]{4})", repl_u, decoded)
    decoded = re.sub(r"\\x([0-9a-fA-F]{2})", repl_x, decoded)

    replacements = {
        r"\r\n": "\n",
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\/": "/",
        r"\"": '"',
        r"\'": "'",
    }
    for old, new in replacements.items():
        if old in decoded:
            decoded = decoded.replace(old, new)
            changed = True

    return decoded, changed


def printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    printable = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    return printable / len(text)


def try_decode_base64(text: str) -> str | None:
    compact = "".join(text.strip().split())
    if len(compact) < 8 or len(compact) > 20000:
        return None
    if not BASE64_RE.fullmatch(compact):
        return None
    padding = "=" * ((4 - len(compact) % 4) % 4)

    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(compact + padding)
        except (binascii.Error, ValueError):
            continue
        if not raw or b"\x00" in raw[:100]:
            continue
        for encoding in ("utf-8", "cp932", "shift_jis"):
            try:
                decoded = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            if printable_ratio(decoded) >= 0.85 and LANGUAGE_RE.search(decoded):
                return decoded
    return None


def decode_text_layers(
    text: str,
    try_base64_decode: bool = False,
    max_rounds: int = 3,
) -> tuple[str, tuple[str, ...]]:
    current = text
    decoders: list[str] = []

    for _ in range(max_rounds):
        round_changed = False

        decoded, changed = decode_backslash_escapes(current)
        if changed and decoded != current:
            current = decoded
            decoders.append("backslash")
            round_changed = True

        html_decoded = html.unescape(current)
        if html_decoded != current:
            current = html_decoded
            decoders.append("html")
            round_changed = True

        if PERCENT_ESCAPE_RE.search(current):
            url_decoded = unquote(current)
            if url_decoded != current:
                current = url_decoded
                decoders.append("percent")
                round_changed = True

        if try_base64_decode:
            b64_decoded = try_decode_base64(current)
            if b64_decoded and b64_decoded != current:
                current = b64_decoded
                decoders.append("base64")
                round_changed = True

        if not round_changed:
            break

    return current, tuple(decoders)


def path_join(base: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{base}[{key}]"
    if base == "$":
        return f"$.{key}"
    return f"{base}.{key}"


def should_keep_text(text: str, include_all: bool) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if not include_all and len(stripped) > 2000:
        return False
    if not include_all and FILE_REF_RE.fullmatch(stripped):
        return False
    if include_all:
        return True
    return bool(LANGUAGE_RE.search(stripped))


def iter_event_command_strings(
    obj: dict[str, Any],
    path: str,
    include_comments: bool,
) -> Iterator[tuple[str, str, str]]:
    code = obj.get("code")
    params = obj.get("parameters")
    if not isinstance(code, int) or not isinstance(params, list):
        return

    for index in EVENT_TEXT_PARAM_INDEXES.get(code, ()):
        if index < len(params) and isinstance(params[index], str):
            yield (f"{path}.parameters[{index}]", "event", params[index])

    if code in EVENT_CHOICE_CODES and params and isinstance(params[0], list):
        for choice_index, choice in enumerate(params[0]):
            if isinstance(choice, str):
                yield (f"{path}.parameters[0][{choice_index}]", "choice", choice)

    if include_comments and code in EVENT_COMMENT_CODES and params and isinstance(params[0], str):
        yield (f"{path}.parameters[0]", "comment", params[0])


def iter_json_text_records(
    value: Any,
    file_name: str,
    include_all: bool,
    include_comments: bool,
    try_base64_decode: bool,
    path: str = "$",
) -> Iterator[TextRecord]:
    if isinstance(value, dict):
        for event_path, kind, text in iter_event_command_strings(value, path, include_comments):
            if should_keep_text(text, include_all):
                decoded, decoders = decode_text_layers(text, try_base64_decode)
                yield TextRecord(file_name, event_path, kind, text, decoded, decoders)

        for key, child in value.items():
            child_path = path_join(path, key)
            if isinstance(child, str):
                key_is_translatable = include_all or key in TRANSLATABLE_KEYS
                if key_is_translatable and should_keep_text(child, include_all):
                    decoded, decoders = decode_text_layers(child, try_base64_decode)
                    yield TextRecord(file_name, child_path, "json", child, decoded, decoders)
            else:
                yield from iter_json_text_records(
                    child,
                    file_name,
                    include_all,
                    include_comments,
                    try_base64_decode,
                    child_path,
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_json_text_records(
                child,
                file_name,
                include_all,
                include_comments,
                try_base64_decode,
                path_join(path, index),
            )


def iter_js_strings(
    text: str,
    file_name: str,
    include_all: bool,
    try_base64_decode: bool,
) -> Iterator[TextRecord]:
    for index, match in enumerate(JS_STRING_RE.finditer(text)):
        raw_body = match.group("body")
        decoded_body, _ = decode_backslash_escapes(raw_body)
        if not should_keep_text(decoded_body, include_all):
            continue
        decoded, decoders = decode_text_layers(decoded_body, try_base64_decode)
        yield TextRecord(
            file=file_name,
            path=f"$strings[{index}]",
            kind="js",
            text=decoded_body,
            decoded=decoded,
            decoders=decoders,
        )


def iter_text_records(
    root: Path,
    include_js: bool,
    include_all: bool,
    include_comments: bool,
    try_base64_decode: bool,
    max_text_mb: int,
) -> Iterator[TextRecord]:
    root = normalize_path(root)
    max_bytes = max_text_mb * 1024 * 1024

    for path in iter_files(root):
        suffix = path.suffix.lower()
        if suffix not in TEXT_EXTENSIONS:
            continue
        if suffix == ".js" and not include_js:
            continue

        text = read_text_lossy(path, max_bytes=max_bytes)
        if text is None:
            continue
        file_name = relpath(path, root)

        if suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            yield from iter_json_text_records(
                data,
                file_name,
                include_all,
                include_comments,
                try_base64_decode,
            )
        elif include_js and suffix == ".js":
            yield from iter_js_strings(text, file_name, include_all, try_base64_decode)


def write_text_records(records: Iterable[TextRecord], output: Path | None, fmt: str) -> int:
    count = 0
    if output:
        output = normalize_path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        handle = output.open("w", encoding="utf-8", newline="")
        close_handle = True
    else:
        handle = sys.stdout
        close_handle = False

    try:
        if fmt == "jsonl":
            for record in records:
                handle.write(
                    json.dumps(
                        {
                            "file": record.file,
                            "path": record.path,
                            "kind": record.kind,
                            "text": record.text,
                            "decoded": record.decoded,
                            "decoders": list(record.decoders),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                count += 1
        elif fmt == "csv":
            writer = csv.DictWriter(
                handle,
                fieldnames=["file", "path", "kind", "text", "decoded", "decoders"],
            )
            writer.writeheader()
            for record in records:
                writer.writerow(
                    {
                        "file": record.file,
                        "path": record.path,
                        "kind": record.kind,
                        "text": record.text,
                        "decoded": record.decoded,
                        "decoders": ",".join(record.decoders),
                    }
                )
                count += 1
        elif fmt == "txt":
            for record in records:
                handle.write(record.decoded + "\n")
                count += 1
        else:
            raise ValueError(f"unsupported text output format: {fmt}")
    finally:
        if close_handle:
            handle.close()

    return count


def write_unified_translation_records(records: Iterable[TextRecord], output: Path, engine: str) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            entry = {
                "id": stable_text_id(engine, record.file, record.path, record.decoded),
                "engine": engine,
                "category": record.kind,
                "source_file": record.file,
                "context": record.path,
                "original": record.decoded,
                "translation": "",
                "control_codes": find_control_codes(record.decoded),
                "metadata": {"raw": record.text, "decoders": list(record.decoders)},
            }
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            count += 1
    return count


class RpgMakerVxAceEngineAdapter:
    engine_id = ENGINE_RPGMAKER_VX_ACE

    def probe(self, path: Path) -> DetectionResult:
        return probe_rpgmaker_vx_ace(path)

    def validate(self, path: Path) -> ValidationResult:
        detection = self.probe(path)
        if detection.confidence < 0.35:
            return ValidationResult(False, warnings=detection.warnings, errors=["RPG Maker VX Ace structure was not recognized."])
        archives = discover_rgss_archives(normalize_path(path))
        if not archives and not has_dir(resolve_game_root(path), "Data"):
            return ValidationResult(False, warnings=detection.warnings, errors=["No RGSS archive or Data folder was found."])
        return ValidationResult(True, warnings=detection.warnings)

    def extract_resources(self, options: ExtractionOptions) -> ExtractionResult:
        source = normalize_path(options.source)
        source_root = resolve_game_root(source)
        output = normalize_path(options.output)
        extracted_dir = output / "extracted"
        images_dir = output / "images"
        output.mkdir(parents=True, exist_ok=True)
        extracted_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        errors: list[str] = []
        detection = self.probe(source)
        warnings.extend(detection.warnings)

        archives = discover_rgss_archives(source)
        archived_files = 0
        archives_processed = 0
        # One allocator for the whole output folder, so archive entries and the
        # plain sidecar files cannot claim the same flat name.
        allocator = None if options.preserve_structure else FlatNameAllocator()
        for archive in archives:
            if archive.suffix.lower() != ".rgss3a":
                warnings.append(f"Skipped unsupported RGSS archive version: {archive.name}")
                continue
            try:
                archived_files += extract_rgss3a_archive(
                    archive,
                    extracted_dir,
                    options.overwrite,
                    options.preserve_structure,
                    allocator,
                )
                archives_processed += 1
            except (OSError, ValueError) as exc:
                errors.append(f"{archive.name}: {exc}")

        copied_plain = 0
        if source_root.exists():
            try:
                copied_plain = copy_rgss_sidecar_files(
                    source_root,
                    extracted_dir,
                    options.overwrite,
                    options.preserve_structure,
                    allocator,
                )
            except (OSError, ValueError) as exc:
                errors.append(f"Plain file copy failed: {exc}")

        image_count = 0
        if options.images or options.resources:
            image_count, _index, image_warnings = extract_images_from_tree(
                extracted_dir, images_dir, options.preserve_structure
            )
            warnings.extend(image_warnings)

        manifest = {
            "engine": ENGINE_RPGMAKER_VX_ACE,
            "edition": detection.edition,
            "detection_confidence": detection.confidence,
            "source_root": str(source_root),
            "extraction_time": datetime.now(timezone.utc).isoformat(),
            "encrypted": bool(archives),
            "key_detected": bool(archives_processed),
            "protection_key_detected": False,
            "archives_processed": archives_processed,
            "files_extracted": archived_files,
            "plain_files_copied": copied_plain,
            "folder_structure": structure_mode(options.preserve_structure),
            "images_extracted": image_count,
            "text_entries_extracted": 0,
            "warnings": warnings,
            "errors": errors,
        }
        atomic_write_json(output / "manifest.json", manifest)
        return ExtractionResult(ENGINE_RPGMAKER_VX_ACE, output, manifest, image_count, warnings=warnings, errors=errors)

    def extract_text(self, options: ExtractionOptions) -> TextExtractionResult:
        output_root = normalize_path(options.output)
        extracted_dir = output_root / "extracted"
        source = extracted_dir if extracted_dir.exists() else resolve_game_root(options.source)
        records = iter_text_records(
            source,
            include_js=False,
            include_all=False,
            include_comments=options.include_comments,
            try_base64_decode=False,
            max_text_mb=25,
        )
        output = output_root / "translation" / "translation.jsonl"
        count = write_unified_translation_records(records, output, ENGINE_RPGMAKER_VX_ACE)
        return TextExtractionResult(count, output)

    def apply_translation(self, options: Any) -> PatchResult:
        return PatchResult(
            False,
            errors=["RPG Maker VX Ace patching through the unified adapter is not enabled."],
        )


class RpgMakerEngineAdapter:
    def __init__(self, engine_id: str) -> None:
        if engine_id not in {ENGINE_RPGMAKER_MV, ENGINE_RPGMAKER_MZ}:
            raise ValueError(f"invalid RPG Maker engine: {engine_id}")
        self.engine_id = engine_id

    def probe(self, path: Path) -> DetectionResult:
        return probe_rpgmaker(path, self.engine_id)

    def validate(self, path: Path) -> ValidationResult:
        detection = self.probe(path)
        if detection.confidence < 0.35:
            return ValidationResult(False, warnings=detection.warnings, errors=["RPG Maker structure was not recognized."])
        return ValidationResult(True, warnings=detection.warnings)

    def extract_resources(self, options: ExtractionOptions) -> ExtractionResult:
        source = normalize_path(options.source)
        output = normalize_path(options.output)
        extracted_dir = output / "extracted"
        logs_dir = output / "logs"
        output.mkdir(parents=True, exist_ok=True)
        extracted_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        errors: list[str] = []

        if options.asset_kinds:
            kinds = set(options.asset_kinds)
        elif options.resources:
            kinds = {"image", "audio", "video"}
        elif options.images:
            kinds = {"image"}
        else:
            kinds = set()
        # One allocator for the whole folder so decrypted and plain assets
        # cannot claim the same flat name.
        allocator = None if options.preserve_structure else FlatNameAllocator()
        jobs = collect_asset_jobs(
            source, extracted_dir, kinds, options.preserve_structure, allocator
        )
        key = resolve_key_arg(options.key or "auto", source, quiet=True)
        if jobs and key is None:
            errors.append("No RPG Maker encryption key was found.")

        results: list[AssetResult] = []
        if key is not None or not jobs:
            max_workers = options.workers or min(32, (os.cpu_count() or 4) + 4)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        decrypt_asset_job,
                        job,
                        key,
                        options.overwrite,
                        False,
                        False,
                        options.strict,
                        True,
                    )
                    for job in jobs
                ]
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    if result.status == "error":
                        errors.append(f"{relpath(result.source, source)}: {result.message}")

        plain = copy_plain_assets(
            source,
            extracted_dir,
            kinds,
            options.preserve_structure,
            allocator,
            options.overwrite,
        )
        # Decrypted assets already land in extracted/ in their final form, so a
        # second copy under images/ would only double the disk usage.
        image_count = sum(
            1 for result in results if result.kind == "image" and result.status in {"ok", "skipped"}
        ) + plain["image"]

        manifest = {
            "engine": self.engine_id,
            "edition": None,
            "detection_confidence": self.probe(source).confidence,
            "source_root": str(source),
            "extraction_time": datetime.now(timezone.utc).isoformat(),
            "encrypted": bool(jobs),
            "key_detected": key is not None,
            "protection_key_detected": False,
            "archives_processed": 0,
            "folder_structure": structure_mode(options.preserve_structure),
            "asset_kinds": sorted(kinds),
            "assets_decrypted": sum(1 for result in results if result.status == "ok"),
            "plain_assets_copied": plain["total"],
            "images_location": "extracted",
            "images_extracted": image_count,
            "text_entries_extracted": 0,
            "warnings": warnings,
            "errors": errors,
        }
        atomic_write_json(output / "manifest.json", manifest)
        return ExtractionResult(self.engine_id, output, manifest, image_count, warnings=warnings, errors=errors)

    def extract_text(self, options: ExtractionOptions) -> TextExtractionResult:
        records = iter_text_records(
            options.source,
            include_js=False,
            include_all=False,
            include_comments=options.include_comments,
            try_base64_decode=False,
            max_text_mb=25,
        )
        output = normalize_path(options.output) / "translation" / "translation.jsonl"
        count = write_unified_translation_records(records, output, self.engine_id)
        return TextExtractionResult(count, output)

    def apply_translation(self, options: Any) -> PatchResult:
        return PatchResult(
            False,
            errors=["RPG Maker patching through the unified adapter is not enabled; use the existing project import workflow."],
        )


class WolfRpgEngineAdapter:
    engine_id = ENGINE_WOLF_RPG

    def probe(self, path: Path) -> DetectionResult:
        return probe_wolf_rpg(path)

    def validate(self, path: Path) -> ValidationResult:
        detection = self.probe(path)
        errors: list[str] = []
        if detection.confidence < 0.35:
            errors.append("WOLF RPG structures were not recognized.")
        return ValidationResult(not errors, warnings=detection.warnings, errors=errors)

    def extract_resources(self, options: ExtractionOptions) -> ExtractionResult:
        source = normalize_path(options.source)
        output = normalize_path(options.output)
        output.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        errors: list[str] = []
        detection = self.probe(source)
        warnings.extend(detection.warnings)
        manifest = {
            "engine": ENGINE_WOLF_RPG,
            "edition": detection.edition,
            "detection_confidence": detection.confidence,
            "source_root": str(resolve_game_root(source)),
            "extraction_time": datetime.now(timezone.utc).isoformat(),
            "encrypted": False,
            "key_detected": False,
            "protection_key_detected": False,
            "archives_processed": 0,
            "folder_structure": structure_mode(options.preserve_structure),
            "images_extracted": 0,
            "text_entries_extracted": 0,
            "warnings": warnings,
            "errors": errors,
        }
        extracted_dir = extract_wolf_archives(options, detection, manifest, warnings, errors)
        image_count = 0
        if options.images or options.resources:
            image_count, _index, image_warnings = extract_images_from_tree(
                extracted_dir, output / "images", options.preserve_structure
            )
            warnings.extend(image_warnings)
            manifest["images_extracted"] = image_count
        if not options.preserve_structure:
            warnings.append(
                "WOLF RPG: the raw unpack in extracted/ keeps its original folders; "
                "the flat layout applies to images/."
            )
        manifest["warnings"] = warnings
        manifest["errors"] = errors
        atomic_write_json(output / "manifest.json", manifest)
        return ExtractionResult(ENGINE_WOLF_RPG, output, manifest, image_count, warnings=warnings, errors=errors)

    def extract_text(self, options: ExtractionOptions) -> TextExtractionResult:
        output = normalize_path(options.output)
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        warnings = list(manifest.get("warnings", []))
        errors = list(manifest.get("errors", []))
        result = extract_wolf_text_with_wolftl(options, output / "extracted", manifest, warnings, errors)
        manifest["warnings"] = warnings
        manifest["errors"] = errors
        manifest["text_entries_extracted"] = result.count
        atomic_write_json(manifest_path, manifest)
        return result

    def apply_translation(self, options: Any) -> PatchResult:
        translation = normalize_path(Path(options.translation))
        game = normalize_path(Path(options.game))
        output = normalize_path(Path(options.output))
        wolftl = find_executable(getattr(options, "wolftl_cli", None), "WOLFTL_CLI", ["WolfTL.exe", "WolfTL"])
        if wolftl is None:
            return PatchResult(False, errors=["WolfTL was not found. Set --wolftl-cli or WOLFTL_CLI."])
        verify_optional_sha256(wolftl, "WOLFTL_CLI_SHA256")
        output.mkdir(parents=True, exist_ok=True)
        patched_game = output / "patched-game"
        if patched_game.exists() and getattr(options, "overwrite", False):
            shutil.rmtree(patched_game)
        if not patched_game.exists():
            copy_tree_safe(resolve_game_root(game), patched_game)
        work_dir = translation.parent
        log_path = output / "logs" / "wolftl-patch.log"
        completed = run_backend_command(
            [str(wolftl), str(patched_game / "Data"), str(work_dir), "patch"],
            log_path,
            cwd=output,
        )
        if completed.returncode != 0:
            return PatchResult(False, patched_game, errors=["WolfTL patch failed. See logs/wolftl-patch.log."])
        return PatchResult(True, patched_game)


def get_engine_adapter(engine: str) -> EngineAdapter:
    if engine == ENGINE_RPGMAKER_MV:
        return RpgMakerEngineAdapter(ENGINE_RPGMAKER_MV)
    if engine == ENGINE_RPGMAKER_MZ:
        return RpgMakerEngineAdapter(ENGINE_RPGMAKER_MZ)
    if engine == ENGINE_RPGMAKER_VX_ACE:
        return RpgMakerVxAceEngineAdapter()
    if engine == ENGINE_WOLF_RPG:
        return WolfRpgEngineAdapter()
    raise ValueError(f"unsupported engine: {engine}")


def run_unified_extraction(options: ExtractionOptions) -> ExtractionResult:
    detection = detect_engine(options.source, options.engine)
    if detection.confidence < 0.35:
        raise ValueError("; ".join(detection.warnings) or "Unable to detect engine.")
    if detection.warnings and any("Conflicting engine evidence" in item for item in detection.warnings):
        raise ValueError("; ".join(detection.warnings))
    adapter = get_engine_adapter(detection.engine)
    validation = adapter.validate(options.source)
    if not validation.ok:
        raise ValueError("; ".join(validation.errors))
    resource_result = adapter.extract_resources(options)
    if options.text:
        text_result = adapter.extract_text(options)
        resource_result.text_entries = text_result.count
        resource_result.manifest["text_entries_extracted"] = text_result.count
        resource_result.manifest["warnings"] = list(
            dict.fromkeys(resource_result.manifest.get("warnings", []) + text_result.warnings)
        )
        resource_result.manifest["errors"] = list(
            dict.fromkeys(resource_result.manifest.get("errors", []) + text_result.errors)
        )
        atomic_write_json(resource_result.output / "manifest.json", resource_result.manifest)
    return resource_result


# ---------------------------------------------------------------------------
# Editable project rebuild
# ---------------------------------------------------------------------------

# nw.js runtime that a deployed game carries around; an editable project has no
# use for it, and it is what makes a "copy of the game folder" huge.
RUNTIME_DIR_NAMES = {"locales", "swiftshader", "pnacl"}
RUNTIME_FILE_SUFFIXES = {".exe", ".dll", ".pak", ".so", ".dylib", ".msi", ".sys", ".bin"}
RUNTIME_FILE_NAMES = {"debug.log", "credits.html.bak"}

PROJECT_MARKERS: dict[str, tuple[str, str]] = {
    ENGINE_RPGMAKER_MV: ("Game.rpgproject", "RPGMV 1.6.2"),
    ENGINE_RPGMAKER_MZ: ("Game.rmmzproject", "RPGMZ 1.0.0"),
}


@dataclass
class ProjectBuildOptions:
    source: Path
    output: Path
    key: str = "auto"
    engine: str = ENGINE_AUTO
    overwrite: bool = False
    include_runtime: bool = False
    strict: bool = True
    workers: int = 0


@dataclass
class ProjectBuildResult:
    output: Path
    engine: str
    manifest: dict[str, Any]
    files_copied: int = 0
    files_decrypted: int = 0
    files_skipped: int = 0
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def is_runtime_file(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if parts and parts[0] in RUNTIME_DIR_NAMES:
        return True
    if relative.name.lower() in RUNTIME_FILE_NAMES:
        return True
    return relative.suffix.lower() in RUNTIME_FILE_SUFFIXES


def project_content_root(game_root: Path) -> Path:
    """Folder holding data/img/js — ``www`` in a deployed MV game, else the root.

    Flattening ``www`` up is what turns a deployed game back into the layout the
    editor expects.
    """

    www = game_root / "www"
    if www.is_dir() and (www / "data").is_dir():
        return www
    return game_root


def plan_project_copy(
    game_root: Path,
    content_root: Path,
    include_runtime: bool = False,
) -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for path in sorted(iter_files(content_root)):
        relative = path.relative_to(content_root)
        if not include_runtime and is_runtime_file(relative):
            continue
        entries.append((path, relative))
        seen.add(relative.as_posix().lower())
    if content_root != game_root:
        # Files that live beside www/ — package.json, Game.rpgproject, icons.
        for path in sorted(game_root.iterdir()):
            if not path.is_file():
                continue
            relative = Path(path.name)
            if not include_runtime and is_runtime_file(relative):
                continue
            if relative.as_posix().lower() in seen:
                continue
            entries.append((path, relative))
            seen.add(relative.as_posix().lower())
    return entries


def patch_system_json_for_editing(project_root: Path) -> tuple[bool, list[str]]:
    """Clear the encryption flags so the editor and the game read plain files."""

    system_path = project_root / "data" / "System.json"
    if not system_path.is_file():
        return False, ["data/System.json was not found; encryption flags were left as they were."]
    try:
        data = json.loads(system_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"data/System.json could not be read: {exc}"]
    if not isinstance(data, dict):
        return False, ["data/System.json has an unexpected shape; encryption flags were left as they were."]

    changed = False
    if data.pop("encryptionKey", None) is not None:
        changed = True
    for flag in ("hasEncryptedImages", "hasEncryptedAudio"):
        if data.get(flag):
            data[flag] = False
            changed = True
    if changed:
        atomic_write_json(system_path, data)
    return changed, []


def fix_package_json_main(project_root: Path) -> bool:
    """Point package.json at index.html once www/ has been flattened up."""

    package_path = project_root / "package.json"
    if not package_path.is_file():
        return False
    try:
        data = json.loads(package_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    main = data.get("main")
    if not isinstance(main, str) or not main.lower().startswith("www/"):
        return False
    data["main"] = main[4:]
    atomic_write_json(package_path, data)
    return True


def ensure_project_marker(project_root: Path, engine: str) -> str | None:
    """Create Game.rpgproject / Game.rmmzproject when the game shipped without it."""

    if any((project_root / name).is_file() for name, _content in PROJECT_MARKERS.values()):
        return None
    marker = PROJECT_MARKERS.get(engine)
    if marker is None:
        return None
    name, content = marker
    (project_root / name).write_text(f"{content}\n", encoding="utf-8")
    return name


def preview_project_build(options: ProjectBuildOptions) -> tuple[Path, list[tuple[Path, Path]]]:
    """What a project build would produce: the game root and every source → target pair."""

    source = normalize_path(options.source)
    game_root = find_game_root_upwards(source) or resolve_game_root(source)
    detection = detect_engine(game_root, options.engine)
    if detection.engine not in PROJECT_MARKERS:
        raise ValueError(
            "Project mode supports RPG Maker MV and MZ only; "
            f"detected engine: {detection.engine}."
        )
    output = normalize_path(options.output)
    content_root = project_content_root(game_root)
    planned: list[tuple[Path, Path]] = []
    for source_path, relative in plan_project_copy(game_root, content_root, options.include_runtime):
        mapped = ASSET_EXTENSIONS.get(source_path.suffix.lower())
        target_relative = relative.with_suffix(mapped[0]) if mapped else relative
        planned.append((source_path, output / target_relative))
    return game_root, planned


def build_editable_project(
    options: ProjectBuildOptions,
    *,
    log: Any = None,
    progress: Any = None,
    cancelled: Any = None,
) -> ProjectBuildResult:
    """Rebuild a deployed RPG Maker game as a folder the editor can open.

    The game is copied into ``options.output`` with ``www`` flattened up to the
    project root, every encrypted asset decrypted back to .png/.ogg/.m4a, the
    encryption flags cleared in System.json, and a project file created if the
    deployed build did not ship one.
    """

    log = log or (lambda _message: None)
    progress = progress or (lambda _done, _total: None)
    cancelled = cancelled or (lambda: False)

    source = normalize_path(options.source)
    game_root = find_game_root_upwards(source) or resolve_game_root(source)
    detection = detect_engine(game_root, options.engine)
    warnings: list[str] = list(detection.warnings)
    errors: list[str] = []

    if detection.engine not in PROJECT_MARKERS:
        raise ValueError(
            "Project mode supports RPG Maker MV and MZ only; "
            f"detected engine: {detection.engine}."
        )
    if detection.confidence < 0.35:
        raise ValueError(
            "; ".join(detection.warnings)
            or "The selected folder does not look like an RPG Maker MV/MZ game."
        )

    output = normalize_path(options.output)
    output.mkdir(parents=True, exist_ok=True)
    content_root = project_content_root(game_root)
    entries = plan_project_copy(game_root, content_root, options.include_runtime)
    if not entries:
        raise ValueError(f"No project files were found in {game_root}.")

    log(f"Game root: {game_root}")
    log(f"Project: {output}")
    log(f"Files to process: {len(entries)}")
    if content_root != game_root:
        log("Layout: www/ is flattened into the project root.")

    key = resolve_key_arg(options.key, game_root, quiet=True)
    encrypted_present = any(
        source_path.suffix.lower() in ASSET_EXTENSIONS for source_path, _relative in entries
    )
    if encrypted_present and key is None:
        errors.append("No RPG Maker encryption key was found; encrypted assets were left as they are.")

    copy_jobs: list[tuple[Path, Path]] = []
    asset_jobs: list[AssetJob] = []
    skipped = 0
    for source_path, relative in entries:
        mapped = ASSET_EXTENSIONS.get(source_path.suffix.lower())
        target_relative = relative.with_suffix(mapped[0]) if mapped else relative
        target = safe_output_path(output, target_relative)
        if target.exists() and not options.overwrite:
            skipped += 1
            continue
        if mapped and key is not None:
            asset_jobs.append(
                AssetJob(source=source_path, output=target, kind=mapped[1], target_ext=mapped[0])
            )
        else:
            copy_jobs.append((source_path, target))

    total = len(copy_jobs) + len(asset_jobs)
    done = skipped
    grand_total = total + skipped
    copied = 0
    decrypted = 0
    was_cancelled = False

    for source_path, target in copy_jobs:
        if cancelled():
            was_cancelled = True
            break
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
            copied += 1
        except OSError as exc:
            errors.append(f"{relpath(source_path, game_root)}: copy failed: {exc}")
        done += 1
        progress(done, max(grand_total, 1))

    if asset_jobs and not was_cancelled:
        max_workers = options.workers or min(32, (os.cpu_count() or 4) + 4)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    decrypt_asset_job, job, key, True, False, False, options.strict, True
                )
                for job in asset_jobs
            ]
            for future in as_completed(futures):
                if cancelled():
                    was_cancelled = True
                    for pending in futures:
                        pending.cancel()
                    break
                result = future.result()
                if result.status == "ok":
                    decrypted += 1
                else:
                    errors.append(f"{relpath(result.source, game_root)}: {result.message}")
                done += 1
                progress(done, max(grand_total, 1))

    system_patched = False
    package_patched = False
    marker_created: str | None = None
    if not was_cancelled:
        system_patched, system_warnings = patch_system_json_for_editing(output)
        warnings.extend(system_warnings)
        package_patched = fix_package_json_main(output)
        marker_created = ensure_project_marker(output, detection.engine)
        if marker_created:
            log(f"Created project file: {marker_created}")
    else:
        warnings.append("Cancelled: the project folder is incomplete.")

    manifest = {
        "mode": "project",
        "engine": detection.engine,
        "detection_confidence": detection.confidence,
        "source_root": str(game_root),
        "content_root": str(content_root),
        "output": str(output),
        "build_time": datetime.now(timezone.utc).isoformat(),
        "files_copied": copied,
        "files_decrypted": decrypted,
        "files_skipped": skipped,
        "key_detected": key is not None,
        "encryption_flags_cleared": system_patched,
        "package_json_fixed": package_patched,
        "project_file_created": marker_created,
        "runtime_files_included": options.include_runtime,
        "cancelled": was_cancelled,
        "warnings": warnings,
        "errors": errors,
    }
    atomic_write_json(output / "project_manifest.json", manifest)

    log(f"Copied: {copied}")
    log(f"Decrypted: {decrypted}")
    if skipped:
        log(f"Skipped (already present): {skipped}")
    if system_patched:
        log("System.json: encryption flags cleared.")
    for item in warnings:
        log(f"Warning: {item}")
    for item in errors:
        log(f"Error: {item}")

    return ProjectBuildResult(
        output=output,
        engine=detection.engine,
        manifest=manifest,
        files_copied=copied,
        files_decrypted=decrypted,
        files_skipped=skipped,
        cancelled=was_cancelled,
        warnings=warnings,
        errors=errors,
    )


def print_key_candidates(candidates: list[KeyCandidate], root: Path) -> None:
    del root
    if not candidates:
        print("No encryption keys found.")
        return
    for index, candidate in enumerate(candidates, start=1):
        reasons = ", ".join(sorted(candidate.reasons))
        sources = "; ".join(candidate.sources[:5])
        if len(candidate.sources) > 5:
            sources += f"; +{len(candidate.sources) - 5} more"
        prefix = "raw:" if candidate.key_format == "raw" else ""
        print(f"{index}. {prefix}{candidate.key}  format={candidate.key_format}  score={candidate.score}")
        print(f"   reasons: {reasons}")
        print(f"   sources: {sources}")


def find_keys_including_game_root(root: Path, max_text_mb: int = 25) -> list[KeyCandidate]:
    """Search ``root`` for keys, then the game root above it if that found none.

    Picking a subfolder such as ``www/img`` puts System.json and the plugins —
    where keys live — outside the search path.
    """

    root = normalize_path(root)
    candidates = find_keys(root, max_text_mb=max_text_mb)
    if candidates:
        return candidates
    game_root = find_game_root_upwards(root)
    if game_root is None or game_root == root:
        return candidates
    return find_keys(game_root, max_text_mb=max_text_mb)


def resolve_key_arg(key_arg: str, root: Path, quiet: bool) -> bytes | None:
    if key_arg.lower() not in {"auto", ""}:
        return key_to_bytes(key_arg)

    candidates = find_keys_including_game_root(root)
    if not candidates:
        return None

    selected = candidates[0]
    if not quiet:
        prefix = "raw:" if selected.key_format == "raw" else ""
        eprint(f"Using key {prefix}{selected.key} from {selected.sources[0]}")
        if len(candidates) > 1:
            eprint(f"Found {len(candidates)} key candidates; highest score was selected.")
    return key_candidate_to_bytes(selected)


def parse_kinds(raw: str) -> set[str]:
    raw = raw.lower()
    if raw == "all":
        return {"image", "audio", "video"}
    kinds = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = kinds - {"image", "audio", "video"}
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown asset kind(s): {', '.join(sorted(unknown))}")
    return kinds


def command_scan(args: argparse.Namespace) -> int:
    root = normalize_path(args.path)
    if not root.exists():
        eprint(f"Path does not exist: {root}")
        return 2

    asset_counts: Counter[str] = Counter()
    header_counts: Counter[str] = Counter()
    text_counts: Counter[str] = Counter()

    for path in iter_files(root):
        suffix = path.suffix.lower()
        if suffix in ASSET_EXTENSIONS:
            target_ext, kind = ASSET_EXTENSIONS[suffix]
            asset_counts[f"{kind}:{suffix}->{target_ext}"] += 1
            header = detect_rpg_header(read_prefix(path, HEADER_LENGTH))
            if header:
                header_counts[header] += 1
        if suffix in TEXT_EXTENSIONS:
            text_counts[suffix] += 1

    print(f"Root: {root}")
    print(f"Text files: {sum(text_counts.values())}")
    for suffix, count in sorted(text_counts.items()):
        print(f"  {suffix}: {count}")

    print(f"Encrypted/renamed asset candidates: {sum(asset_counts.values())}")
    for name, count in sorted(asset_counts.items()):
        print(f"  {name}: {count}")

    if header_counts:
        print("Detected RPG Maker headers:")
        for name, count in sorted(header_counts.items()):
            print(f"  {name}: {count}")

    candidates = find_keys(root, max_text_mb=args.max_text_mb)
    print(f"Key candidates: {len(candidates)}")
    print_key_candidates(candidates, root)
    return 0


def command_keys(args: argparse.Namespace) -> int:
    root = normalize_path(args.path)
    candidates = find_keys(
        root,
        max_text_mb=args.max_text_mb,
        validate_assets=not args.no_asset_validation,
        deep=args.deep,
    )
    print_key_candidates(candidates, root)
    return 0 if candidates else 1


def command_decrypt(args: argparse.Namespace) -> int:
    input_path = normalize_path(args.path)
    if not input_path.exists():
        eprint(f"Path does not exist: {input_path}")
        return 2

    kinds = parse_kinds(args.types)
    preserve_structure = not args.flat
    if args.flat and args.out is None:
        eprint("--flat needs an output folder; add -o/--out.")
        return 2
    jobs = collect_asset_jobs(input_path, args.out, kinds, preserve_structure)
    if not jobs:
        eprint("No matching encrypted/renamed assets found.")
        return 1

    key = resolve_key_arg(args.key, input_path, args.quiet)
    has_files_that_need_key = True
    if key is None:
        has_files_that_need_key = any(
            detect_rpg_header(read_prefix(job.source, HEADER_LENGTH))
            for job in jobs[: min(len(jobs), 50)]
        )
    if key is None and has_files_that_need_key:
        eprint("No key was provided and no key was found. Use --key <32-hex-key>.")
        return 2

    if args.dry_run:
        print(f"Would process {len(jobs)} asset(s).")
        for job in jobs[: args.preview]:
            print(f"{job.source} -> {job.output}")
        if len(jobs) > args.preview:
            print(f"... and {len(jobs) - args.preview} more")
        return 0

    max_workers = args.workers or min(32, (os.cpu_count() or 4) + 4)
    results: list[AssetResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                decrypt_asset_job,
                job,
                key,
                args.overwrite,
                False,
                args.force_xor,
                args.strict,
                args.preserve_time,
            )
            for job in jobs
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if args.verbose or result.status == "error":
                src = relpath(result.source, input_path if input_path.is_dir() else input_path.parent)
                out = result.output
                message = f" ({result.message})" if result.message else ""
                print(f"{result.status}: {src} -> {out}{message}")

    by_status = Counter(result.status for result in results)
    print(
        "Done: "
        + ", ".join(f"{status}={count}" for status, count in sorted(by_status.items()))
    )
    warnings = [result for result in results if result.message.startswith("warning")]
    if warnings and not args.verbose:
        print(f"Warnings: {len(warnings)} decoded file(s) had unexpected signatures.")
    return 1 if by_status.get("error") else 0


def command_strings(args: argparse.Namespace) -> int:
    root = normalize_path(args.path)
    if not root.exists():
        eprint(f"Path does not exist: {root}")
        return 2
    records = iter_text_records(
        root,
        include_js=args.include_js,
        include_all=args.all,
        include_comments=args.comments,
        try_base64_decode=args.try_base64,
        max_text_mb=args.max_text_mb,
    )
    count = write_text_records(records, args.out, args.format)
    if args.out:
        print(f"Exported {count} string(s) to {args.out}")
    return 0


def command_decode_string(args: argparse.Namespace) -> int:
    decoded, decoders = decode_text_layers(args.text, try_base64_decode=args.try_base64)
    print(decoded)
    if args.show_decoders:
        eprint("decoders:", ", ".join(decoders) if decoders else "none")
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    path = normalize_path(args.path)
    result = detect_engine(path, args.engine)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Engine: {result.engine}")
        print(f"Confidence: {result.confidence:.2f}")
        if result.edition:
            print(f"Edition: {result.edition}")
        if result.root:
            print(f"Root: {result.root}")
        print("Evidence:")
        for item in result.evidence:
            print(f"  - {item}")
        if result.warnings:
            print("Warnings:")
            for item in result.warnings:
                print(f"  - {item}")
    return 0 if result.confidence >= 0.35 else 1


def command_extract(args: argparse.Namespace) -> int:
    source = normalize_path(args.path)
    if not source.exists():
        eprint(f"Path does not exist: {source}")
        return 2

    all_selected = args.all or not (args.images or args.text or args.resources)
    output = args.out
    if output is None:
        output = DEFAULT_OUTPUT_PARENT / default_output_folder_name(source)

    options = ExtractionOptions(
        source=source,
        output=output,
        engine=args.engine,
        images=args.images or all_selected,
        text=args.text or all_selected,
        resources=args.resources or all_selected,
        include_comments=args.comments,
        show_keys=args.show_keys,
        overwrite=args.overwrite,
        strict=not args.no_strict,
        workers=args.workers,
        preserve_structure=not args.flat,
        key=args.key,
        uberwolf_cli=args.uberwolf_cli,
        wolftl_cli=args.wolftl_cli,
    )

    try:
        result = run_unified_extraction(options)
    except ValueError as exc:
        eprint(f"Error: {exc}")
        return 2

    print(f"Engine: {result.engine}")
    print(f"Output: {result.output}")
    print(f"Images extracted: {result.manifest.get('images_extracted', 0)}")
    print(f"Text entries extracted: {result.manifest.get('text_entries_extracted', 0)}")
    print(f"Archives processed: {result.manifest.get('archives_processed', 0)}")
    print(f"Encryption key detected: {'yes' if result.manifest.get('key_detected') else 'no'}")
    print(
        "Protection key detected: "
        + ("yes" if result.manifest.get("protection_key_detected") else "no")
    )
    if result.warnings:
        print("Warnings:")
        for item in result.warnings:
            print(f"  - {item}")
    if result.errors:
        print("Errors:")
        for item in result.errors:
            print(f"  - {item}")
    return 1 if result.errors else 0


def command_project(args: argparse.Namespace) -> int:
    source = normalize_path(args.path)
    if not source.exists():
        eprint(f"Path does not exist: {source}")
        return 2

    output = args.out
    if output is None:
        output = DEFAULT_OUTPUT_PARENT / default_output_folder_name(source, "_project")

    options = ProjectBuildOptions(
        source=source,
        output=output,
        key=args.key,
        engine=args.engine,
        overwrite=args.overwrite,
        include_runtime=args.include_runtime,
        strict=not args.no_strict,
        workers=args.workers,
    )
    try:
        result = build_editable_project(options, log=print)
    except ValueError as exc:
        eprint(f"Error: {exc}")
        return 2

    print(f"Engine: {result.engine}")
    print(f"Project: {result.output}")
    print(f"Files copied: {result.files_copied}")
    print(f"Files decrypted: {result.files_decrypted}")
    return 1 if result.errors else 0


def command_patch(args: argparse.Namespace) -> int:
    detection = detect_engine(args.game, args.engine)
    if detection.confidence < 0.35:
        eprint("; ".join(detection.warnings) or "Unable to detect engine.")
        return 2
    adapter = get_engine_adapter(detection.engine)
    result = adapter.apply_translation(args)
    if result.ok:
        print(f"Patched game: {result.output}")
        return 0
    for item in result.conflicts:
        print(f"Conflict: {item}")
    for item in result.warnings:
        print(f"Warning: {item}")
    for item in result.errors:
        print(f"Error: {item}")
    return 1


def command_gui(args: argparse.Namespace) -> int:
    del args
    try:
        from rpg_maker_gui import main as gui_main
    except ModuleNotFoundError as exc:
        if exc.name == "PyQt6":
            eprint("PyQt6 is not installed. Install it with: pip install PyQt6")
            return 2
        raise
    return gui_main()


def encrypt_for_self_test(clear: bytes, key: bytes, header: bytes) -> bytes:
    return header + xor_first_16_bytes(clear, key)


def command_self_test(args: argparse.Namespace) -> int:
    del args
    key_hex = "00112233445566778899aabbccddeeff"
    key = bytes.fromhex(key_hex)
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"fake-png-body"
    ogg = b"OggS" + b"\x00" * 12 + b"fake-ogg-body"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "Game"
        (root / "www" / "data").mkdir(parents=True)
        (root / "www" / "img" / "pictures").mkdir(parents=True)
        (root / "www" / "audio" / "bgm").mkdir(parents=True)

        system = {
            "gameTitle": "Test",
            "hasEncryptedImages": True,
            "hasEncryptedAudio": True,
            "encryptionKey": key_hex,
        }
        (root / "www" / "data" / "System.json").write_text(
            json.dumps(system), encoding="utf-8"
        )
        (root / "www" / "img" / "pictures" / "A.rpgmvp").write_bytes(
            encrypt_for_self_test(png, key, MV_HEADER)
        )
        (root / "www" / "audio" / "bgm" / "B.ogg_").write_bytes(
            encrypt_for_self_test(ogg, key, MZ_HEADER)
        )

        candidates = find_keys(root)
        if not candidates or candidates[0].key != key_hex:
            raise AssertionError("key discovery failed")

        jobs = collect_asset_jobs(root, root / "out", {"image", "audio", "video"})
        for job in jobs:
            result = decrypt_asset_job(
                job,
                key,
                overwrite=True,
                dry_run=False,
                force_xor=False,
                strict=True,
                preserve_time=False,
            )
            if result.status != "ok":
                raise AssertionError(f"decrypt failed: {result}")

        if (root / "out" / "www" / "img" / "pictures" / "A.png").read_bytes() != png:
            raise AssertionError("PNG output mismatch")
        if (root / "out" / "www" / "audio" / "bgm" / "B.ogg").read_bytes() != ogg:
            raise AssertionError("OGG output mismatch")

        # Same project, flat layout: no folders, and the same-named picture in a
        # second folder must survive under a disambiguated name.
        (root / "www" / "img" / "characters").mkdir(parents=True)
        (root / "www" / "img" / "characters" / "A.rpgmvp").write_bytes(
            encrypt_for_self_test(png + b"-second", key, MV_HEADER)
        )
        flat_out = root / "flat-out"
        flat_jobs = collect_asset_jobs(
            root, flat_out, {"image", "audio", "video"}, preserve_structure=False
        )
        for job in flat_jobs:
            result = decrypt_asset_job(
                job,
                key,
                overwrite=True,
                dry_run=False,
                force_xor=False,
                strict=True,
                preserve_time=False,
            )
            if result.status != "ok":
                raise AssertionError(f"flat decrypt failed: {result}")
        flat_files = sorted(path.name for path in flat_out.rglob("*") if path.is_file())
        if flat_files != ["A.png", "B.ogg", "pictures_A.png"]:
            raise AssertionError(f"unexpected flat output: {flat_files}")
        if any(path.is_dir() for path in flat_out.iterdir()):
            raise AssertionError("flat output must not create subfolders")

        # A subfolder deep inside the game is still named after the game.
        (root / "www" / "js").mkdir(parents=True, exist_ok=True)
        (root / "www" / "js" / "rpg_core.js").write_text("// core\n", encoding="utf-8")
        if default_output_folder_name(root / "www" / "img" / "pictures") != "Test":
            raise AssertionError("output name for a subfolder ignored the game title")
        if default_output_folder_name(root / "www" / "img", "_project") != "Test_project":
            raise AssertionError("project suffix was not applied")

        # Project mode: www is flattened, assets decrypted, flags cleared.
        project_out = Path(tmp) / "Test_project"
        build = build_editable_project(
            ProjectBuildOptions(source=root / "www" / "img", output=project_out, overwrite=True)
        )
        if not (project_out / "data" / "System.json").is_file():
            raise AssertionError("project build did not flatten www/")
        if (project_out / "img" / "pictures" / "A.png").read_bytes() != png:
            raise AssertionError("project build did not decrypt images")
        if not (project_out / "Game.rpgproject").is_file():
            raise AssertionError("project build did not create a project file")
        rebuilt_system = json.loads((project_out / "data" / "System.json").read_text(encoding="utf-8"))
        if rebuilt_system.get("hasEncryptedImages") or "encryptionKey" in rebuilt_system:
            raise AssertionError("project build left the encryption flags in place")
        if build.files_decrypted < 2:
            raise AssertionError(f"project build decrypted too little: {build.files_decrypted}")

        masked_root = Path(tmp) / "MaskedGame"
        raw_key_text = "showDefault:eval"
        raw_key = raw_key_text.encode("utf-8")
        (masked_root / "www" / "data").mkdir(parents=True)
        (masked_root / "www" / "js" / "plugins").mkdir(parents=True)
        (masked_root / "www" / "img" / "pictures").mkdir(parents=True)
        (masked_root / "www" / "data" / "System.json").write_text(
            json.dumps({"gameTitle": "Masked Test", "hasEncryptedImages": True}),
            encoding="utf-8",
        )
        (masked_root / "www" / "js" / "plugins" / "MaskPlugin.js").write_text(
            "var p = {\"showDefault\":\"eval\"};\n"
            "Decrypter._maskedKey = 'showDefault' + ':' + 'eval';\n",
            encoding="utf-8",
        )
        (masked_root / "www" / "img" / "pictures" / "Masked.rpgmvp").write_bytes(
            encrypt_for_self_test(png, raw_key, MV_HEADER)
        )

        masked_candidates = find_keys(masked_root)
        if not masked_candidates:
            raise AssertionError("masked key discovery failed")
        best_masked = masked_candidates[0]
        if key_candidate_to_bytes(best_masked) != raw_key:
            raise AssertionError(f"wrong masked key candidate: {best_masked}")

        decoded, decoders = decode_text_layers(r"\u041f\u0440\u0438\u0432\u0435\u0442")
        if decoded != "Привет" or "backslash" not in decoders:
            raise AssertionError("string decoder failed")

    print("self-test OK")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RPG Maker MV/MZ helper for encrypted assets and game text.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan a game/project folder")
    scan.add_argument("path", type=Path)
    scan.add_argument("--max-text-mb", type=int, default=25)
    scan.set_defaults(func=command_scan)

    keys = subparsers.add_parser("keys", help="find RPG Maker encryption keys")
    keys.add_argument("path", type=Path)
    keys.add_argument("--max-text-mb", type=int, default=25)
    keys.add_argument(
        "--no-asset-validation",
        action="store_true",
        help="skip checking candidates against encrypted asset signatures",
    )
    keys.add_argument(
        "--deep",
        action="store_true",
        help="scan every supported text file instead of only likely key/plugin files",
    )
    keys.set_defaults(func=command_keys)

    inspect = subparsers.add_parser("inspect", help="detect game engine and show evidence")
    inspect.add_argument("path", type=Path)
    inspect.add_argument(
        "--engine",
        choices=sorted(SUPPORTED_ENGINES),
        default=ENGINE_AUTO,
        help="manual engine override",
    )
    inspect.add_argument("--json", action="store_true", help="print machine-readable result")
    inspect.set_defaults(func=command_inspect)

    extract = subparsers.add_parser("extract", help="unified extraction for RPG Maker and WOLF RPG")
    extract.add_argument("path", type=Path)
    extract.add_argument("-o", "--out", type=Path)
    extract.add_argument(
        "--engine",
        choices=sorted(SUPPORTED_ENGINES),
        default=ENGINE_AUTO,
        help="manual engine override",
    )
    extract.add_argument("--all", action="store_true", help="extract resources, images, and text")
    extract.add_argument("--resources", action="store_true", help="unpack/decrypt resources")
    extract.add_argument("--images", action="store_true", help="extract/copy image resources only")
    extract.add_argument("--text", action="store_true", help="extract translatable text")
    extract.add_argument(
        "--key",
        default="auto",
        help="32-hex encryption key, or 'auto' to search project files (default)",
    )
    extract.add_argument("--comments", action="store_true", help="include developer comments where supported")
    extract.add_argument("--show-keys", action="store_true", help="allow backend logs to contain detected keys")
    extract.add_argument("--overwrite", action="store_true")
    extract.add_argument("--no-strict", action="store_true")
    extract.add_argument(
        "--flat",
        action="store_true",
        help="do not keep the game's folder structure; collect files into one folder per kind",
    )
    extract.add_argument("--workers", type=int, default=0)
    extract.add_argument("--uberwolf-cli", type=Path, help="path to UberWolfCli executable")
    extract.add_argument("--wolftl-cli", type=Path, help="path to WolfTL executable")
    extract.set_defaults(func=command_extract)

    project = subparsers.add_parser(
        "project", help="rebuild a deployed MV/MZ game as an editable project folder"
    )
    project.add_argument("path", type=Path, help="game folder, or any folder inside it")
    project.add_argument("-o", "--out", type=Path, help="project folder to create")
    project.add_argument(
        "--engine",
        choices=sorted(SUPPORTED_ENGINES),
        default=ENGINE_AUTO,
        help="manual engine override",
    )
    project.add_argument(
        "--key",
        default="auto",
        help="32-hex encryption key, or 'auto' to search project files (default)",
    )
    project.add_argument("--overwrite", action="store_true", help="replace files already in the project folder")
    project.add_argument(
        "--include-runtime",
        action="store_true",
        help="also copy the nw.js runtime (Game.exe, *.dll, locales/)",
    )
    project.add_argument("--no-strict", action="store_true")
    project.add_argument("--workers", type=int, default=0)
    project.set_defaults(func=command_project)

    patch = subparsers.add_parser("patch", help="apply translation through the selected engine backend")
    patch.add_argument("translation", type=Path)
    patch.add_argument("--game", type=Path, required=True)
    patch.add_argument("-o", "--output", type=Path, default=Path("patched-output"))
    patch.add_argument(
        "--engine",
        choices=sorted(SUPPORTED_ENGINES),
        default=ENGINE_AUTO,
        help="manual engine override",
    )
    patch.add_argument("--wolftl-cli", type=Path, help="path to WolfTL executable")
    patch.add_argument("--overwrite", action="store_true")
    patch.set_defaults(func=command_patch)

    decrypt = subparsers.add_parser("decrypt", help="decrypt/copy encrypted assets in bulk")
    decrypt.add_argument("path", type=Path, help="game root, project root, or one asset")
    decrypt.add_argument("-o", "--out", type=Path, help="output folder; default writes next to files")
    decrypt.add_argument(
        "--key",
        default="auto",
        help="32-hex encryption key, or 'auto' to search project files (default)",
    )
    decrypt.add_argument(
        "--types",
        default="image",
        help="asset kinds: image, audio, video, comma-separated, or all (default: image)",
    )
    decrypt.add_argument(
        "--flat",
        action="store_true",
        help="write every decrypted asset directly into --out instead of mirroring folders",
    )
    decrypt.add_argument("--workers", type=int, default=0, help="parallel workers")
    decrypt.add_argument("--overwrite", action="store_true", help="replace existing output files")
    decrypt.add_argument("--dry-run", action="store_true", help="show planned outputs only")
    decrypt.add_argument("--preview", type=int, default=25, help="dry-run preview limit")
    decrypt.add_argument(
        "--force-xor",
        action="store_true",
        help="try headerless XOR decoding for non-standard files",
    )
    decrypt.add_argument(
        "--strict",
        action="store_true",
        help="fail if decoded file signature does not match target extension",
    )
    decrypt.add_argument(
        "--preserve-time",
        action="store_true",
        help="copy source timestamps to decrypted outputs",
    )
    decrypt.add_argument("-v", "--verbose", action="store_true")
    decrypt.add_argument("-q", "--quiet", action="store_true")
    decrypt.set_defaults(func=command_decrypt)

    strings = subparsers.add_parser("strings", help="export/decode text strings from game files")
    strings.add_argument("path", type=Path)
    strings.add_argument("-o", "--out", type=Path)
    strings.add_argument("--format", choices=("jsonl", "csv", "txt"), default="jsonl")
    strings.add_argument("--include-js", action="store_true", help="also scan JavaScript strings")
    strings.add_argument("--all", action="store_true", help="export all strings, including IDs/paths")
    strings.add_argument("--comments", action="store_true", help="include RPG Maker event comments")
    strings.add_argument("--try-base64", action="store_true", help="try safe base64 text decoding")
    strings.add_argument("--max-text-mb", type=int, default=25)
    strings.set_defaults(func=command_strings)

    decode_string = subparsers.add_parser("decode-string", help="decode one string")
    decode_string.add_argument("text")
    decode_string.add_argument("--try-base64", action="store_true")
    decode_string.add_argument("--show-decoders", action="store_true")
    decode_string.set_defaults(func=command_decode_string)

    gui = subparsers.add_parser("gui", help="open the PyQt6 graphical interface")
    gui.set_defaults(func=command_gui)

    self_test = subparsers.add_parser("self-test", help="run built-in sanity checks")
    self_test.set_defaults(func=command_self_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130
    except ValueError as exc:
        eprint(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
