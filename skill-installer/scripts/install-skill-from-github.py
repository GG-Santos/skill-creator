#!/usr/bin/env python3
"""Install one or more validated Codex skills from a GitHub repository."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import zipfile

import yaml

from github_utils import ResponseTooLargeError, github_request


DEFAULT_REF = "main"
DEFAULT_MAX_DOWNLOAD_MIB = 100
DEFAULT_MAX_ENTRIES = 5_000
# Backward-compatible import for callers that used the old internal constant.
DEFAULT_MAX_FILES = DEFAULT_MAX_ENTRIES
DEFAULT_MAX_UNPACKED_MIB = 250
DEFAULT_MAX_COMPRESSION_RATIO = 1_000.0
MAX_SKILL_MD_BYTES = 2 * 1024 * 1024
MAX_SKILL_NAME_LENGTH = 64
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<yaml>.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL
)
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
FORBIDDEN_TREE_DIRS = {".git", ".hg", ".svn", "__pycache__"}
FORBIDDEN_TREE_DIR_KEYS = {name.casefold() for name in FORBIDDEN_TREE_DIRS}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass
class Args:
    url: str | None = None
    repo: str | None = None
    path: list[str] | None = None
    ref: str = DEFAULT_REF
    dest: str | None = None
    expected_name: str | None = None
    method: str = "auto"
    archive_sha256: str | None = None
    max_download_mib: int = DEFAULT_MAX_DOWNLOAD_MIB
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_unpacked_mib: int = DEFAULT_MAX_UNPACKED_MIB
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO


@dataclass
class Source:
    owner: str
    repo: str
    ref: str
    paths: list[str]
    repo_url: str | None = None


@dataclass(frozen=True)
class Limits:
    max_download_bytes: int
    max_entries: int
    max_unpacked_bytes: int
    max_compression_ratio: float


@dataclass(frozen=True)
class PreparedRepo:
    root: str
    method: str
    revision: str


class InstallError(Exception):
    pass


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))


def _tmp_root() -> str:
    base = os.path.join(tempfile.gettempdir(), "codex")
    os.makedirs(base, exist_ok=True)
    return base


def _limits(args: Args) -> Limits:
    if args.max_download_mib <= 0:
        raise InstallError("--max-download-mib must be positive.")
    if args.max_entries <= 0:
        raise InstallError("--max-entries must be positive.")
    if args.max_unpacked_mib <= 0:
        raise InstallError("--max-unpacked-mib must be positive.")
    if not math.isfinite(args.max_compression_ratio) or args.max_compression_ratio <= 0:
        raise InstallError("--max-compression-ratio must be a positive finite number.")
    return Limits(
        max_download_bytes=args.max_download_mib * 1024 * 1024,
        max_entries=args.max_entries,
        max_unpacked_bytes=args.max_unpacked_mib * 1024 * 1024,
        max_compression_ratio=args.max_compression_ratio,
    )


def _request(url: str, max_bytes: int) -> bytes:
    try:
        return github_request(url, "codex-skill-install", max_bytes=max_bytes)
    except ResponseTooLargeError as exc:
        raise InstallError(f"Download rejected: {exc}") from exc


def _parse_github_url(url: str, default_ref: str) -> tuple[str, str, str, str | None]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise InstallError("Only HTTPS github.com URLs are supported for download mode.")
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise InstallError("Invalid GitHub URL.")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    ref = default_ref
    subpath = ""
    if len(parts) > 2:
        if parts[2] in ("tree", "blob"):
            if len(parts) < 4:
                raise InstallError("GitHub URL is missing a ref or path.")
            ref = parts[3]
            subpath = "/".join(parts[4:])
        else:
            subpath = "/".join(parts[2:])
    _validate_repo_identity(owner, repo)
    return owner, repo, ref, subpath or None


def _validate_repo_identity(owner: str, repo: str) -> None:
    allowed = re.compile(r"^[A-Za-z0-9_.-]+$")
    if (
        owner in {".", ".."}
        or repo in {".", ".."}
        or not allowed.fullmatch(owner)
        or not allowed.fullmatch(repo)
    ):
        raise InstallError("Invalid GitHub owner or repository name.")


def _validate_portable_component(part: str, full_path: str) -> None:
    base_name = part.split(".", 1)[0].upper()
    if (
        not part
        or len(part) > 255
        or unicodedata.normalize("NFC", part) != part
        or any(ord(char) < 32 or ord(char) == 127 for char in part)
        or part.rstrip(" .") != part
        or any(char in part for char in '<>:"|?*\\')
        or base_name in WINDOWS_RESERVED_NAMES
    ):
        raise InstallError(f"Path is not portable across Codex hosts: {full_path!r}")


def _portable_path_key(path: PurePosixPath) -> str:
    """Return a host-independent key for collision checks."""
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _safe_archive_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise InstallError(f"Unsafe archive member path: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise InstallError(f"Unsafe archive member path: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise InstallError(f"Unsafe archive member path: {name!r}")
    for part in pure.parts:
        _validate_portable_component(part, name)
    return pure


def _validated_zip_entries(
    zip_file: zipfile.ZipFile, limits: Limits
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    infos = zip_file.infolist()
    if not infos:
        raise InstallError("Downloaded archive was empty.")

    entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    canonical_names: set[str] = set()
    file_names: set[str] = set()
    entry_count = 0
    total_unpacked = 0

    if not math.isfinite(limits.max_compression_ratio) or limits.max_compression_ratio <= 0:
        raise InstallError("The compression-ratio limit must be a positive finite number.")

    for info in infos:
        pure = _safe_archive_name(info.filename)
        canonical = _portable_path_key(pure)
        if canonical in canonical_names:
            raise InstallError(f"Archive contains a duplicate or colliding path: {info.filename}")
        canonical_names.add(canonical)
        entry_count += 1
        if entry_count > limits.max_entries:
            raise InstallError(
                f"Archive contains more than {limits.max_entries} entries."
            )

        if info.flag_bits & 0x1:
            raise InstallError(f"Encrypted archive entries are not supported: {info.filename}")
        file_type = stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)
        if file_type == stat.S_IFLNK:
            raise InstallError(f"Symbolic links are not allowed: {info.filename}")
        if info.is_dir():
            if file_type not in (0, stat.S_IFDIR):
                raise InstallError(f"Unsupported archive entry type: {info.filename}")
        else:
            if file_type not in (0, stat.S_IFREG):
                raise InstallError(f"Unsupported archive entry type: {info.filename}")
            total_unpacked += info.file_size
            if total_unpacked > limits.max_unpacked_bytes:
                raise InstallError("Archive exceeds the unpacked-size limit.")
            if info.compress_size == 0:
                if info.file_size > 0:
                    raise InstallError(f"Suspicious compression ratio: {info.filename}")
            elif info.file_size / info.compress_size > limits.max_compression_ratio:
                raise InstallError(f"Suspicious compression ratio: {info.filename}")
            file_names.add(canonical)
        entries.append((info, pure))

    for file_name in file_names:
        parts = file_name.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in file_names:
                raise InstallError("Archive contains a file/directory path collision.")

    roots = {
        unicodedata.normalize("NFC", pure.parts[0]).casefold() for _, pure in entries
    }
    if len(roots) != 1:
        raise InstallError("Downloaded archive must contain one top-level directory.")
    return entries


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: str, limits: Limits) -> str:
    entries = _validated_zip_entries(zip_file, limits)
    destination = Path(dest_dir).resolve()
    root_name = entries[0][1].parts[0]

    for info, pure in entries:
        target = destination.joinpath(*pure.parts).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise InstallError("Archive contains files outside the destination.") from exc
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zip_file.open(info, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        except FileExistsError as exc:
            raise InstallError(f"Archive path collision: {info.filename}") from exc
        permissions = (info.external_attr >> 16) & 0o777
        if permissions:
            target.chmod(permissions)
    return str(destination / root_name)


def _download_repo_zip(
    owner: str,
    repo: str,
    ref: str,
    dest_dir: str,
    limits: Limits,
    expected_sha256: str | None,
) -> PreparedRepo:
    encoded_ref = urllib.parse.quote(ref, safe="")
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/{encoded_ref}"
    try:
        payload = _request(zip_url, limits.max_download_bytes)
    except urllib.error.HTTPError as exc:
        raise InstallError(f"Download failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"Download failed: {exc.reason}") from exc

    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if expected_sha256 and not hmac.compare_digest(
        actual_sha256, expected_sha256.lower()
    ):
        raise InstallError(
            f"Archive SHA-256 mismatch: expected {expected_sha256.lower()}, got {actual_sha256}."
        )

    zip_path = os.path.join(dest_dir, "repo.zip")
    with open(zip_path, "wb") as file_handle:
        file_handle.write(payload)
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            repo_root = _safe_extract_zip(zip_file, dest_dir, limits)
    except zipfile.BadZipFile as exc:
        raise InstallError("Downloaded response is not a valid ZIP archive.") from exc
    return PreparedRepo(repo_root, "download", actual_sha256)


def _run_git(args: list[str], *, capture: bool = False) -> str:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise InstallError("Git is not installed or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallError("Git command timed out after 300 seconds.") from exc
    if result.returncode != 0:
        raise InstallError(result.stderr.strip() or "Git command failed.")
    return result.stdout.strip() if capture else ""


def _git_sparse_checkout(
    repo_url: str, ref: str, paths: list[str], dest_dir: str
) -> PreparedRepo:
    repo_dir = os.path.join(dest_dir, "repo")

    def clear_failed_checkout() -> None:
        if not os.path.lexists(repo_dir):
            return
        lexical_parent = os.path.abspath(dest_dir)
        lexical_repo = os.path.abspath(repo_dir)
        if os.path.commonpath([lexical_parent, lexical_repo]) != lexical_parent:
            raise InstallError("Refusing to clean an unexpected Git clone path.")
        try:
            if os.path.islink(repo_dir) or not os.path.isdir(repo_dir):
                os.unlink(repo_dir)
            else:
                shutil.rmtree(repo_dir)
        except OSError as exc:
            raise InstallError(f"Could not clean failed Git checkout: {exc}") from exc

    clear_failed_checkout()
    os.makedirs(dest_dir, exist_ok=True)
    os.mkdir(repo_dir)
    try:
        _run_git(["git", "-C", repo_dir, "init", "--quiet"])
        _run_git(["git", "-C", repo_dir, "remote", "add", "origin", repo_url])
        _run_git(["git", "-C", repo_dir, "sparse-checkout", "init", "--no-cone"])
        _run_git(["git", "-C", repo_dir, "sparse-checkout", "set", "--", *paths])
        _run_git(
            [
                "git",
                "-C",
                repo_dir,
                "fetch",
                "--depth",
                "1",
                "--filter=blob:none",
                "origin",
                ref,
            ]
        )
        _run_git(["git", "-C", repo_dir, "checkout", "--detach", "FETCH_HEAD"])
        revision = _run_git(
            ["git", "-C", repo_dir, "rev-parse", "HEAD"], capture=True
        )
    except InstallError as exc:
        try:
            clear_failed_checkout()
        except InstallError as cleanup_error:
            raise InstallError(f"{exc} Cleanup also failed: {cleanup_error}") from exc
        raise
    return PreparedRepo(repo_dir, "git", revision)


def _normalize_relative_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise InstallError("Skill path must be a non-empty relative path.")
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise InstallError("Skill path must be relative to the repository.")
    pure = PurePosixPath(normalized)
    if not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise InstallError("Skill path must stay inside the repository.")
    for part in pure.parts:
        _validate_portable_component(part, normalized)
    return pure.as_posix()


def _validate_skill_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or len(name) > MAX_SKILL_NAME_LENGTH
        or not NAME_RE.fullmatch(name)
    ):
        raise InstallError(
            "Skill name must be 1-64 lowercase letters, digits, and single hyphens."
        )


def _read_skill_frontmatter(skill_md: str) -> dict:
    try:
        size = os.path.getsize(skill_md)
    except OSError as exc:
        raise InstallError(f"Could not inspect SKILL.md: {exc}") from exc
    if size > MAX_SKILL_MD_BYTES:
        raise InstallError("SKILL.md exceeds the 2 MiB validation limit.")
    try:
        content = Path(skill_md).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError("SKILL.md must be valid UTF-8.") from exc
    match = FRONTMATTER_RE.match(content)
    if not match:
        raise InstallError("SKILL.md must begin with YAML frontmatter.")
    try:
        data = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise InstallError(f"SKILL.md has invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError("SKILL.md frontmatter must be a mapping.")
    if not content[match.end() :].strip():
        raise InstallError("SKILL.md must contain instructions after frontmatter.")
    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not name.strip():
        raise InstallError("SKILL.md frontmatter name must be a non-empty string.")
    if not isinstance(description, str) or not description.strip():
        raise InstallError("SKILL.md frontmatter description must be a non-empty string.")
    data["name"] = name.strip()
    return data


def _is_link_like(path: str) -> bool:
    if os.path.islink(path):
        return True
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _reject_symlink_components(path: str, repo_root: str) -> None:
    lexical_root = os.path.abspath(repo_root)
    lexical_path = os.path.abspath(path)
    try:
        inside_repo = os.path.commonpath([lexical_root, lexical_path]) == lexical_root
    except ValueError:
        inside_repo = False
    if not inside_repo:
        raise InstallError("Skill path must be inside the repository.")

    relative = os.path.relpath(lexical_path, lexical_root)
    candidates = [lexical_root]
    cursor = lexical_root
    if relative != os.curdir:
        for component in Path(relative).parts:
            cursor = os.path.join(cursor, component)
            candidates.append(cursor)
    for candidate in candidates:
        if _is_link_like(candidate):
            display = os.path.relpath(candidate, lexical_root)
            if display == os.curdir:
                display = "repository root"
            raise InstallError(f"Symbolic links or junctions are not allowed: {display}")


def _validate_skill(
    path: str,
    repo_root: str,
    expected_name: str,
    limits: Limits,
) -> None:
    _reject_symlink_components(path, repo_root)
    resolved_repo_root = os.path.realpath(repo_root)
    resolved_path = os.path.realpath(path)
    try:
        inside_repo = os.path.commonpath([resolved_repo_root, resolved_path]) == resolved_repo_root
    except ValueError:
        inside_repo = False
    if not inside_repo:
        raise InstallError("Skill path must be inside the repository.")
    if not os.path.isdir(path):
        raise InstallError(f"Skill path not found: {path}")

    entry_count = 0
    total_bytes = 0
    canonical_entries: set[str] = set()

    def register_entry(relative: str) -> None:
        nonlocal entry_count
        portable = PurePosixPath(relative.replace(os.sep, "/"))
        for component in portable.parts:
            _validate_portable_component(component, portable.as_posix())
        key = _portable_path_key(portable)
        if key in canonical_entries:
            raise InstallError(f"Skill tree contains a colliding path: {relative}")
        canonical_entries.add(key)
        entry_count += 1
        if entry_count > limits.max_entries:
            raise InstallError(
                f"Skill contains more than {limits.max_entries} entries."
            )

    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        for directory in directories:
            entry = os.path.join(root, directory)
            relative = os.path.relpath(entry, path)
            if _is_link_like(entry):
                raise InstallError(f"Symbolic links or junctions are not allowed: {relative}")
            if directory.casefold() in FORBIDDEN_TREE_DIR_KEYS:
                raise InstallError(f"Generated or VCS directory is not installable: {directory}")
            register_entry(relative)
        for filename in files:
            entry = os.path.join(root, filename)
            relative = os.path.relpath(entry, path)
            if _is_link_like(entry):
                raise InstallError(f"Symbolic links or junctions are not allowed: {relative}")
            if not os.path.isfile(entry):
                raise InstallError(f"Unsupported file type in skill: {relative}")
            register_entry(relative)
            try:
                total_bytes += os.path.getsize(entry)
            except OSError as exc:
                raise InstallError(f"Could not inspect skill file {relative}: {exc}") from exc
            if total_bytes > limits.max_unpacked_bytes:
                raise InstallError("Skill exceeds the unpacked-size limit.")

    skill_md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(skill_md):
        raise InstallError("SKILL.md not found in selected skill directory.")
    frontmatter = _read_skill_frontmatter(skill_md)
    declared_name = frontmatter["name"]
    _validate_skill_name(declared_name)
    source_name = os.path.basename(os.path.normpath(path))
    if declared_name != source_name:
        raise InstallError(
            f"Skill folder '{source_name}' does not match frontmatter name '{declared_name}'."
        )
    if expected_name != declared_name:
        raise InstallError(
            f"Expected name '{expected_name}' must match frontmatter name '{declared_name}'."
        )


def _copy_skills_transactionally(plans: list[tuple[str, str]]) -> None:
    staged: list[tuple[str, str]] = []
    committed: list[tuple[str, tuple[int, int]]] = []
    try:
        for source, destination in plans:
            parent = os.path.dirname(destination)
            os.makedirs(parent, exist_ok=True)
            if os.path.lexists(destination):
                raise InstallError(f"Destination already exists: {destination}")
            skill_name = os.path.basename(destination)
            staging = tempfile.mkdtemp(prefix=f".{skill_name}-install-", dir=parent)
            staged.append((staging, destination))
            shutil.copytree(source, staging, dirs_exist_ok=True, symlinks=False)

        for staging, destination in staged:
            try:
                os.mkdir(destination)
            except FileExistsError as exc:
                raise InstallError(
                    f"Destination appeared during installation: {destination}"
                ) from exc
            try:
                metadata = os.stat(destination, follow_symlinks=False)
            except OSError as metadata_error:
                try:
                    os.rmdir(destination)
                except OSError as cleanup_error:
                    raise InstallError(
                        "Could not inspect the reserved destination and rollback was "
                        f"incomplete for {destination}: {cleanup_error}"
                    ) from metadata_error
                raise
            committed.append((destination, (metadata.st_dev, metadata.st_ino)))
            shutil.copytree(staging, destination, dirs_exist_ok=True, symlinks=False)
            shutil.copystat(staging, destination, follow_symlinks=False)
    except (InstallError, OSError, shutil.Error) as exc:
        rollback_errors: list[str] = []
        for destination, identity in reversed(committed):
            try:
                metadata = os.stat(destination, follow_symlinks=False)
                current_identity = (metadata.st_dev, metadata.st_ino)
                if not stat.S_ISDIR(metadata.st_mode) or current_identity != identity:
                    rollback_errors.append(
                        f"refused to remove changed destination {destination}"
                    )
                    continue
                shutil.rmtree(destination)
            except FileNotFoundError:
                continue
            except OSError as rollback_error:
                rollback_errors.append(f"{destination}: {rollback_error}")

        message = str(exc) if isinstance(exc, InstallError) else f"Installation failed: {exc}"
        if rollback_errors:
            message += " Rollback incomplete: " + "; ".join(rollback_errors)
        raise InstallError(message) from exc
    finally:
        for staging, _ in staged:
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)


def _copy_skills_atomically(plans: list[tuple[str, str]]) -> None:
    """Compatibility wrapper for the former private helper name."""
    _copy_skills_transactionally(plans)


def _build_repo_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def _build_repo_ssh(owner: str, repo: str) -> str:
    return f"git@github.com:{owner}/{repo}.git"


def _should_try_ssh(error: InstallError) -> bool:
    message = str(error).casefold()
    authentication_markers = (
        "authentication failed",
        "could not read username",
        "permission denied",
        "publickey",
        "repository not found",
        "terminal prompts disabled",
        "http 401",
        "http 403",
        "access denied",
    )
    return any(marker in message for marker in authentication_markers)


def _transport_error(
    *,
    download_error: InstallError | None,
    https_error: InstallError,
    ssh_error: InstallError | None = None,
) -> InstallError:
    details: list[str] = []
    if download_error is not None:
        details.append(f"download: {download_error}")
    details.append(f"Git HTTPS: {https_error}")
    if ssh_error is not None:
        details.append(f"Git SSH: {ssh_error}")
    return InstallError("Repository preparation failed (" + "; ".join(details) + ").")


def _prepare_repo(source: Source, args: Args, limits: Limits, tmp_dir: str) -> PreparedRepo:
    download_error: InstallError | None = None
    if args.method in ("download", "auto"):
        try:
            return _download_repo_zip(
                source.owner,
                source.repo,
                source.ref,
                tmp_dir,
                limits,
                args.archive_sha256,
            )
        except InstallError as exc:
            if args.method == "download" or args.archive_sha256:
                raise
            download_error = exc
            message = str(exc)
            if not any(code in message for code in ("HTTP 401", "HTTP 403", "HTTP 404")):
                raise
    if args.method in ("git", "auto"):
        https_url = _build_repo_url(source.owner, source.repo)
        repo_url = source.repo_url or https_url
        try:
            return _git_sparse_checkout(repo_url, source.ref, source.paths, tmp_dir)
        except InstallError as https_error:
            if repo_url != https_url or not _should_try_ssh(https_error):
                if download_error is not None:
                    raise _transport_error(
                        download_error=download_error, https_error=https_error
                    ) from https_error
                raise
            try:
                return _git_sparse_checkout(
                    _build_repo_ssh(source.owner, source.repo),
                    source.ref,
                    source.paths,
                    tmp_dir,
                )
            except InstallError as ssh_error:
                raise _transport_error(
                    download_error=download_error,
                    https_error=https_error,
                    ssh_error=ssh_error,
                ) from ssh_error
    raise InstallError("Unsupported installation method.")


def _resolve_source(args: Args) -> Source:
    if args.url:
        owner, repo, ref, url_path = _parse_github_url(args.url, args.ref)
        if args.path is not None:
            paths = list(args.path)
        elif url_path:
            paths = [url_path]
        else:
            paths = []
        if not paths:
            raise InstallError("Missing --path for GitHub URL.")
        return Source(owner=owner, repo=repo, ref=ref, paths=paths)

    if not args.repo:
        raise InstallError("Provide --repo or --url.")
    if "://" in args.repo:
        return _resolve_source(
            Args(url=args.repo, repo=None, path=args.path, ref=args.ref)
        )
    repo_parts = [part for part in args.repo.split("/") if part]
    if len(repo_parts) != 2:
        raise InstallError("--repo must use owner/repo format.")
    _validate_repo_identity(repo_parts[0], repo_parts[1])
    if not args.path:
        raise InstallError("Missing --path for --repo.")
    return Source(
        owner=repo_parts[0],
        repo=repo_parts[1],
        ref=args.ref,
        paths=list(args.path),
    )


def _default_dest() -> str:
    return os.path.join(_codex_home(), "skills")


def _parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description="Install validated Codex skills from GitHub.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--repo", help="GitHub owner/repo")
    source_group.add_argument("--url", help="https://github.com/owner/repo[/tree/ref/path]")
    parser.add_argument("--path", nargs="+", help="Path(s) to skills inside the repository")
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--dest", help="Destination skills directory")
    parser.add_argument(
        "--expect-name",
        "--name",
        dest="expected_name",
        help=(
            "Expected name for one skill; validates the folder and frontmatter without "
            "renaming it (--name is a compatibility alias)"
        ),
    )
    parser.add_argument(
        "--method", choices=["auto", "download", "git"], default="auto"
    )
    parser.add_argument(
        "--archive-sha256",
        help="Expected codeload ZIP SHA-256; requires --method download",
    )
    parser.add_argument("--max-download-mib", type=int, default=DEFAULT_MAX_DOWNLOAD_MIB)
    parser.add_argument(
        "--max-entries",
        "--max-files",
        dest="max_entries",
        type=int,
        default=DEFAULT_MAX_ENTRIES,
        help="Maximum archive or selected-tree entries (--max-files is a compatibility alias)",
    )
    parser.add_argument("--max-unpacked-mib", type=int, default=DEFAULT_MAX_UNPACKED_MIB)
    parser.add_argument(
        "--max-compression-ratio", type=float, default=DEFAULT_MAX_COMPRESSION_RATIO
    )
    return parser.parse_args(argv, namespace=Args())


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        if args.archive_sha256:
            if not SHA256_RE.fullmatch(args.archive_sha256):
                raise InstallError("--archive-sha256 must contain exactly 64 hexadecimal characters.")
            if args.method != "download":
                raise InstallError("--archive-sha256 requires --method download; git fallback is disabled.")
        limits = _limits(args)
        source = _resolve_source(args)
        source.ref = source.ref or args.ref
        source.paths = [_normalize_relative_path(path) for path in source.paths]
        if args.expected_name and len(source.paths) != 1:
            raise InstallError("--expect-name can be used only when installing one skill.")

        dest_root = os.path.abspath(args.dest or _default_dest())
        tmp_dir = tempfile.mkdtemp(prefix="skill-install-", dir=_tmp_root())
        try:
            prepared = _prepare_repo(source, args, limits, tmp_dir)
            plans: list[tuple[str, str]] = []
            destination_keys: set[str] = set()
            installed: list[tuple[str, str]] = []
            for relative_path in source.paths:
                source_parts = PurePosixPath(relative_path).parts
                skill_source = os.path.join(prepared.root, *source_parts)
                derived_name = source_parts[-1]
                skill_name = derived_name
                _validate_skill_name(skill_name)
                destination = os.path.join(dest_root, skill_name)
                destination_key = os.path.normcase(os.path.abspath(destination))
                if destination_key in destination_keys:
                    raise InstallError(f"Duplicate destination requested: {destination}")
                destination_keys.add(destination_key)
                if os.path.lexists(destination):
                    raise InstallError(f"Destination already exists: {destination}")
                _validate_skill(
                    skill_source,
                    prepared.root,
                    args.expected_name or skill_name,
                    limits,
                )
                plans.append((skill_source, destination))
                installed.append((skill_name, destination))

            _copy_skills_transactionally(plans)
        finally:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

        print(
            f"Source: {source.owner}/{source.repo}@{source.ref} via {prepared.method} "
            f"({prepared.revision})"
        )
        for skill_name, destination in installed:
            print(f"Installed {skill_name} to {destination}")
        return 0
    except InstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
