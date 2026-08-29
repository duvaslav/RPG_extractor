#!/usr/bin/env python3
"""Pixel Game Maker MV (PGMMV) support: detection, resource triage, project text.

PGMMV is not RPG Maker. It ships a Cocos2d-based player, keeps everything under
``Resources/``, describes the whole game in one ``Resources/data/project.json``
(hundreds of MB is normal) and stores its resource-protection metadata in
``Resources/data/info.json``.

Two things here exist because the generic RPG Maker code gets them wrong on a
PGMMV game:

* **Protection is not visible in the file name.** A protected image is still
  called ``.png``; only its header says otherwise. Everything in this module
  classifies by content, never by extension.
* **The project file is too big to load.** :func:`find_json_value` pulls a
  single top-level value (``textList``) out of a multi-hundred-MB JSON file
  without parsing — let alone materializing — the rest of it.

Standard library only.
"""

from __future__ import annotations

import json
import mmap
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

ENGINE_PIXEL_GAME_MAKER_MV = "pixel-game-maker-mv"

# Protected resources start with this marker instead of their format signature.
PGMMV_PROTECTED_MAGIC = b"enc"

LOCALE_CODE_RE = re.compile(r"^[a-z]{2}(?:[_-][A-Za-z]{2,4})?$")
KNOWN_LOCALES = {
    "en_US", "en_GB", "ja_JP", "zh_CN", "zh_TW", "ko_KR", "fr_FR", "de_DE",
    "es_ES", "it_IT", "pt_BR", "ru_RU",
}

PLAYER_VERSION_RE = re.compile(rb"Pixel Game Maker MV[^\x00]{0,64}?(\d+\.\d+\.\d+(?:\.\d+)?)")
PRODUCT_NAME_RE = re.compile(rb"P\x00i\x00x\x00e\x00l\x00 \x00G\x00a\x00m\x00e\x00 \x00M\x00a\x00k\x00e\x00r")
PROJECT_VERSION_RE = re.compile(r'"(?:projectVersion|version|formatVersion)"\s*:\s*"?(\d+\.\d+(?:\.\d+)?)"?')

IMAGE_SIGNATURES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"BM",
    b"GIF87a",
    b"GIF89a",
)
AUDIO_SIGNATURES: tuple[bytes, ...] = (b"OggS", b"RIFF", b"ID3", b"\xff\xfb", b"fLaC")
FONT_SIGNATURES: tuple[bytes, ...] = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf", b"wOFF", b"wOF2")

ASSET_KIND_BY_EXTENSION: dict[str, str] = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".bmp": "image",
    ".gif": "image", ".webp": "image",
    ".ogg": "audio", ".m4a": "audio", ".mp3": "audio", ".wav": "audio", ".flac": "audio",
    ".ttf": "font", ".otf": "font", ".ttc": "font", ".woff": "font", ".woff2": "font",
    ".mp4": "video", ".webm": "video",
}


@dataclass
class PgmmvDetection:
    """What a folder tells us about being a Pixel Game Maker MV game."""

    confidence: float = 0.0
    root: Path | None = None
    resources_root: Path | None = None
    project_json: Path | None = None
    info_json: Path | None = None
    project_version: str | None = None
    player_version: str | None = None
    has_protection_key: bool = False
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def edition(self) -> str | None:
        parts = []
        if self.project_version:
            parts.append(f"project {self.project_version}")
        if self.player_version:
            parts.append(f"player {self.player_version}")
        return ", ".join(parts) or None


@dataclass
class AssetScan:
    """Content-based census of a game's resources."""

    protected: dict[str, int] = field(default_factory=dict)
    plain: dict[str, int] = field(default_factory=dict)
    unknown: int = 0
    total_files: int = 0
    protected_magics: dict[str, int] = field(default_factory=dict)

    @property
    def protected_total(self) -> int:
        return sum(self.protected.values())

    @property
    def plain_total(self) -> int:
        return sum(self.plain.values())


def find_resources_root(root: Path) -> Path | None:
    for candidate in (root / "Resources", root):
        if (candidate / "data" / "project.json").is_file():
            return candidate
    for candidate in root.glob("*/Resources"):
        if (candidate / "data" / "project.json").is_file():
            return candidate
    return None


def _read_prefix(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _scan_executable_for_version(path: Path, max_bytes: int = 8 * 1024 * 1024) -> tuple[bool, str | None]:
    """Look for the player's product name and version inside Game.exe."""

    data = _read_prefix(path, max_bytes)
    if not data:
        return False, None
    match = PLAYER_VERSION_RE.search(data)
    if match:
        return True, match.group(1).decode("ascii", "ignore")
    if b"Pixel Game Maker" in data or PRODUCT_NAME_RE.search(data):
        return True, None
    return False, None


def detect_pixel_game_maker(path: Path) -> PgmmvDetection:
    root = path if path.is_dir() else path.parent
    root = root.expanduser().resolve()
    detection = PgmmvDetection(root=root)

    resources = find_resources_root(root)
    if resources is None:
        return detection

    detection.resources_root = resources
    detection.project_json = resources / "data" / "project.json"
    detection.confidence += 0.6
    size_mb = detection.project_json.stat().st_size / (1024 * 1024)
    detection.evidence.append(f"Resources/data/project.json found ({size_mb:.1f} MB)")

    info_json = resources / "data" / "info.json"
    if info_json.is_file():
        detection.info_json = info_json
        detection.confidence += 0.15
        detection.evidence.append("Resources/data/info.json found")
        detection.has_protection_key = info_json_has_key(info_json)
        if detection.has_protection_key:
            detection.evidence.append("resource protection key metadata present in info.json")

    version = PROJECT_VERSION_RE.search(
        _read_prefix(detection.project_json, 8192).decode("utf-8", "ignore")
    )
    if version:
        detection.project_version = version.group(1)
        detection.evidence.append(f"project format {detection.project_version}")

    for exe_name in ("Game.exe", "game.exe"):
        exe = root / exe_name
        if exe.is_file():
            named, player_version = _scan_executable_for_version(exe)
            if named:
                detection.confidence += 0.25
                detection.player_version = player_version
                detection.evidence.append(
                    "Pixel Game Maker MV player"
                    + (f" {player_version}" if player_version else "")
                    + f" in {exe_name}"
                )
            break

    if (resources / "plugins").is_dir():
        detection.confidence += 0.05
        detection.evidence.append("Resources/plugins found")

    detection.confidence = min(detection.confidence, 0.99)
    if detection.confidence >= 0.35 and not detection.player_version:
        detection.warnings.append(
            "Player version could not be read from Game.exe; project data still identifies PGMMV."
        )
    return detection


def info_json_has_key(info_json: Path) -> bool:
    """True when info.json carries resource-protection metadata.

    The value itself is never returned — callers only ever report its presence.
    """

    try:
        data = json.loads(info_json.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return bool(_find_key_metadata(data))


def _find_key_metadata(node: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("key", "encrypt", "protect", "crypt")):
                if value not in (None, "", False, 0):
                    return True
            if _find_key_metadata(value, depth + 1):
                return True
    elif isinstance(node, list):
        return any(_find_key_metadata(item, depth + 1) for item in node[:64])
    return False


# ---------------------------------------------------------------------------
# Content-based resource triage
# ---------------------------------------------------------------------------


def _matches(data: bytes, signatures: Iterable[bytes]) -> bool:
    return any(data.startswith(signature) for signature in signatures)


def content_kind(data: bytes) -> str | None:
    if _matches(data, IMAGE_SIGNATURES):
        return "image"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image"
    if _matches(data, AUDIO_SIGNATURES):
        return "audio"
    if _matches(data, FONT_SIGNATURES):
        return "font"
    return None


def protected_magic(data: bytes) -> str | None:
    """Marker of a protected resource, or None when the file is readable as-is."""

    if data.startswith(PGMMV_PROTECTED_MAGIC):
        return PGMMV_PROTECTED_MAGIC.decode("ascii")
    return None


def classify_file(path: Path) -> tuple[str, str | None, str | None]:
    """Return (state, kind, magic) for one resource.

    ``state`` is "plain" when the bytes match the format the name promises,
    "protected" when the file carries a protection header or simply does not
    decode as its own format, and "unknown" otherwise.
    """

    expected = ASSET_KIND_BY_EXTENSION.get(path.suffix.lower())
    if expected is None:
        return "other", None, None
    head = _read_prefix(path, 32)
    if not head:
        return "unknown", expected, None
    magic = protected_magic(head)
    if magic is not None:
        return "protected", expected, magic
    actual = content_kind(head)
    if actual is not None:
        return "plain", actual, None
    # Named like a resource, but the header is neither the format nor a known
    # marker: still protected as far as extraction is concerned.
    return "protected", expected, head[:4].hex()


def scan_resources(
    root: Path,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> AssetScan:
    progress = progress or (lambda _done, _total: None)
    cancelled = cancelled or (lambda: False)
    files = [path for path in sorted(root.rglob("*")) if path.is_file()]
    scan = AssetScan(total_files=len(files))
    for index, path in enumerate(files, start=1):
        if cancelled():
            break
        state, kind, magic = classify_file(path)
        if state == "protected" and kind:
            scan.protected[kind] = scan.protected.get(kind, 0) + 1
            if magic:
                scan.protected_magics[magic] = scan.protected_magics.get(magic, 0) + 1
        elif state == "plain" and kind:
            scan.plain[kind] = scan.plain.get(kind, 0) + 1
        elif state == "unknown":
            scan.unknown += 1
        if index % 200 == 0 or index == len(files):
            progress(index, len(files))
    return scan


# ---------------------------------------------------------------------------
# Huge project.json
# ---------------------------------------------------------------------------


def find_json_value(
    path: Path,
    key: str,
    max_value_bytes: int = 256 * 1024 * 1024,
) -> Any | None:
    """Decode the value of one key from a JSON file without loading the file.

    The file is memory-mapped, the key located by byte search, and only the
    value that follows it is decoded — growing the decode window until the value
    fits. This is what makes a 300 MB project.json usable.
    """

    needle = f'"{key}"'.encode("utf-8")
    decoder = json.JSONDecoder()
    try:
        with path.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                offset = data.find(needle)
                while offset != -1:
                    cursor = offset + len(needle)
                    while cursor < len(data) and data[cursor : cursor + 1] in b" \t\r\n":
                        cursor += 1
                    if data[cursor : cursor + 1] != b":":
                        offset = data.find(needle, cursor)
                        continue
                    cursor += 1
                    window = 1 << 20
                    while True:
                        chunk = data[cursor : cursor + window]
                        text = chunk.decode("utf-8", "replace")
                        try:
                            value, _end = decoder.raw_decode(text.lstrip())
                            return value
                        except ValueError:
                            if cursor + window >= len(data) or window >= max_value_bytes:
                                break
                            window = min(window * 4, max_value_bytes)
                    offset = data.find(needle, cursor)
    except (OSError, ValueError):
        return None
    return None


def is_locale_code(value: str) -> bool:
    if value in KNOWN_LOCALES:
        return True
    return bool(LOCALE_CODE_RE.fullmatch(value)) and "_" in value


@dataclass
class LocaleRecord:
    entry_id: str
    locale: str
    text: str
    context: str
    font: str | None = None


def iter_locale_records(node: Any, context: str = "textList") -> Iterator[LocaleRecord]:
    """Yield every localized string in a PGMMV text subtree.

    The layout is matched by shape rather than by a fixed schema: any object
    whose keys look like locale codes is a translation bundle.
    """

    if isinstance(node, dict):
        locale_keys = [key for key in node if isinstance(key, str) and is_locale_code(key)]
        if locale_keys:
            entry_id = str(
                node.get("id")
                or node.get("Id")
                or node.get("name")
                or context.rsplit(".", 1)[-1]
            )
            font = node.get("font") or node.get("fontId")
            for key in locale_keys:
                value = node[key]
                text = _locale_text(value)
                if text is None:
                    continue
                yield LocaleRecord(
                    entry_id=entry_id,
                    locale=key,
                    text=text,
                    context=f"{context}.{key}",
                    font=str(font) if font is not None else None,
                )
        for key, value in node.items():
            if isinstance(key, str) and is_locale_code(key):
                continue
            yield from iter_locale_records(value, f"{context}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from iter_locale_records(item, f"{context}[{index}]")


def _locale_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "value", "string", "str"):
            inner = value.get(key)
            if isinstance(inner, str):
                return inner
    return None


def extract_project_text(
    project_json: Path,
    locales: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[list[LocaleRecord], dict[str, int], list[str]]:
    """Pull localized text out of project.json. Returns (records, per-locale counts, warnings)."""

    log = log or (lambda _message: None)
    warnings: list[str] = []
    size_mb = project_json.stat().st_size / (1024 * 1024)
    log(f"project.json: {size_mb:.1f} MB, читаю только textList")

    subtree = find_json_value(project_json, "textList")
    if subtree is None:
        warnings.append(
            "textList was not found in project.json; no localized text could be exported."
        )
        return [], {}, warnings

    records = [
        record
        for record in iter_locale_records(subtree)
        if locales is None or record.locale in locales
    ]
    counts: dict[str, int] = {}
    for record in records:
        counts[record.locale] = counts.get(record.locale, 0) + 1
    if not records:
        warnings.append("textList was found but held no localized strings.")
    return records, counts, warnings
