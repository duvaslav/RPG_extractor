#!/usr/bin/env python3
"""Unity asset extraction helpers for the RPG Maker/WOLF/Unity GUI."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from output_structure import FlatNameAllocator, structure_mode

ENGINE_UNITY = "Unity"

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LEVEL_FILE_RE = re.compile(r"^level\d+$", re.IGNORECASE)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_UNITY_EXTENSIONS = {
    ".assets",
    ".bundle",
    ".unity3d",
    ".assetbundle",
    ".ab",
    ".apk",
    ".xapk",
    ".aab",
    ".obb",
}

_LOOSE_TEXT_EXTENSIONS = {
    ".txt",
    ".json",
    ".csv",
    ".tsv",
    ".xml",
    ".yaml",
    ".yml",
    ".ini",
    ".config",
    ".bytes",
}


@dataclass(slots=True)
class UnityDetection:
    engine: str = ENGINE_UNITY
    confidence: float = 0.0
    edition: str | None = None
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UnityExtractionOptions:
    source: Path
    output: Path
    images: bool = True
    text: bool = True
    audio: bool = False
    fonts: bool = True
    overwrite: bool = False
    verbose: bool = False
    fallback_unity_version: str | None = None
    preserve_structure: bool = True
    copy_loose_text: bool = True
    max_loose_text_mb: int = 50


@dataclass(slots=True)
class UnityExtractionResult:
    output: Path
    manifest: dict[str, Any]


def safe_name(value: Any, fallback: str = "unnamed") -> str:
    name = str(value or "").strip()
    name = _INVALID_FILENAME_CHARS.sub("_", name).rstrip(". ")
    if not name:
        name = fallback
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name[:180]


def detect_unity_input(path: Path) -> UnityDetection:
    path = path.expanduser()
    evidence: list[str] = []
    warnings: list[str] = []
    score = 0.0
    edition: str | None = None

    if not path.exists():
        return UnityDetection(
            confidence=0.0,
            warnings=[f"Путь не существует: {path}"],
        )

    if path.is_file():
        suffix = path.suffix.lower()
        lower_name = path.name.lower()
        if suffix in {".apk", ".xapk", ".aab", ".obb"}:
            score = 0.95
            edition = "Android"
            evidence.append(f"Контейнер мобильной сборки: {path.name}")
        elif suffix in _UNITY_EXTENSIONS or lower_name.startswith("cab-"):
            score = 0.92
            edition = "Serialized file / AssetBundle"
            evidence.append(f"Файл Unity-ассетов: {path.name}")
        elif lower_name in {"globalgamemanagers", "globalgamemanagers.assets"}:
            score = 0.95
            evidence.append("Найден globalgamemanagers")
        else:
            warnings.append("Расширение файла не является типичным для Unity.")
        return UnityDetection(confidence=score, edition=edition, evidence=evidence, warnings=warnings)

    data_dirs = list(path.glob("*_Data"))
    if path.name.lower().endswith("_data"):
        data_dirs.insert(0, path)

    if data_dirs:
        score += 0.45
        evidence.append(f"Найдена папка Unity Data: {data_dirs[0].name}")
        edition = "Desktop"

    unity_player = list(path.glob("UnityPlayer.dll"))
    if unity_player:
        score += 0.25
        evidence.append("Найден UnityPlayer.dll")
        edition = edition or "Windows"

    game_assembly = list(path.glob("GameAssembly.dll"))
    if game_assembly:
        score += 0.15
        evidence.append("Найден GameAssembly.dll (IL2CPP)")
        edition = edition or "Windows IL2CPP"

    roots = [path, *data_dirs[:3]]
    marker_names = {"globalgamemanagers", "resources.assets"}
    for root in roots:
        for marker in marker_names:
            if (root / marker).exists():
                score += 0.2
                evidence.append(f"Найден {marker}")
                break
        if list(root.glob("sharedassets*.assets")):
            score += 0.15
            evidence.append("Найдены sharedassets*.assets")
            break

    if (path / "assets" / "bin" / "Data").exists():
        score += 0.55
        edition = "Android extracted"
        evidence.append("Найдена Android-структура assets/bin/Data")

    if any((root / "StreamingAssets").exists() for root in roots):
        score += 0.08
        evidence.append("Найдена папка StreamingAssets")

    return UnityDetection(
        confidence=min(score, 1.0),
        edition=edition,
        evidence=list(dict.fromkeys(evidence)),
        warnings=warnings,
    )


def _is_unity_source_file(path: Path) -> bool:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in _UNITY_EXTENSIONS:
        return True
    if name in {"globalgamemanagers", "globalgamemanagers.assets"}:
        return True
    if name.startswith("cab-"):
        return True
    if _LEVEL_FILE_RE.fullmatch(name):
        return True
    return False


def collect_unity_sources(source: Path, output: Path | None = None) -> list[Path]:
    source = source.resolve()
    if source.is_file():
        return [source]

    output_resolved = output.resolve() if output is not None and output.exists() else output
    candidates: list[Path] = []

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if output_resolved is not None:
            try:
                path.resolve().relative_to(output_resolved)
                continue
            except (ValueError, OSError):
                pass
        if _is_unity_source_file(path):
            candidates.append(path)

    priority_names = {
        "globalgamemanagers": 0,
        "globalgamemanagers.assets": 0,
        "resources.assets": 1,
    }

    def sort_key(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        if name in priority_names:
            priority = priority_names[name]
        elif name.startswith("sharedassets"):
            priority = 2
        elif path.suffix.lower() in {".bundle", ".unity3d", ".assetbundle", ".ab"}:
            priority = 3
        else:
            priority = 4
        return priority, str(path).lower()

    return sorted(dict.fromkeys(candidates), key=sort_key)


def _asset_file_name(obj: Any) -> str:
    assets_file = getattr(obj, "assets_file", None)
    for attribute in ("name", "file_name"):
        value = getattr(assets_file, attribute, None)
        if value:
            return Path(str(value)).name
    return "unknown_asset"


def _object_name(obj: Any, parsed: Any | None = None) -> str:
    if parsed is not None:
        name = getattr(parsed, "m_Name", None)
        if name:
            return str(name)
    try:
        name = obj.peek_name()
        if name:
            return str(name)
    except Exception:
        pass
    return f"{obj.type.name}_{obj.path_id}"


def _source_bucket(source: Path, obj: Any) -> str:
    source_hash = hashlib.sha1(str(source).encode("utf-8", "surrogateescape")).hexdigest()[:8]
    asset_name = safe_name(Path(_asset_file_name(obj)).stem, "asset")
    return f"{asset_name}__{source_hash}"


def _write_bytes(path: Path, data: bytes, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def _write_text(path: Path, text: str, overwrite: bool) -> bool:
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__type__": "bytes", "hex": value.hex()}
    if isinstance(value, bytearray):
        return {"__type__": "bytearray", "hex": bytes(value).hex()}
    if hasattr(value, "path_id"):
        return {"path_id": getattr(value, "path_id", None)}
    return str(value)


def _decode_text(data: bytes) -> tuple[str | None, str | None]:
    attempts: list[tuple[str, str]] = []
    if data.startswith(b"\xef\xbb\xbf"):
        attempts.append(("utf-8-sig", "utf-8-sig"))
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or data[:200].count(b"\x00") > 10:
        attempts.append(("utf-16", "utf-16"))
    attempts.extend(
        [
            ("utf-8", "utf-8"),
            ("utf-8-sig", "utf-8-sig"),
            ("cp1251", "cp1251"),
        ]
    )
    seen: set[str] = set()
    for codec, label in attempts:
        if codec in seen:
            continue
        seen.add(codec)
        try:
            return data.decode(codec), label
        except UnicodeDecodeError:
            continue
    return None, None


def _is_translation_candidate(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 2 or len(stripped) > 50_000:
        return False
    if not any(character.isalpha() for character in stripped):
        return False
    control_count = sum(ord(character) < 32 and character not in "\r\n\t" for character in stripped)
    return control_count == 0


def _collect_strings(
    value: Any,
    writer: csv.DictWriter,
    *,
    asset_file: str,
    object_type: str,
    object_name: str,
    path_id: int | str,
    field_path: str = "$",
) -> int:
    count = 0
    if isinstance(value, str):
        if _is_translation_candidate(value):
            writer.writerow(
                {
                    "asset_file": asset_file,
                    "object_type": object_type,
                    "object_name": object_name,
                    "path_id": path_id,
                    "field_path": field_path,
                    "text": value,
                }
            )
            return 1
        return 0

    if isinstance(value, dict):
        for key, child in value.items():
            key_string = str(key).replace('"', '\\"')
            count += _collect_strings(
                child,
                writer,
                asset_file=asset_file,
                object_type=object_type,
                object_name=object_name,
                path_id=path_id,
                field_path=f'{field_path}["{key_string}"]',
            )
        return count

    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            count += _collect_strings(
                child,
                writer,
                asset_file=asset_file,
                object_type=object_type,
                object_name=object_name,
                path_id=path_id,
                field_path=f"{field_path}[{index}]",
            )
    return count


def _collect_text_lines(
    text: str,
    writer: csv.DictWriter,
    *,
    asset_file: str,
    object_type: str,
    object_name: str,
    path_id: int | str,
) -> int:
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        decoded = None

    if decoded is not None:
        return _collect_strings(
            decoded,
            writer,
            asset_file=asset_file,
            object_type=object_type,
            object_name=object_name,
            path_id=path_id,
        )

    count = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _is_translation_candidate(line):
            writer.writerow(
                {
                    "asset_file": asset_file,
                    "object_type": object_type,
                    "object_name": object_name,
                    "path_id": path_id,
                    "field_path": f"line:{line_number}",
                    "text": line,
                }
            )
            count += 1
    return count


def _iter_loose_text_files(source: Path, output: Path, max_bytes: int) -> Iterable[Path]:
    if not source.is_dir():
        return []
    files: list[Path] = []
    output_resolved = output.resolve() if output.exists() else output
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _LOOSE_TEXT_EXTENSIONS:
            continue
        try:
            path.resolve().relative_to(output_resolved)
            continue
        except (ValueError, OSError):
            pass
        try:
            if path.stat().st_size <= max_bytes:
                files.append(path)
        except OSError:
            continue
    return files


def extract_unity(
    options: UnityExtractionOptions,
    *,
    log: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> UnityExtractionResult:
    log = log or (lambda _message: None)
    progress = progress or (lambda _done, _total: None)
    cancelled = cancelled or (lambda: False)

    try:
        import UnityPy
        import UnityPy.config
    except ImportError as exc:
        raise RuntimeError(
            "Для Unity-режима установите зависимости: python -m pip install -U UnityPy Pillow"
        ) from exc

    if options.fallback_unity_version:
        UnityPy.config.FALLBACK_UNITY_VERSION = options.fallback_unity_version.strip()

    source = options.source.expanduser().resolve()
    output = options.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    flat_allocators: dict[str, FlatNameAllocator] = {}

    def destination(category: str, relative: Path | str) -> Path:
        """Output path for one asset under ``output/<category>``.

        With the preserved layout the asset keeps its type/archive subfolders.
        In flat mode everything of one category lands in a single folder, with
        names kept unique per category.
        """

        relative_path = Path(relative)
        if options.preserve_structure:
            return output.joinpath(category, relative_path)
        allocator = flat_allocators.setdefault(category, FlatNameAllocator())
        return output.joinpath(category, allocator.allocate(relative_path))

    typetree_generator = None
    generator_warning: str | None = None
    if options.text and options.fallback_unity_version and source.is_dir():
        try:
            from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator

            game_root = source.parent if source.name.lower().endswith("_data") else source
            typetree_generator = TypeTreeGenerator(options.fallback_unity_version.strip())
            typetree_generator.load_local_game(str(game_root))
            log(f"TypeTree generator loaded from: {game_root}")
        except Exception as exc:
            generator_warning = (
                "TypeTreeGenerator недоступен; часть MonoBehaviour может не декодироваться. "
                "Для расширенной поддержки установите TypeTreeGeneratorAPI. "
                f"Причина: {type(exc).__name__}: {exc}"
            )
            log(f"Warning: {generator_warning}")

    sources = collect_unity_sources(source, output)
    loose_text_files = (
        list(_iter_loose_text_files(source, output, options.max_loose_text_mb * 1024 * 1024))
        if options.text and options.copy_loose_text and source.is_dir()
        else []
    )

    if not sources and not loose_text_files:
        raise RuntimeError("Unity-файлы или подходящие текстовые ресурсы не найдены.")

    manifest: dict[str, Any] = {
        "engine": ENGINE_UNITY,
        "edition": detect_unity_input(source).edition,
        "detection_confidence": detect_unity_input(source).confidence,
        "source": str(source),
        "output": str(output),
        "unitypy_version": getattr(UnityPy, "__version__", "unknown"),
        "folder_structure": structure_mode(options.preserve_structure),
        "archives_processed": 0,
        "files_skipped": 0,
        "images_extracted": 0,
        "texture2d_extracted": 0,
        "sprites_extracted": 0,
        "texture_arrays_extracted": 0,
        "text_assets_extracted": 0,
        "monobehaviours_extracted": 0,
        "loose_text_files_copied": 0,
        "text_entries_extracted": 0,
        "audio_extracted": 0,
        "fonts_extracted": 0,
        "errors": [],
        "warnings": [],
    }
    if generator_warning:
        manifest["warnings"].append(generator_warning)

    csv_path = output / "translation_strings.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    total_steps = len(sources) + len(loose_text_files)
    done_steps = 0

    fieldnames = [
        "asset_file",
        "object_type",
        "object_name",
        "path_id",
        "field_path",
        "text",
    ]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        for source_file in sources:
            if cancelled():
                manifest["warnings"].append("Операция отменена пользователем.")
                break

            log(f"UnityPy: {source_file}")
            try:
                env = UnityPy.load(str(source_file))
                if typetree_generator is not None:
                    env.typetree_generator = typetree_generator
            except Exception as exc:
                message = f"Не удалось открыть {source_file}: {type(exc).__name__}: {exc}"
                manifest["errors"].append(message)
                log(f"ERROR: {message}")
                done_steps += 1
                progress(done_steps, max(total_steps, 1))
                continue

            manifest["archives_processed"] += 1

            try:
                for obj in env.objects:
                    if cancelled():
                        manifest["warnings"].append("Операция отменена пользователем.")
                        break

                    object_type = obj.type.name
                    if object_type not in {
                        "Texture2D",
                        "Sprite",
                        "Texture2DArray",
                        "TextAsset",
                        "MonoBehaviour",
                        "AudioClip",
                        "Font",
                    }:
                        continue

                    bucket = _source_bucket(source_file, obj)
                    try:
                        if options.images and object_type in {"Texture2D", "Sprite"}:
                            parsed = obj.parse_as_object()
                            name = safe_name(_object_name(obj, parsed))
                            target = destination(
                                "images",
                                Path(object_type) / bucket / f"{name}__{obj.path_id}.png",
                            )
                            if target.exists() and not options.overwrite:
                                manifest["files_skipped"] += 1
                            else:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                parsed.image.save(target)
                                manifest["images_extracted"] += 1
                                key = "texture2d_extracted" if object_type == "Texture2D" else "sprites_extracted"
                                manifest[key] += 1

                        elif options.images and object_type == "Texture2DArray":
                            parsed = obj.parse_as_object()
                            name = safe_name(_object_name(obj, parsed))
                            images = getattr(parsed, "images", [])
                            for index, image in enumerate(images):
                                target = destination(
                                    "images",
                                    Path(object_type)
                                    / bucket
                                    / f"{name}__{obj.path_id}__layer_{index}.png",
                                )
                                if target.exists() and not options.overwrite:
                                    manifest["files_skipped"] += 1
                                    continue
                                target.parent.mkdir(parents=True, exist_ok=True)
                                image.save(target)
                                manifest["images_extracted"] += 1
                                manifest["texture_arrays_extracted"] += 1

                        elif options.text and object_type == "TextAsset":
                            parsed = obj.parse_as_object()
                            name = _object_name(obj, parsed)
                            original_suffix = Path(name).suffix
                            if original_suffix and len(original_suffix) <= 12:
                                filename = f"{safe_name(Path(name).stem)}__{obj.path_id}{original_suffix}"
                            else:
                                filename = f"{safe_name(name)}__{obj.path_id}.txt"
                            target = destination("text", Path("TextAsset") / bucket / filename)
                            raw = parsed.m_Script.encode("utf-8", "surrogateescape")
                            if _write_bytes(target, raw, options.overwrite):
                                manifest["text_assets_extracted"] += 1
                            else:
                                manifest["files_skipped"] += 1
                            decoded, _encoding = _decode_text(raw)
                            if decoded is not None:
                                manifest["text_entries_extracted"] += _collect_text_lines(
                                    decoded,
                                    writer,
                                    asset_file=_asset_file_name(obj),
                                    object_type=object_type,
                                    object_name=name,
                                    path_id=obj.path_id,
                                )

                        elif options.text and object_type == "MonoBehaviour":
                            tree = obj.parse_as_dict()
                            name = str(tree.get("m_Name") or _object_name(obj))
                            target = destination(
                                "text",
                                Path("MonoBehaviour") / bucket / f"{safe_name(name)}__{obj.path_id}.json",
                            )
                            json_text = json.dumps(
                                tree,
                                ensure_ascii=False,
                                indent=2,
                                default=_json_default,
                            )
                            if _write_text(target, json_text, options.overwrite):
                                manifest["monobehaviours_extracted"] += 1
                            else:
                                manifest["files_skipped"] += 1
                            manifest["text_entries_extracted"] += _collect_strings(
                                tree,
                                writer,
                                asset_file=_asset_file_name(obj),
                                object_type=object_type,
                                object_name=name,
                                path_id=obj.path_id,
                            )

                        elif options.audio and object_type == "AudioClip":
                            parsed = obj.parse_as_object()
                            clip_name = safe_name(_object_name(obj, parsed))
                            for sample_name, sample_data in parsed.samples.items():
                                sample_path = Path(str(sample_name))
                                extension = sample_path.suffix or ".wav"
                                filename = f"{clip_name}__{safe_name(sample_path.stem)}__{obj.path_id}{extension}"
                                target = destination("audio", Path(bucket) / filename)
                                if _write_bytes(target, bytes(sample_data), options.overwrite):
                                    manifest["audio_extracted"] += 1
                                else:
                                    manifest["files_skipped"] += 1

                        elif options.fonts and object_type == "Font":
                            parsed = obj.parse_as_object()
                            font_data = bytes(getattr(parsed, "m_FontData", b"") or b"")
                            if not font_data:
                                continue
                            extension = ".otf" if font_data.startswith(b"OTTO") else ".ttf"
                            name = safe_name(_object_name(obj, parsed))
                            target = destination(
                                "fonts", Path(bucket) / f"{name}__{obj.path_id}{extension}"
                            )
                            if _write_bytes(target, font_data, options.overwrite):
                                manifest["fonts_extracted"] += 1
                            else:
                                manifest["files_skipped"] += 1

                    except Exception as exc:
                        message = (
                            f"{source_file} | {_asset_file_name(obj)} | {object_type} | "
                            f"path_id={obj.path_id} | {type(exc).__name__}: {exc}"
                        )
                        manifest["errors"].append(message)
                        if options.verbose:
                            log(f"ERROR: {message}")
            finally:
                done_steps += 1
                progress(done_steps, max(total_steps, 1))

        for loose_file in loose_text_files:
            if cancelled():
                manifest["warnings"].append("Операция отменена пользователем.")
                break
            try:
                raw = loose_file.read_bytes()
                relative = loose_file.relative_to(source)
                target = destination("text", Path("LooseFiles") / relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and not options.overwrite:
                    manifest["files_skipped"] += 1
                else:
                    shutil.copy2(loose_file, target)
                    manifest["loose_text_files_copied"] += 1
                decoded, _encoding = _decode_text(raw)
                if decoded is not None:
                    manifest["text_entries_extracted"] += _collect_text_lines(
                        decoded,
                        writer,
                        asset_file=str(relative),
                        object_type="LooseTextFile",
                        object_name=loose_file.name,
                        path_id="",
                    )
            except Exception as exc:
                message = f"Loose text {loose_file}: {type(exc).__name__}: {exc}"
                manifest["errors"].append(message)
                if options.verbose:
                    log(f"ERROR: {message}")
            finally:
                done_steps += 1
                progress(done_steps, max(total_steps, 1))

    manifest_path = output / "unity_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"Unity archives processed: {manifest['archives_processed']}")
    log(f"Images extracted: {manifest['images_extracted']}")
    log(f"Text assets extracted: {manifest['text_assets_extracted']}")
    log(f"MonoBehaviours extracted: {manifest['monobehaviours_extracted']}")
    log(f"Translation rows: {manifest['text_entries_extracted']}")
    log(f"Audio extracted: {manifest['audio_extracted']}")
    log(f"Fonts extracted: {manifest['fonts_extracted']}")
    log(f"Output: {output}")

    return UnityExtractionResult(output=output, manifest=manifest)
