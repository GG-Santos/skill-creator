#!/usr/bin/env python3
"""Create and verify deterministic .skill archives for Codex skills."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import fnmatch
import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any
import unicodedata
from urllib.parse import unquote, urlparse
import zipfile

import yaml

from skill_validation import LOCAL_LINK_RE, _non_fenced_lines, format_text_report, validate_path


MANIFEST_NAME = "PACKAGE-MANIFEST.json"
MANIFEST_SCHEMA_VERSION = 1
MAX_ARCHIVE_FILES = 2_000
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1_000
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
DEFAULT_EXCLUDED_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", "dist"}
DEFAULT_EXCLUDED_FILES = {".DS_Store", "Thumbs.db"}
DEFAULT_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".skill"}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class PackageError(Exception):
    pass


@dataclass(frozen=True)
class PackageResult:
    archive: str
    skill_name: str
    files: int
    bytes: int
    sha256: str
    verified: bool


@dataclass(frozen=True)
class VerificationResult:
    archive: str
    skill_name: str
    files: int
    unpacked_bytes: int
    sha256: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_ignore_patterns(skill_dir: Path) -> list[str]:
    path = skill_dir / ".skillignore"
    if not path.exists():
        return []
    patterns: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        if pattern.startswith("!"):
            raise PackageError("Negated .skillignore patterns are not supported.")
        normalized = pattern.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", normalized):
            raise PackageError(f"Unsafe .skillignore pattern: {pattern}")
        patterns.append(normalized)
    return patterns


def _matches_pattern(relative: str, pattern: str) -> bool:
    directory_pattern = pattern.endswith("/")
    normalized = pattern.rstrip("/")
    if directory_pattern and (relative == normalized or relative.startswith(normalized + "/")):
        return True
    if fnmatch.fnmatchcase(relative, normalized):
        return True
    return any(fnmatch.fnmatchcase(part, normalized) for part in PurePosixPath(relative).parts)


def _default_excluded(relative: PurePosixPath) -> bool:
    if any(part in DEFAULT_EXCLUDED_DIRS for part in relative.parts[:-1]):
        return True
    if relative.name in DEFAULT_EXCLUDED_FILES:
        return True
    return relative.suffix.lower() in DEFAULT_EXCLUDED_SUFFIXES


def _source_files(skill_dir: Path) -> list[Path]:
    patterns = _load_ignore_patterns(skill_dir)
    files: list[Path] = []
    for path in sorted(skill_dir.rglob("*")):
        relative = PurePosixPath(path.relative_to(skill_dir).as_posix())
        if path.is_symlink():
            raise PackageError(f"Symbolic links are not supported: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackageError(f"Unsupported filesystem entry: {relative}")
        _validate_member_name(f"{skill_dir.name}/{relative.as_posix()}")
        if _default_excluded(relative):
            continue
        if relative.as_posix() != ".skillignore" and any(
            _matches_pattern(relative.as_posix(), pattern) for pattern in patterns
        ):
            continue
        files.append(path)
    if not files:
        raise PackageError("No files remain after applying package exclusions.")
    return files


def _clean_local_link_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = re.split(r"\s+[\"']", target, maxsplit=1)[0]
    target = unquote(target.strip())
    if not target or target.startswith("#"):
        return None
    if "{{" in target or "}}" in target:
        return None
    if target.startswith(("/", "//")) or "\\" in target or re.match(r"^[A-Za-z]:", target):
        raise PackageError(f"Packaged Markdown link is not a portable relative path: {raw_target}")
    if urlparse(target).scheme:
        return None
    return target.split("#", 1)[0].split("?", 1)[0] or None


def _validate_archive_markdown_links(
    archive: zipfile.ZipFile,
    skill_name: str,
    actual_files: set[str],
) -> None:
    for relative in sorted(actual_files):
        if PurePosixPath(relative).suffix.lower() != ".md":
            continue
        try:
            text = archive.read(f"{skill_name}/{relative}").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageError(f"Packaged Markdown file is not UTF-8: {relative}") from exc
        for line_number, line in _non_fenced_lines(text):
            without_code = re.sub(r"`[^`]*`", "", line)
            for match in LOCAL_LINK_RE.finditer(without_code):
                raw_target = match.group("target")
                target = _clean_local_link_target(raw_target)
                if target is None:
                    continue
                combined = posixpath.normpath(
                    posixpath.join(PurePosixPath(relative).parent.as_posix(), target)
                )
                if combined == ".." or combined.startswith("../"):
                    raise PackageError(
                        f"Packaged Markdown link escapes the skill root: {relative}:{line_number}"
                    )
                if combined in actual_files:
                    continue
                directory_prefix = combined.rstrip("/") + "/"
                if any(path.startswith(directory_prefix) for path in actual_files):
                    continue
                raise PackageError(
                    f"Packaged Markdown link target is absent: {relative}:{line_number} -> {raw_target}"
                )


def _zip_info(archive_name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = (mode & 0xFFFF) << 16
    info.flag_bits |= 0x800
    return info


def _manifest(skill_name: str, entries: list[dict[str, Any]]) -> bytes:
    data = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "skill_name": skill_name,
        "files": entries,
    }
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def create_package(
    skill_directory: str | Path,
    output: str | Path | None = None,
    *,
    strict: bool = False,
    force: bool = False,
) -> PackageResult:
    skill_dir = Path(skill_directory).resolve()
    report = validate_path(skill_dir, deep=True)
    if not report.passed(strict):
        raise PackageError("Skill validation failed:\n" + format_text_report(report, strict))
    if not report.skill_name:
        raise PackageError("Validated skill has no name.")
    skill_name = report.skill_name

    output_path = (
        Path(output).resolve()
        if output is not None
        else (skill_dir.parent / f"{skill_name}.skill").resolve()
    )
    try:
        output_path.relative_to(skill_dir)
    except ValueError:
        pass
    else:
        raise PackageError("Output archive must be outside the skill directory.")
    if os.path.lexists(output_path) and not force:
        raise PackageError(f"Output already exists: {output_path}. Use --force to replace it.")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    files = _source_files(skill_dir)
    manifest_entries: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes, bool]] = []
    total_bytes = 0
    for path in files:
        relative = path.relative_to(skill_dir).as_posix()
        payload = path.read_bytes()
        total_bytes += len(payload)
        executable = relative.startswith("scripts/") and path.suffix.lower() in {".py", ".sh"}
        payloads.append((relative, payload, executable))
        manifest_entries.append(
            {"path": relative, "sha256": _sha256_bytes(payload), "size": len(payload)}
        )

    temp_handle = tempfile.NamedTemporaryFile(
        prefix=f".{skill_name}-", suffix=".tmp", dir=output_path.parent, delete=False
    )
    temp_path = Path(temp_handle.name)
    temp_handle.close()
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, payload, executable in payloads:
                archive.writestr(
                    _zip_info(f"{skill_name}/{relative}", executable=executable), payload
                )
            archive.writestr(
                _zip_info(f"{skill_name}/{MANIFEST_NAME}"),
                _manifest(skill_name, manifest_entries),
            )
        verification = verify_archive(temp_path)
        if verification.skill_name != skill_name:
            raise PackageError("Archive verification returned the wrong skill name.")
        if os.path.lexists(output_path) and not force:
            raise PackageError(f"Output already exists: {output_path}")
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return PackageResult(
        archive=str(output_path),
        skill_name=skill_name,
        files=len(files),
        bytes=output_path.stat().st_size,
        sha256=_sha256_file(output_path),
        verified=True,
    )


def _validate_member_name(name: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name or not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise PackageError(f"Unsafe archive member path: {name!r}")
    raw_parts = name.split("/")
    if raw_parts[-1] == "":
        raw_parts = raw_parts[:-1]
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise PackageError(f"Unsafe archive member path: {name!r}")
    for part in raw_parts:
        base_name = part.split(".", 1)[0].upper()
        if (
            part.rstrip(" .") != part
            or any(char in part for char in '<>:"|?*')
            or any(ord(char) < 32 or ord(char) == 127 for char in part)
            or unicodedata.normalize("NFC", part) != part
            or base_name in WINDOWS_RESERVED_NAMES
        ):
            raise PackageError(f"Archive member path is not portable: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PackageError(f"Unsafe archive member path: {name!r}")
    return pure


def verify_archive(archive_path: str | Path) -> VerificationResult:
    path = Path(archive_path).resolve()
    if not path.is_file():
        raise PackageError(f"Archive not found: {path}")
    archive_hash = _sha256_file(path)
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        if not infos:
            raise PackageError("Archive is empty.")
        if len(infos) > MAX_ARCHIVE_FILES:
            raise PackageError(f"Archive contains too many entries: {len(infos)}")
        names = [info.filename for info in infos]
        canonical_names = [
            unicodedata.normalize("NFC", _validate_member_name(name).as_posix()).casefold()
            for name in names
        ]
        if len(canonical_names) != len(set(canonical_names)):
            raise PackageError("Archive contains duplicate or case-colliding member names.")
        roots: set[str] = set()
        file_names: set[str] = set()
        total_unpacked = 0
        for info in infos:
            pure = _validate_member_name(info.filename)
            roots.add(pure.parts[0])
            if info.flag_bits & 0x1:
                raise PackageError(f"Encrypted archive entry is not supported: {info.filename}")
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                raise PackageError(f"Symbolic links are not supported: {info.filename}")
            if info.is_dir():
                if file_type not in (0, 0o040000):
                    raise PackageError(f"Unsupported archive entry type: {info.filename}")
            elif file_type not in (0, 0o100000):
                raise PackageError(f"Unsupported archive entry type: {info.filename}")
            total_unpacked += info.file_size
            if total_unpacked > MAX_UNPACKED_BYTES:
                raise PackageError("Archive exceeds the unpacked-size limit.")
            if info.compress_size == 0:
                if info.file_size > 0:
                    raise PackageError(f"Suspicious compression ratio: {info.filename}")
            elif info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise PackageError(f"Suspicious compression ratio: {info.filename}")
            if not info.is_dir():
                file_names.add(pure.as_posix().casefold())
        for file_name in file_names:
            parts = file_name.split("/")
            for index in range(1, len(parts)):
                if "/".join(parts[:index]) in file_names:
                    raise PackageError("Archive contains a file/directory path collision.")
        if len(roots) != 1:
            raise PackageError("Archive must contain exactly one top-level skill directory.")
        skill_name = next(iter(roots))
        required_skill_md = f"{skill_name}/SKILL.md"
        manifest_name = f"{skill_name}/{MANIFEST_NAME}"
        if required_skill_md not in names:
            raise PackageError("Archive does not contain SKILL.md at its skill root.")
        if manifest_name not in names:
            raise PackageError(f"Archive does not contain {MANIFEST_NAME}.")
        if archive.getinfo(manifest_name).file_size > MAX_MANIFEST_BYTES:
            raise PackageError(f"{MANIFEST_NAME} exceeds the size limit.")

        try:
            frontmatter_text = archive.read(required_skill_md).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageError("Packaged SKILL.md is not UTF-8.") from exc
        match = re.match(r"\A---\r?\n(.*?)\r?\n---", frontmatter_text, re.DOTALL)
        if not match:
            raise PackageError("Packaged SKILL.md frontmatter is invalid.")
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise PackageError(f"Packaged SKILL.md YAML is invalid: {exc}") from exc
        if not isinstance(frontmatter, dict) or frontmatter.get("name") != skill_name:
            raise PackageError("Archive root must match packaged frontmatter name.")

        try:
            manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackageError(f"Invalid {MANIFEST_NAME}: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise PackageError(f"Unsupported {MANIFEST_NAME} schema.")
        if manifest.get("skill_name") != skill_name:
            raise PackageError(f"{MANIFEST_NAME} skill_name does not match archive root.")
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise PackageError(f"{MANIFEST_NAME} files must be a list.")
        expected: dict[str, dict[str, Any]] = {}
        expected_canonical: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise PackageError(f"Invalid file entry in {MANIFEST_NAME}.")
            relative = entry["path"]
            canonical = _validate_member_name(relative).as_posix().casefold()
            if canonical in expected_canonical:
                raise PackageError(f"Duplicate manifest path: {relative}")
            expected_canonical.add(canonical)
            expected[relative] = entry
        actual = {
            PurePosixPath(name).relative_to(skill_name).as_posix()
            for name in names
            if name != manifest_name and not name.endswith("/")
        }
        if actual != set(expected):
            missing = sorted(set(expected) - actual)
            extra = sorted(actual - set(expected))
            raise PackageError(f"Manifest/file mismatch; missing={missing}, extra={extra}")
        _validate_archive_markdown_links(archive, skill_name, actual)
        for relative, entry in expected.items():
            payload = archive.read(f"{skill_name}/{relative}")
            if entry.get("size") != len(payload):
                raise PackageError(f"Size mismatch for {relative}")
            if entry.get("sha256") != _sha256_bytes(payload):
                raise PackageError(f"SHA-256 mismatch for {relative}")
    return VerificationResult(
        archive=str(path),
        skill_name=skill_name,
        files=len(expected),
        unpacked_bytes=total_unpacked,
        sha256=archive_hash,
    )


def _print_result(result, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
    else:
        for key, value in asdict(result).items():
            print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a deterministic, verified .skill archive.")
    parser.add_argument("skill_directory")
    parser.add_argument("--output", help="Output .skill path (default: sibling of skill directory).")
    parser.add_argument("--strict", action="store_true", help="Treat validation warnings as errors.")
    parser.add_argument("--force", action="store_true", help="Replace an existing output archive.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)
    try:
        result = create_package(
            args.skill_directory,
            args.output,
            strict=args.strict,
            force=args.force,
        )
    except (OSError, PackageError, zipfile.BadZipFile) as exc:
        if args.json:
            print(json.dumps({"passed": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"Error: {exc}")
        return 1
    _print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
