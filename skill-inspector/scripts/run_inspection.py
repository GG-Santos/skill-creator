#!/usr/bin/env python3
"""Run NVIDIA SkillSpector safely and normalize its evidence.

The adapter uses only the Python standard library. It never installs the
scanner, never executes files from the target, and always launches the trusted
scanner with ``shell=False`` and an argument vector.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "skill-inspection/v1"
RAW_REPORT_NAME = "skillspector.raw.json"
NORMALIZED_REPORT_NAME = "inspection.v1.json"
STDOUT_NAME = "skillspector.stdout.txt"
STDERR_NAME = "skillspector.stderr.txt"
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES = 4 * 1024 * 1024
ALLOWED_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
ALLOWED_MAX_ISSUE_SEVERITIES = {"NONE", *ALLOWED_SEVERITIES}
ALLOWED_RECOMMENDATIONS = {"SAFE", "CAUTION", "DO_NOT_INSTALL"}
ALLOWED_VERDICTS = {"APPROVE", "CAUTION", "REJECT"}
ALLOWED_DISPOSITIONS = {"explained", "false_positive", "mitigated", "unresolved"}
TREE_HASH_ALGORITHM = "sha256-tree-v2"
FILE_HASH_ALGORITHM = "sha256-file-v2"
PROVIDER_SECRET_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_PROXY_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_API_KEY",
    "NVIDIA_INFERENCE_KEY",
    "OPENAI_API_KEY",
    "SKILLSPECTOR_COMPAT_API_KEY",
}
LLM_PROVIDER_ENV_NAMES = {
    *PROVIDER_SECRET_NAMES,
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROXY_API_VERSION",
    "ANTHROPIC_PROXY_ENDPOINT_URL",
    "AWS_CONFIG_FILE",
    "AWS_DEFAULT_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_ROLE_ARN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_ENDPOINT",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "NVIDIA_INFERENCE_METADATA_KEY",
    "OLLAMA_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT_ID",
    "SKILLSPECTOR_COMPAT_BASE_URL",
    "SKILLSPECTOR_LOG_LEVEL",
    "SKILLSPECTOR_MAX_LLM_CONCURRENCY",
    "SKILLSPECTOR_MAX_WORKFLOW_SECONDS",
    "SKILLSPECTOR_MODEL",
    "SKILLSPECTOR_MODEL_REGISTRY",
    "SKILLSPECTOR_OSV_TIMEOUT",
    "SKILLSPECTOR_OUTPUT_LANGUAGE",
    "SKILLSPECTOR_PROVIDER",
    "SKILLSPECTOR_REASONING_EFFORT",
    "SKILLSPECTOR_SEED",
    "SKILLSPECTOR_SSL_VERIFY",
    "SKILLSPECTOR_STRICT_MODEL_VALIDATION",
    "SKILLSPECTOR_TEMPERATURE",
}
SAFE_STATIC_ENV_NAMES = {
    "APPDATA",
    "COMSPEC",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "CURL_CA_BUNDLE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
}
TRACING_ENV_PREFIXES = ("LANGCHAIN_", "LANGSMITH_", "OTEL_")


class InspectionError(RuntimeError):
    """Expected, user-facing inspection error."""


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _reject_link_components(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and _is_link_like(current):
            raise InspectionError(
                f"Refusing {label} with a symbolic-link or junction component: {current}"
            )


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _hash_file(path: Path, digest: "hashlib._Hash") -> int:
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size


def _hash_entry_header(
    digest: "hashlib._Hash", marker: bytes, relative: bytes, mode: int
) -> None:
    digest.update(marker)
    digest.update(b"\0")
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    digest.update(b"\0")
    digest.update((mode & 0o111).to_bytes(2, "big"))
    digest.update(b"\0")


def hash_target(target: Path) -> dict[str, Any]:
    """Return a stable content digest without following symbolic links."""

    _reject_link_components(target, label="target")
    if _is_link_like(target):
        raise InspectionError(f"Refusing symbolic-link or junction target: {target}")
    resolved = target.resolve(strict=True)
    if resolved.is_file():
        metadata = resolved.stat(follow_symlinks=False)
        digest = hashlib.sha256()
        kind = "archive" if resolved.suffix.lower() in {".zip", ".skill"} else "file"
        if kind == "file":
            digest.update(b"skill-inspector-file-v2\0")
            digest.update((metadata.st_mode & 0o111).to_bytes(2, "big"))
            digest.update(b"\0")
        size = _hash_file(resolved, digest)
        return {
            "kind": kind,
            "algorithm": "sha256" if kind == "archive" else FILE_HASH_ALGORITHM,
            "digest": digest.hexdigest(),
            "entries": 1,
            "bytes": size,
        }
    if not resolved.is_dir():
        raise InspectionError(f"Target is not a regular file or directory: {resolved}")

    digest = hashlib.sha256()
    digest.update(b"skill-inspector-directory-v2\0")
    entries = 0
    total_bytes = 0
    def walk_error(error: OSError) -> None:
        raise InspectionError(f"Could not enumerate target: {error}") from error

    for current, directories, files in os.walk(
        resolved, topdown=True, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        if _is_link_like(current_path):
            raise InspectionError(
                f"Refusing symbolic link or junction inside target: {current_path}"
            )
        directories.sort()
        files.sort()
        for directory_name in list(directories):
            directory_path = current_path / directory_name
            if _is_link_like(directory_path):
                raise InspectionError(
                    f"Refusing symbolic link or junction inside target: {directory_path}"
                )
            relative = directory_path.relative_to(resolved).as_posix().encode("utf-8")
            _hash_entry_header(
                digest,
                b"D",
                relative,
                directory_path.stat(follow_symlinks=False).st_mode,
            )
            entries += 1
        for file_name in files:
            file_path = current_path / file_name
            if _is_link_like(file_path):
                raise InspectionError(
                    f"Refusing symbolic link or junction inside target: {file_path}"
                )
            if not file_path.is_file():
                raise InspectionError(f"Refusing non-regular target entry: {file_path}")
            relative = file_path.relative_to(resolved).as_posix().encode("utf-8")
            _hash_entry_header(
                digest,
                b"F",
                relative,
                file_path.stat(follow_symlinks=False).st_mode,
            )
            total_bytes += _hash_file(file_path, digest)
            digest.update(b"\0")
            entries += 1
    return {
        "kind": "directory",
        "algorithm": TREE_HASH_ALGORITHM,
        "digest": digest.hexdigest(),
        "entries": entries,
        "bytes": total_bytes,
    }


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_scanner_executable(path: Path) -> Path:
    if os.name == "nt" and path.suffix.casefold() in {".bat", ".cmd"}:
        raise InspectionError(
            "Refusing a Windows batch-file scanner because argument quoting is not shell-free"
        )
    return path


def _resolve_scanner(scanner: str) -> Path:
    has_separator = any(separator and separator in scanner for separator in (os.sep, os.altsep))
    supplied_path = Path(scanner).expanduser()
    if supplied_path.is_absolute():
        candidate = supplied_path.resolve(strict=False)
        if not candidate.is_file():
            raise InspectionError(f"Configured SkillSpector executable does not exist: {candidate}")
        return _validate_scanner_executable(candidate)
    if has_separator:
        raise InspectionError(
            "A path-form --scanner value must be absolute; use a bare command name for PATH lookup"
        )
    located = shutil.which(scanner)
    if not located:
        raise InspectionError(
            "NVIDIA SkillSpector is not installed or is not on PATH. "
            "The adapter will not install it automatically."
        )
    resolved = Path(located).resolve(strict=True)
    current_directory = Path.cwd().resolve(strict=True)
    if _is_within(resolved, current_directory):
        raise InspectionError(
            "Refusing a PATH-resolved scanner from the current working directory; "
            "review it and pass its absolute path explicitly"
        )
    return _validate_scanner_executable(resolved)


def _scanner_environment(use_llm: bool) -> tuple[dict[str, str], list[str]]:
    inherited = dict(os.environ)
    removed: list[str] = []
    allowed_names = set(SAFE_STATIC_ENV_NAMES)
    if use_llm:
        allowed_names.update(LLM_PROVIDER_ENV_NAMES)
    environment = {
        name: value
        for name, value in inherited.items()
        if name.upper() in allowed_names
        or (use_llm and name.upper().startswith("SKILLSPECTOR_MODEL_"))
    }
    removed.extend(
        sorted(
            name
            for name in inherited
            if name not in environment and (
                name.upper() in PROVIDER_SECRET_NAMES
                or name.upper().endswith(
                    ("_API_KEY", "_ACCESS_TOKEN", "_AUTH_TOKEN", "_PASSWORD", "_SECRET")
                )
                or name.upper() in {"GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN"}
            )
        )
    )
    for name in list(environment):
        if name.upper().startswith(TRACING_ENV_PREFIXES):
            removed.append(name)
            environment.pop(name, None)
    environment.update(
        {
            "LANGCHAIN_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
            "LANGSMITH_TRACING": "false",
            "OTEL_SDK_DISABLED": "true",
        }
    )
    removed = sorted(set(removed))
    return environment, removed


def _scanner_version(
    command: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: float,
    cwd: Path,
) -> str | None:
    try:
        with tempfile.TemporaryDirectory(prefix=".scanner-version-", dir=cwd) as temporary:
            temporary_path = Path(temporary)
            stdout_path = temporary_path / "stdout"
            stderr_path = temporary_path / "stderr"
            _run_scanner_bounded(
                [*command, "--version"],
                environment=environment,
                cwd=cwd,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                raw_path=temporary_path / "unused-raw-report",
                timeout_seconds=min(timeout_seconds, 15.0),
            )
            stdout = stdout_path.read_bytes().decode("utf-8", errors="replace")
            stderr = stderr_path.read_bytes().decode("utf-8", errors="replace")
    except (InspectionError, OSError, subprocess.SubprocessError):
        return None
    text = (stdout or stderr).strip().splitlines()
    return text[0][:300] if text else None


def _safe_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InspectionError(f"Upstream report field {field!r} must be a non-empty string")
    return value


def _normalize_location(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"file": None, "start_line": None, "end_line": None}
    start_line = value.get("start_line")
    end_line = value.get("end_line")
    return {
        "file": value.get("file") if isinstance(value.get("file"), str) else None,
        "start_line": start_line if isinstance(start_line, int) and start_line >= 0 else None,
        "end_line": end_line if isinstance(end_line, int) and end_line >= 0 else None,
    }


def _normalize_finding(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InspectionError(f"Upstream {field} must be an object")
    rule_id = value.get("id")
    finding_id = value.get("finding_id")
    severity = value.get("severity")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise InspectionError(f"Upstream {field} has no non-empty rule id")
    if not isinstance(finding_id, str) or not finding_id.strip():
        raise InspectionError(f"Upstream {field} has no non-empty finding_id")
    if not isinstance(severity, str) or severity.upper() not in ALLOWED_SEVERITIES:
        raise InspectionError(f"Upstream {field} has an invalid severity")
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= float(confidence) <= 1
    ):
        raise InspectionError(f"Upstream {field} has invalid confidence")
    tags = value.get("tags")
    evidence = value.get("evidence")
    occurrences = value.get("occurrences")
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise InspectionError(f"Upstream {field}.tags must contain strings")
    if not isinstance(evidence, Mapping):
        raise InspectionError(f"Upstream {field}.evidence must be an object")
    if not isinstance(occurrences, list) or any(
        not isinstance(occurrence, Mapping) for occurrence in occurrences
    ):
        raise InspectionError(f"Upstream {field}.occurrences must contain objects")
    normalized = json.loads(json.dumps(value))
    normalized["id"] = rule_id
    normalized["finding_id"] = finding_id
    normalized["severity"] = severity.upper()
    normalized["confidence"] = float(confidence)
    normalized["location"] = _normalize_location(value.get("location"))
    return normalized


def normalize_upstream(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise InspectionError("Upstream JSON report must be an object")
    skill = raw.get("skill")
    risk = raw.get("risk_assessment")
    components = raw.get("components")
    structured_summaries = raw.get("structured_summaries")
    issues = raw.get("issues")
    metadata = raw.get("metadata")
    completeness = raw.get("analysis_completeness")
    suppressed_count = raw.get("suppressed_count")
    suppressed = raw.get("suppressed")
    if not isinstance(skill, Mapping):
        raise InspectionError("Upstream JSON report is missing object field 'skill'")
    if not isinstance(risk, Mapping):
        raise InspectionError("Upstream JSON report is missing object field 'risk_assessment'")
    if not isinstance(components, list) or any(
        not isinstance(component, Mapping) for component in components
    ):
        raise InspectionError("Upstream JSON report field 'components' must be a list of objects")
    if not isinstance(structured_summaries, list):
        raise InspectionError("Upstream JSON report field 'structured_summaries' must be a list")
    if not isinstance(issues, list):
        raise InspectionError("Upstream JSON report is missing list field 'issues'")
    if not isinstance(metadata, Mapping):
        raise InspectionError("Upstream JSON report is missing object field 'metadata'")
    if not isinstance(completeness, Mapping):
        raise InspectionError("Upstream JSON report is missing object field 'analysis_completeness'")
    if (
        isinstance(suppressed_count, bool)
        or not isinstance(suppressed_count, int)
        or suppressed_count < 0
    ):
        raise InspectionError("Upstream suppressed_count must be a non-negative integer")
    if not isinstance(suppressed, list):
        raise InspectionError("Upstream suppressed must be a list")
    if suppressed_count != len(suppressed):
        raise InspectionError("Upstream suppression count does not match suppressed evidence")
    normalized_suppressed = [
        _normalize_finding(item, field=f"suppressed[{index}]")
        for index, item in enumerate(suppressed)
    ]

    score = risk.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise InspectionError("Upstream risk score must be an integer from 0 to 100")
    severity = _safe_string(risk.get("severity"), field="risk_assessment.severity").upper()
    recommendation = _safe_string(
        risk.get("recommendation"), field="risk_assessment.recommendation"
    ).upper()
    max_issue_severity = _safe_string(
        risk.get("max_issue_severity"), field="risk_assessment.max_issue_severity"
    ).upper()
    if severity not in ALLOWED_SEVERITIES:
        raise InspectionError(f"Unsupported upstream severity: {severity}")
    if recommendation not in ALLOWED_RECOMMENDATIONS:
        raise InspectionError(f"Unsupported upstream recommendation: {recommendation}")
    if max_issue_severity not in ALLOWED_MAX_ISSUE_SEVERITIES:
        raise InspectionError(f"Unsupported upstream maximum issue severity: {max_issue_severity}")

    normalized_issues = [
        _normalize_finding(issue, field=f"issues[{index}]")
        for index, issue in enumerate(issues)
    ]
    finding_severities: dict[str, str] = {}
    for issue in normalized_issues:
        previous_severity = finding_severities.get(issue["finding_id"])
        if previous_severity is not None and previous_severity != issue["severity"]:
            raise InspectionError(
                "Occurrence-expanded rows for one finding_id have conflicting severities"
            )
        finding_severities[issue["finding_id"]] = issue["severity"]
    expected_max_severity = "NONE"
    severity_rank = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    for issue in normalized_issues:
        if severity_rank[issue["severity"]] > severity_rank[expected_max_severity]:
            expected_max_severity = issue["severity"]
    if max_issue_severity != expected_max_severity:
        raise InspectionError(
            "Upstream max_issue_severity does not match active issue evidence"
        )
    expected_severity = (
        "CRITICAL" if score >= 81 else "HIGH" if score >= 51 else "MEDIUM" if score >= 21 else "LOW"
    )
    if severity != expected_severity:
        raise InspectionError("Upstream risk severity does not match its score band")
    if severity in {"MEDIUM"} and recommendation != "CAUTION":
        raise InspectionError("Upstream recommendation does not match its risk severity")
    if severity in {"HIGH", "CRITICAL"} and recommendation != "DO_NOT_INSTALL":
        raise InspectionError("Upstream recommendation does not match its risk severity")

    raw_execution_successful = raw.get("execution_successful")
    completeness_execution_successful = completeness.get("execution_successful")
    if not isinstance(raw_execution_successful, bool):
        raise InspectionError("Upstream execution_successful must be a boolean")
    if not isinstance(completeness_execution_successful, bool):
        raise InspectionError(
            "Upstream analysis_completeness.execution_successful must be a boolean"
        )
    execution_successful = (
        raw_execution_successful is True and completeness_execution_successful is True
    )
    entirely_uninspected = completeness.get("entirely_uninspected_files")
    if (
        isinstance(entirely_uninspected, bool)
        or not isinstance(entirely_uninspected, int)
        or entirely_uninspected < 0
    ):
        raise InspectionError(
            "Upstream analysis_completeness.entirely_uninspected_files must be a non-negative integer"
        )
    coverage = completeness.get("coverage_percent")
    if (
        isinstance(coverage, bool)
        or not isinstance(coverage, (int, float))
        or not 0 <= float(coverage) <= 100
    ):
        raise InspectionError(
            "Upstream analysis_completeness.coverage_percent must be from 0 to 100"
        )
    required_count_fields = (
        "total_components",
        "scanned_components",
        "fully_inspected_files",
        "partially_inspected_files",
        "findings_before_filtering",
        "findings_after_filtering",
    )
    for field in required_count_fields:
        count = completeness.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise InspectionError(
                f"Upstream analysis_completeness.{field} must be a non-negative integer"
            )
    if not isinstance(completeness.get("is_complete"), bool):
        raise InspectionError("Upstream analysis_completeness.is_complete must be a boolean")
    completeness_status = completeness.get("status")
    if completeness_status not in {"complete", "partial", "failed"}:
        raise InspectionError("Upstream analysis_completeness.status is unsupported")
    list_fields = (
        "ledger_exceptions",
        "scope_exclusions",
        "analyzer_statuses",
        "references",
        "limitations",
    )
    for field in list_fields:
        if not isinstance(completeness.get(field), list):
            raise InspectionError(
                f"Upstream analysis_completeness.{field} must be a list"
            )
    limitations = completeness.get("limitations")
    analyzer_statuses = completeness.get("analyzer_statuses")
    if any(not isinstance(item, str) for item in limitations):
        raise InspectionError(
            "Upstream analysis_completeness.limitations must contain strings"
        )
    if any(not isinstance(item, Mapping) for item in analyzer_statuses):
        raise InspectionError(
            "Upstream analysis_completeness.analyzer_statuses must contain objects"
        )
    metadata_bool_fields = (
        "has_executable_scripts",
        "llm_requested",
        "llm_available",
        "meta_analysis_applied",
    )
    for field in metadata_bool_fields:
        if not isinstance(metadata.get(field), bool):
            raise InspectionError(f"Upstream metadata.{field} must be a boolean")
    if not isinstance(metadata.get("skillspector_version"), str):
        raise InspectionError("Upstream metadata.skillspector_version must be a string")
    if not isinstance(metadata.get("inference_usage"), list):
        raise InspectionError("Upstream metadata.inference_usage must be a list")
    if "llm_degraded" in metadata and not isinstance(metadata["llm_degraded"], bool):
        raise InspectionError("Upstream metadata.llm_degraded must be a boolean")
    if "llm_error" in metadata and not isinstance(metadata["llm_error"], str):
        raise InspectionError("Upstream metadata.llm_error must be a string")
    llm_calls_attempted = metadata.get("llm_calls_attempted")
    llm_calls_succeeded = metadata.get("llm_calls_succeeded")
    if llm_calls_attempted is not None:
        if (
            isinstance(llm_calls_attempted, bool)
            or not isinstance(llm_calls_attempted, int)
            or llm_calls_attempted < 0
        ):
            raise InspectionError(
                "Upstream metadata.llm_calls_attempted must be a non-negative integer"
            )
    if llm_calls_succeeded is not None:
        if (
            isinstance(llm_calls_succeeded, bool)
            or not isinstance(llm_calls_succeeded, int)
            or llm_calls_succeeded < 0
        ):
            raise InspectionError(
                "Upstream metadata.llm_calls_succeeded must be a non-negative integer"
            )
        if llm_calls_attempted is None or llm_calls_succeeded > llm_calls_attempted:
            raise InspectionError(
                "Upstream metadata LLM call counters are inconsistent"
            )
    for field in ("transitive_targets_scanned", "transitive_bytes_scanned"):
        value = metadata.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise InspectionError(
                f"Upstream metadata.{field} must be a non-negative integer"
            )
    transitive_truncated = metadata.get("transitive_truncated", False)
    if not isinstance(transitive_truncated, bool):
        raise InspectionError("Upstream metadata.transitive_truncated must be a boolean")
    transitive_reasons = metadata.get("transitive_truncation_reasons", [])
    if not isinstance(transitive_reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in transitive_reasons
    ):
        raise InspectionError(
            "Upstream metadata.transitive_truncation_reasons must contain non-empty strings"
        )
    if transitive_truncated is not bool(transitive_reasons):
        raise InspectionError("Upstream transitive-truncation metadata is inconsistent")
    llm_line_complete = not metadata["llm_requested"] or (
        metadata["llm_available"] is True and metadata.get("llm_degraded") is not True
    )
    optional_static_analyzers = {
        "meta_analyzer",
        "semantic_security_discovery",
        "semantic_developer_intent",
        "semantic_quality_policy",
    }
    analyzer_line_complete = True
    for index, status_item in enumerate(analyzer_statuses):
        analyzer_id = status_item.get("analyzer_id")
        analyzer_status = status_item.get("status")
        if not isinstance(analyzer_id, str) or not isinstance(analyzer_status, str):
            raise InspectionError(
                f"Upstream analyzer_statuses[{index}] needs string analyzer_id and status"
            )
        allowed = analyzer_status in {"completed", "not_applicable"} or (
            metadata["llm_requested"] is False
            and analyzer_status == "disabled"
            and analyzer_id in optional_static_analyzers
        )
        analyzer_line_complete = analyzer_line_complete and allowed
    complete = (
        completeness.get("is_complete") is True
        and execution_successful
        and entirely_uninspected == 0
        and completeness.get("partially_inspected_files") == 0
        and completeness.get("scanned_components") == completeness.get("total_components")
        and completeness.get("fully_inspected_files") == completeness.get("total_components")
        and not completeness.get("ledger_exceptions")
        and not limitations
        and analyzer_line_complete
        and completeness_status == "complete"
        and float(coverage) == 100.0
        and llm_line_complete
        and not transitive_truncated
    )
    expected_low_recommendation = "SAFE" if complete else "CAUTION"
    if severity == "LOW" and recommendation != expected_low_recommendation:
        raise InspectionError(
            "Upstream recommendation does not reflect low-risk analysis completeness"
        )

    return {
        "skill": {
            "name": skill.get("name") if isinstance(skill.get("name"), str) else "unknown",
            "source": skill.get("source") if isinstance(skill.get("source"), str) else None,
            "scanned_at": skill.get("scanned_at")
            if isinstance(skill.get("scanned_at"), str)
            else None,
        },
        "risk_assessment": {
            "score": score,
            "severity": severity,
            "recommendation": recommendation,
            "max_issue_severity": max_issue_severity,
        },
        "components": json.loads(json.dumps(components)),
        "structured_summaries": json.loads(json.dumps(structured_summaries)),
        "issues": normalized_issues,
        "suppressed_count": suppressed_count,
        "suppressed": normalized_suppressed,
        "metadata": json.loads(json.dumps(metadata)),
        "execution_successful": execution_successful,
        "analysis_completeness": json.loads(json.dumps(completeness)),
        "analysis_complete": complete,
    }


def _data_egress(use_llm: bool, removed_secrets: Sequence[str]) -> dict[str, Any]:
    return {
        "remote_target_fetch": {
            "performed_by_adapter": False,
            "fact": "The adapter accepts local targets only.",
        },
        "dependency_coordinates": {
            "may_leave_machine": True,
            "destination": "https://api.osv.dev",
            "applies_in_static_mode": True,
            "fact": (
                "SkillSpector's SC4 check may send declared package names and versions; "
                "it uses a bundled fallback when OSV.dev is unreachable."
            ),
        },
        "target_file_contents": {
            "may_leave_machine": use_llm,
            "destination": "configured SKILLSPECTOR_PROVIDER endpoint" if use_llm else None,
            "fact": (
                "LLM mode was explicitly enabled; analyzer-eligible target contents may be sent."
                if use_llm
                else "Static mode was selected with --no-llm; target contents are not sent to an LLM."
            ),
        },
        "static_mode_default": True,
        "subprocess_environment_policy": (
            "base allowlist plus documented SkillSpector/provider variables"
            if use_llm
            else "static allowlist without provider credentials"
        ),
        "secrets_removed_from_subprocess": list(removed_secrets),
    }


def _base_report(
    target: Path,
    target_hash: Mapping[str, Any],
    output_dir: Path,
    use_llm: bool,
    removed_secrets: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "updated_at": None,
        "status": "error",
        "target": {
            "source": str(target),
            "resolved_path": str(target.resolve(strict=True)),
            **dict(target_hash),
        },
        "artifacts": {
            "directory": str(output_dir),
            "normalized_report": NORMALIZED_REPORT_NAME,
            "raw_report": None,
            "raw_report_sha256": None,
            "scanner_stdout": STDOUT_NAME,
            "scanner_stderr": STDERR_NAME,
        },
        "scanner": {
            "name": "NVIDIA SkillSpector",
            "available": False,
            "executable": None,
            "version": None,
            "mode": "llm" if use_llm else "static",
            "exit_code": None,
            "analysis_complete": False,
        },
        "data_egress": _data_egress(use_llm, removed_secrets),
        "upstream": None,
        "semantic_review": {"status": "pending"},
        "combined_verdict": None,
        "gate_decision": "BLOCK_PENDING_REVIEW",
        "failure": None,
        "limitations": [],
    }


def _load_json_file(path: Path, *, max_bytes: int = MAX_REPORT_BYTES) -> object:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise InspectionError(f"Expected JSON file was not produced: {path}") from exc
    if size > max_bytes:
        raise InspectionError(f"JSON file exceeds the {max_bytes}-byte safety limit: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectionError(f"Invalid UTF-8 JSON in {path}: {exc}") from exc


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    _hash_file(path, digest)
    return digest.hexdigest()


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _copy_target_snapshot(target: Path, snapshot_root: Path) -> Path:
    snapshot_root.mkdir()
    snapshot_target = snapshot_root / (target.name or "target")
    if target.is_file():
        if _is_link_like(target):
            raise InspectionError(f"Refusing link-like target during snapshot: {target}")
        shutil.copy2(target, snapshot_target, follow_symlinks=False)
        return snapshot_target

    snapshot_target.mkdir()

    def walk_error(error: OSError) -> None:
        raise InspectionError(f"Could not snapshot target: {error}") from error

    for current, directories, files in os.walk(
        target, topdown=True, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        if _is_link_like(current_path):
            raise InspectionError(
                f"Refusing symbolic link or junction during snapshot: {current_path}"
            )
        relative = current_path.relative_to(target)
        snapshot_current = snapshot_target / relative
        directories.sort()
        files.sort()
        for directory_name in directories:
            source_directory = current_path / directory_name
            if _is_link_like(source_directory):
                raise InspectionError(
                    f"Refusing symbolic link or junction during snapshot: {source_directory}"
                )
            destination_directory = snapshot_current / directory_name
            destination_directory.mkdir()
            shutil.copystat(
                source_directory, destination_directory, follow_symlinks=False
            )
        for file_name in files:
            source_file = current_path / file_name
            if _is_link_like(source_file) or not source_file.is_file():
                raise InspectionError(
                    f"Refusing non-regular entry during snapshot: {source_file}"
                )
            shutil.copy2(
                source_file, snapshot_current / file_name, follow_symlinks=False
            )
    shutil.copystat(target, snapshot_target, follow_symlinks=False)
    return snapshot_target


def _make_snapshot_read_only(snapshot_target: Path) -> None:
    paths: list[Path]
    if snapshot_target.is_dir():
        paths = [
            *(path for path in snapshot_target.rglob("*") if path.is_file()),
            *sorted(
                (path for path in snapshot_target.rglob("*") if path.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ),
            snapshot_target,
        ]
    else:
        paths = [snapshot_target]
    for path in paths:
        try:
            mode = path.stat(follow_symlinks=False).st_mode
            path.chmod(mode & ~0o222, follow_symlinks=False)
        except OSError as exc:
            raise InspectionError(f"Could not make inspection snapshot read-only: {path}") from exc


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _run_scanner_bounded(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    raw_path: Path,
    timeout_seconds: float,
) -> int:
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    process = subprocess.Popen(
        list(arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=dict(environment),
        cwd=cwd,
        start_new_session=os.name != "nt",
        creationflags=creation_flags,
    )
    overflow = threading.Event()
    reader_errors: list[BaseException] = []

    def drain(stream: Any, destination: Path) -> None:
        written = 0
        try:
            with destination.open("xb") as handle:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    remaining = MAX_LOG_BYTES - written
                    if remaining > 0:
                        bounded = chunk[:remaining]
                        handle.write(bounded)
                        written += len(bounded)
                    if len(chunk) > remaining:
                        overflow.set()
        except BaseException as exc:  # propagated after the child is stopped
            reader_errors.append(exc)
            overflow.set()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout_path), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_path), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_seconds
    failure: str | None = None
    while process.poll() is None:
        if overflow.is_set():
            failure = f"SkillSpector output exceeded the {MAX_LOG_BYTES}-byte log limit"
            break
        if raw_path.is_symlink():
            failure = "SkillSpector created a symbolic-link raw report"
            break
        try:
            raw_size = raw_path.stat().st_size if raw_path.exists() else 0
        except OSError as exc:
            failure = f"Could not monitor SkillSpector raw output: {exc}"
            break
        if raw_size > MAX_REPORT_BYTES:
            failure = f"SkillSpector raw report exceeded the {MAX_REPORT_BYTES}-byte limit"
            break
        if time.monotonic() >= deadline:
            failure = f"SkillSpector exceeded the {timeout_seconds:g}-second timeout"
            break
        time.sleep(0.05)

    if failure is not None:
        _terminate_process_tree(process)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        process.wait(timeout=10)

    for thread in threads:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads):
        _terminate_process_tree(process)
        for thread in threads:
            thread.join(timeout=2)
    if reader_errors:
        raise InspectionError(f"Could not capture SkillSpector output: {reader_errors[0]}")
    if overflow.is_set() and failure is None:
        failure = f"SkillSpector output exceeded the {MAX_LOG_BYTES}-byte log limit"
    if failure is not None:
        raise InspectionError(failure)
    if raw_path.is_symlink():
        raise InspectionError("SkillSpector created a symbolic-link raw report")
    if raw_path.exists() and raw_path.stat().st_size > MAX_REPORT_BYTES:
        raise InspectionError(
            f"SkillSpector raw report exceeded the {MAX_REPORT_BYTES}-byte limit"
        )
    return process.returncode


def scan_target(
    *,
    target: Path,
    output_dir: Path,
    scanner_command: Sequence[str] | None = None,
    scanner_name: str = "skillspector",
    use_llm: bool = False,
    timeout_seconds: float = 660.0,
) -> tuple[int, Path]:
    """Run a scan and return ``(adapter_exit_code, normalized_report_path)``."""

    if timeout_seconds <= 0:
        raise InspectionError("Timeout must be greater than zero")
    requested_target = target.expanduser()
    _reject_link_components(requested_target, label="target")
    if _is_link_like(requested_target):
        raise InspectionError(f"Refusing symbolic-link or junction target: {requested_target}")
    target = requested_target.resolve(strict=True)
    requested_output = output_dir.expanduser()
    _reject_link_components(requested_output.parent, label="output directory")
    if _is_link_like(requested_output):
        raise InspectionError(
            f"Refusing symbolic-link or junction output directory: {requested_output}"
        )
    output_dir = requested_output.resolve(strict=False)
    if target.is_dir() and _is_within(output_dir, target):
        raise InspectionError("Output directory must be outside the inspected target directory")
    if os.path.lexists(output_dir):
        raise InspectionError("Output directory must be a new path to prevent artifact overwrite")
    output_dir.mkdir(parents=True, exist_ok=False)
    before_snapshot_hash = hash_target(target)
    snapshot_target = _copy_target_snapshot(target, output_dir / "snapshot")
    target_hash = hash_target(snapshot_target)
    after_snapshot_hash = hash_target(target)
    if before_snapshot_hash != target_hash or after_snapshot_hash != target_hash:
        raise InspectionError("Target changed while the immutable inspection snapshot was created")
    _make_snapshot_read_only(snapshot_target)
    environment, removed_secrets = _scanner_environment(use_llm)
    report = _base_report(target, target_hash, output_dir, use_llm, removed_secrets)
    report["artifacts"]["snapshot_target"] = str(
        snapshot_target.relative_to(output_dir).as_posix()
    )
    normalized_path = output_dir / NORMALIZED_REPORT_NAME
    raw_path = output_dir / RAW_REPORT_NAME
    stdout_path = output_dir / STDOUT_NAME
    stderr_path = output_dir / STDERR_NAME

    try:
        scan_deadline = time.monotonic() + timeout_seconds
        if scanner_command is None:
            resolved_scanner = _resolve_scanner(scanner_name)
            command = [str(resolved_scanner)]
        else:
            if not scanner_command:
                raise InspectionError("Scanner command cannot be empty")
            command = [str(item) for item in scanner_command]
            resolved_scanner = Path(command[0]).expanduser().resolve(strict=True)
            _validate_scanner_executable(resolved_scanner)
            command[0] = str(resolved_scanner)
        if (target.is_dir() and _is_within(resolved_scanner, target)) or (
            target.is_file() and resolved_scanner == target
        ):
            raise InspectionError("Refusing to execute a scanner from inside the untrusted target")
        report["scanner"].update(
            {
                "available": True,
                "executable": str(resolved_scanner),
                "version": _scanner_version(
                    command,
                    environment,
                    max(0.001, scan_deadline - time.monotonic()),
                    output_dir,
                ),
            }
        )
        arguments = [
            *command,
            "scan",
            str(snapshot_target),
            "--format",
            "json",
            "--output",
            str(raw_path),
            "--fail-on-incomplete",
        ]
        if not use_llm:
            arguments.append("--no-llm")
        remaining_seconds = scan_deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise InspectionError(
                f"SkillSpector exceeded the {timeout_seconds:g}-second aggregate timeout"
            )
        return_code = _run_scanner_bounded(
            arguments,
            environment=environment,
            cwd=output_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            raw_path=raw_path,
            timeout_seconds=remaining_seconds,
        )
        if hash_target(snapshot_target) != target_hash or hash_target(target) != target_hash:
            raise InspectionError("Target or inspection snapshot changed during scanning")
        report["scanner"]["exit_code"] = return_code
        if raw_path.exists():
            report["artifacts"]["raw_report"] = RAW_REPORT_NAME
            report["artifacts"]["raw_report_sha256"] = _sha256_path(raw_path)
        raw = _load_json_file(raw_path)
        upstream = normalize_upstream(raw)
        upstream_metadata = upstream["metadata"]
        if upstream_metadata.get("llm_requested") is not use_llm:
            raise InspectionError(
                "SkillSpector metadata does not match the adapter's requested analysis mode"
            )
        if not use_llm and upstream_metadata.get("meta_analysis_applied") is True:
            raise InspectionError(
                "SkillSpector reported LLM meta-analysis during a static-only scan"
            )
        report["upstream"] = upstream
        report["scanner"]["analysis_complete"] = upstream["analysis_complete"]

        if return_code not in (0, 1):
            raise InspectionError(
                f"SkillSpector returned error exit code {return_code}; raw evidence was preserved"
            )
        report["status"] = (
            "pending_semantic_review" if upstream["analysis_complete"] else "partial"
        )
        report["gate_decision"] = "BLOCK_PENDING_REVIEW"
        if not upstream["analysis_complete"]:
            report["limitations"].append("SkillSpector reported incomplete analysis.")
        _write_json(normalized_path, report)
        return 0, normalized_path
    except (InspectionError, OSError, subprocess.SubprocessError) as exc:
        for log_path in (stdout_path, stderr_path):
            if not os.path.lexists(log_path):
                _atomic_write_text(log_path, "")
        if (
            raw_path.is_file()
            and not _is_link_like(raw_path)
            and raw_path.stat().st_size <= MAX_REPORT_BYTES
        ):
            report["artifacts"]["raw_report"] = RAW_REPORT_NAME
            report["artifacts"]["raw_report_sha256"] = _sha256_path(raw_path)
        report["status"] = "error"
        report["gate_decision"] = "BLOCK_PENDING_REVIEW"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        report["limitations"].append(
            "Static scanner evidence is unavailable or incomplete; use the documented manual fallback."
        )
        _write_json(normalized_path, report)
        return 2, normalized_path


def _list_of_strings(value: object, *, field: str, non_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (non_empty and not value):
        qualifier = "a non-empty list" if non_empty else "a list"
        raise InspectionError(f"Semantic review field {field!r} must be {qualifier}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise InspectionError(f"Semantic review field {field!r} must contain non-empty strings")
    return list(value)


def normalize_semantic_review(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InspectionError("Semantic review must be a JSON object")
    verdict = value.get("verdict")
    if not isinstance(verdict, str) or verdict.upper() not in ALLOWED_VERDICTS:
        raise InspectionError("Semantic review verdict must be APPROVE, CAUTION, or REJECT")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise InspectionError("Semantic review summary must be a non-empty string")
    reviewed_files = _list_of_strings(
        value.get("reviewed_files"), field="reviewed_files", non_empty=True
    )
    surfaces = _list_of_strings(
        value.get("sensitive_surfaces", []), field="sensitive_surfaces"
    )
    guardrails = _list_of_strings(value.get("guardrails", []), field="guardrails")
    limitations = _list_of_strings(value.get("limitations", []), field="limitations")
    raw_judgments = value.get("finding_judgments", [])
    if not isinstance(raw_judgments, list):
        raise InspectionError("Semantic review field 'finding_judgments' must be a list")
    judgments: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, judgment in enumerate(raw_judgments):
        if not isinstance(judgment, Mapping):
            raise InspectionError(f"Finding judgment {index} must be an object")
        finding_id = judgment.get("id")
        disposition = judgment.get("disposition")
        rationale = judgment.get("rationale")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise InspectionError(f"Finding judgment {index} needs a non-empty id")
        if finding_id in seen_ids:
            raise InspectionError(f"Duplicate finding judgment id: {finding_id}")
        if not isinstance(disposition, str) or disposition not in ALLOWED_DISPOSITIONS:
            raise InspectionError(f"Invalid disposition for finding {finding_id!r}")
        if not isinstance(rationale, str) or not rationale.strip():
            raise InspectionError(f"Finding judgment {finding_id!r} needs a rationale")
        seen_ids.add(finding_id)
        judgments.append(
            {"id": finding_id, "disposition": disposition, "rationale": rationale}
        )
    return {
        "status": "complete",
        "reviewed_at": utc_now(),
        "verdict": verdict.upper(),
        "summary": summary,
        "reviewed_files": reviewed_files,
        "sensitive_surfaces": surfaces,
        "finding_judgments": judgments,
        "guardrails": guardrails,
        "limitations": limitations,
    }


def _issue_ids(report: Mapping[str, Any]) -> tuple[dict[str, str], set[str]]:
    upstream = report.get("upstream")
    if not isinstance(upstream, Mapping):
        return {}, set()
    issues = upstream.get("issues")
    if not isinstance(issues, list):
        return {}, set()
    severities: dict[str, str] = {}
    duplicates: set[str] = set()
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        issue_id = issue.get("finding_id")
        severity = issue.get("severity")
        if isinstance(issue_id, str) and isinstance(severity, str):
            if issue_id in severities and severities[issue_id] != severity:
                duplicates.add(issue_id)
            severities[issue_id] = severity
    return severities, duplicates


def _verify_preserved_upstream_evidence(
    report_path: Path, report: Mapping[str, Any]
) -> None:
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise InspectionError("Normalized report has no valid artifact manifest")
    raw_name = artifacts.get("raw_report")
    expected_digest = artifacts.get("raw_report_sha256")
    if not isinstance(raw_name, str) or Path(raw_name).name != raw_name:
        raise InspectionError("Complete scanner evidence needs a local raw-report filename")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise InspectionError("Complete scanner evidence needs a raw-report SHA-256")
    raw_path = report_path.parent / raw_name
    if _sha256_path(raw_path) != expected_digest.lower():
        raise InspectionError("Preserved SkillSpector evidence changed after scanning")
    normalized = normalize_upstream(_load_json_file(raw_path))
    if normalized != report.get("upstream"):
        raise InspectionError(
            "Normalized SkillSpector evidence no longer matches the preserved raw report"
        )
    snapshot_relative = artifacts.get("snapshot_target")
    if (
        not isinstance(snapshot_relative, str)
        or Path(snapshot_relative).is_absolute()
        or ".." in Path(snapshot_relative).parts
    ):
        raise InspectionError("Normalized report has no safe snapshot target path")
    snapshot_path = (report_path.parent / snapshot_relative).resolve(strict=True)
    if not _is_within(snapshot_path, report_path.parent):
        raise InspectionError("Inspection snapshot escapes the report directory")
    target = report.get("target")
    if not isinstance(target, Mapping):
        raise InspectionError("Normalized report has no target identity")
    snapshot_hash = hash_target(snapshot_path)
    for field in ("algorithm", "digest", "entries", "bytes"):
        if snapshot_hash.get(field) != target.get(field):
            raise InspectionError("Preserved inspection snapshot no longer matches its target")


def _verdict_rank(verdict: str) -> int:
    return {"APPROVE": 0, "CAUTION": 1, "REJECT": 2}[verdict]


def _evidence_verdict(
    upstream: Mapping[str, Any] | None,
    severities: Mapping[str, str],
    judgments: Mapping[str, Mapping[str, str]],
    *,
    scanner_complete: bool,
) -> str:
    unresolved_any = {
        finding_id
        for finding_id in severities
        if finding_id not in judgments
        or judgments[finding_id].get("disposition") == "unresolved"
    }
    evidence_verdict = "APPROVE"
    if not scanner_complete or unresolved_any:
        evidence_verdict = "CAUTION"
    if upstream is None:
        return evidence_verdict
    risk = upstream.get("risk_assessment")
    if not isinstance(risk, Mapping):
        raise InspectionError("Normalized upstream risk assessment is missing")
    recommendation = risk.get("recommendation")
    risk_severity = risk.get("severity")
    max_issue_severity = risk.get("max_issue_severity")
    if recommendation == "CAUTION" or upstream.get("suppressed_count", 0) > 0:
        evidence_verdict = max(evidence_verdict, "CAUTION", key=_verdict_rank)
    suppressed = upstream.get("suppressed")
    suppressed_high = bool(
        isinstance(suppressed, list)
        and any(
            isinstance(item, Mapping)
            and item.get("severity") in {"HIGH", "CRITICAL"}
            for item in suppressed
        )
    )
    if (
        recommendation == "DO_NOT_INSTALL"
        or risk_severity in {"HIGH", "CRITICAL"}
        or max_issue_severity in {"HIGH", "CRITICAL"}
        or any(severity in {"HIGH", "CRITICAL"} for severity in severities.values())
        or suppressed_high
    ):
        return "REJECT"
    return evidence_verdict


def finalize_report(report_path: Path, semantic_review_path: Path) -> tuple[int, Path]:
    report_path = report_path.expanduser().resolve(strict=True)
    semantic_review_path = semantic_review_path.expanduser().resolve(strict=True)
    report = _load_json_file(report_path)
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise InspectionError(f"Unsupported normalized report schema in {report_path}")
    if report.get("status") == "complete":
        raise InspectionError("A finalized report is immutable; run a new inspection to revise it")
    pending_review = report.get("semantic_review")
    if not isinstance(pending_review, Mapping) or pending_review.get("status") != "pending":
        raise InspectionError("Normalized report is not awaiting semantic review")
    target = report.get("target")
    if not isinstance(target, Mapping) or not isinstance(target.get("resolved_path"), str):
        raise InspectionError("Normalized report has no valid target identity")
    current_hash = hash_target(Path(target["resolved_path"]))
    for field in ("algorithm", "digest", "entries", "bytes"):
        if current_hash.get(field) != target.get(field):
            raise InspectionError("Target contents changed after scanning; run a new inspection")

    semantic_raw = _load_json_file(semantic_review_path, max_bytes=2 * 1024 * 1024)
    semantic = normalize_semantic_review(semantic_raw)
    severities, duplicate_ids = _issue_ids(report)
    if duplicate_ids:
        raise InspectionError(
            "Upstream report contains finding_ids with conflicting severities: "
            + ", ".join(sorted(duplicate_ids))
        )
    judgments = {item["id"]: item for item in semantic["finding_judgments"]}
    unknown = set(judgments) - set(severities)
    if unknown:
        raise InspectionError("Semantic review references unknown finding ids: " + ", ".join(sorted(unknown)))
    verdict = semantic["verdict"]
    scanner = report.get("scanner")
    scanner_complete = bool(
        isinstance(scanner, Mapping)
        and report.get("status") == "pending_semantic_review"
        and report.get("failure") is None
        and scanner.get("available") is True
        and scanner.get("analysis_complete") is True
        and scanner.get("exit_code") in {0, 1}
        and isinstance(scanner.get("version"), str)
        and bool(scanner.get("version"))
    )
    upstream = report.get("upstream")
    if isinstance(upstream, Mapping):
        analysis_completeness = upstream.get("analysis_completeness")
        scanner_complete = bool(
            scanner_complete
            and upstream.get("execution_successful") is True
            and upstream.get("analysis_complete") is True
            and isinstance(analysis_completeness, Mapping)
            and analysis_completeness.get("is_complete") is True
            and analysis_completeness.get("entirely_uninspected_files") == 0
        )
    else:
        scanner_complete = False
    if isinstance(upstream, Mapping):
        _verify_preserved_upstream_evidence(report_path, report)

    evidence_verdict = _evidence_verdict(
        upstream if isinstance(upstream, Mapping) else None,
        severities,
        judgments,
        scanner_complete=scanner_complete,
    )

    if verdict == "APPROVE" and evidence_verdict != "APPROVE":
        raise InspectionError(
            "APPROVE is invalid because scanner evidence requires "
            f"{evidence_verdict}; use a conservative semantic verdict"
        )
    combined_verdict = max(verdict, evidence_verdict, key=_verdict_rank)
    if combined_verdict == "APPROVE":
        gate = "ALLOW"
    elif combined_verdict == "CAUTION":
        gate = "PROMPT"
    else:
        gate = "BLOCK"

    report["semantic_review"] = semantic
    report["combined_verdict"] = combined_verdict
    report["gate_decision"] = gate
    report["status"] = "complete" if scanner_complete else "partial"
    report["updated_at"] = utc_now()
    report["limitations"] = list(report.get("limitations") or []) + semantic["limitations"]
    if not scanner_complete:
        report["limitations"].append(
            "The verdict used manual fallback; automated release or installation remains gated."
        )
    report["semantic_review_source_sha256"] = _sha256_path(semantic_review_path)
    report["semantic_review_sha256"] = _canonical_json_sha256(semantic)
    _write_json(report_path, report)
    return (0 if gate == "ALLOW" else 1), report_path


def validate_report(report_path: Path, target_path: Path | None = None) -> tuple[int, str]:
    report_path = report_path.expanduser().resolve(strict=True)
    raw = _load_json_file(report_path)
    if not isinstance(raw, Mapping):
        raise InspectionError("Normalized report must be a JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise InspectionError(f"Expected schema_version {SCHEMA_VERSION!r}")
    status = raw.get("status")
    if status not in {"pending_semantic_review", "complete", "partial", "error"}:
        raise InspectionError(f"Unsupported report status: {status!r}")
    target = raw.get("target")
    if not isinstance(target, Mapping):
        raise InspectionError("Report target identity is missing")
    digest = target.get("digest")
    algorithm = target.get("algorithm")
    entries = target.get("entries")
    byte_count = target.get("bytes")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in digest)
    ):
        raise InspectionError("Report target digest is invalid")
    if algorithm not in {"sha256", FILE_HASH_ALGORITHM, TREE_HASH_ALGORITHM}:
        raise InspectionError("Report target hash algorithm is unsupported")
    kind = target.get("kind")
    expected_algorithm = {
        "archive": "sha256",
        "file": FILE_HASH_ALGORITHM,
        "directory": TREE_HASH_ALGORITHM,
    }.get(kind)
    if expected_algorithm != algorithm:
        raise InspectionError("Report target kind and hash algorithm are inconsistent")
    if isinstance(entries, bool) or not isinstance(entries, int) or entries < 0:
        raise InspectionError("Report target entry count is invalid")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise InspectionError("Report target byte count is invalid")
    if raw.get("gate_decision") == "ALLOW" and target_path is None:
        raise InspectionError("Validating an ALLOW report requires --target freshness checking")
    if target_path is not None:
        current = hash_target(target_path)
        for field in ("algorithm", "digest", "entries", "bytes"):
            if current.get(field) != target.get(field):
                raise InspectionError(
                    "Report target identity does not match the supplied target"
                )
    gate = raw.get("gate_decision")
    if gate not in {"BLOCK_PENDING_REVIEW", "ALLOW", "PROMPT", "BLOCK"}:
        raise InspectionError(f"Unsupported gate decision: {gate!r}")
    verdict = raw.get("combined_verdict")
    scanner = raw.get("scanner")
    semantic = raw.get("semantic_review")
    upstream = raw.get("upstream")
    if not isinstance(scanner, Mapping):
        raise InspectionError("Report scanner state must be an object")
    scanner_mode = scanner.get("mode")
    scanner_exit = scanner.get("exit_code")
    if scanner_mode not in {"static", "llm"}:
        raise InspectionError("Report scanner mode is invalid")
    if scanner_exit is not None and (
        isinstance(scanner_exit, bool) or not isinstance(scanner_exit, int)
    ):
        raise InspectionError("Report scanner exit code is invalid")
    data_egress = raw.get("data_egress")
    if not isinstance(data_egress, Mapping):
        raise InspectionError("Report data-egress declaration is missing")
    remote_fetch = data_egress.get("remote_target_fetch")
    dependency_egress = data_egress.get("dependency_coordinates")
    content_egress = data_egress.get("target_file_contents")
    removed_secrets = data_egress.get("secrets_removed_from_subprocess")
    expected_environment_policy = (
        "base allowlist plus documented SkillSpector/provider variables"
        if scanner_mode == "llm"
        else "static allowlist without provider credentials"
    )
    if (
        not isinstance(remote_fetch, Mapping)
        or remote_fetch.get("performed_by_adapter") is not False
        or not isinstance(dependency_egress, Mapping)
        or dependency_egress.get("may_leave_machine") is not True
        or not isinstance(content_egress, Mapping)
        or content_egress.get("may_leave_machine") is not (scanner_mode == "llm")
        or data_egress.get("subprocess_environment_policy")
        != expected_environment_policy
        or not isinstance(removed_secrets, list)
        or any(not isinstance(name, str) or not name for name in removed_secrets)
    ):
        raise InspectionError("Report data-egress facts contradict the scanner mode")

    if isinstance(upstream, Mapping):
        _verify_preserved_upstream_evidence(report_path, raw)
        metadata = upstream.get("metadata")
        if not isinstance(metadata, Mapping):
            raise InspectionError("Normalized upstream metadata is missing")
        if metadata.get("llm_requested") is not (scanner_mode == "llm"):
            raise InspectionError("Scanner mode contradicts normalized upstream metadata")
        scanner_version = scanner.get("version")
        metadata_version = metadata.get("skillspector_version")
        if (
            isinstance(scanner_version, str)
            and isinstance(metadata_version, str)
            and metadata_version.casefold() not in scanner_version.casefold()
        ):
            raise InspectionError("Scanner version contradicts normalized upstream metadata")

    scanner_complete = bool(
        status == "complete"
        and raw.get("failure") is None
        and scanner.get("available") is True
        and scanner.get("analysis_complete") is True
        and scanner_exit in {0, 1}
        and isinstance(scanner.get("version"), str)
        and bool(scanner.get("version"))
        and isinstance(upstream, Mapping)
        and upstream.get("execution_successful") is True
        and upstream.get("analysis_complete") is True
    )
    if status == "complete" and not scanner_complete:
        raise InspectionError("Complete reports require successful, versioned scanner evidence")

    if not isinstance(semantic, Mapping) or semantic.get("status") not in {
        "pending",
        "complete",
    }:
        raise InspectionError("Report semantic-review state is invalid")
    if semantic.get("status") == "pending":
        if status == "complete" or verdict is not None or gate != "BLOCK_PENDING_REVIEW":
            raise InspectionError("Pending semantic review cannot carry a final decision")
    else:
        normalized_semantic = normalize_semantic_review(semantic)
        semantic_digest = raw.get("semantic_review_source_sha256")
        if (
            not isinstance(semantic_digest, str)
            or len(semantic_digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in semantic_digest)
        ):
            raise InspectionError("Final report lacks a valid semantic-review source digest")
        embedded_semantic_digest = raw.get("semantic_review_sha256")
        if (
            not isinstance(embedded_semantic_digest, str)
            or len(embedded_semantic_digest) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in embedded_semantic_digest
            )
            or not hmac.compare_digest(
                embedded_semantic_digest.lower(),
                _canonical_json_sha256(semantic),
            )
        ):
            raise InspectionError("Final report's embedded semantic review changed")
        severities, conflicts = _issue_ids(raw)
        if conflicts:
            raise InspectionError("Final report contains conflicting finding identities")
        judgments = {
            item["id"]: item for item in normalized_semantic["finding_judgments"]
        }
        unknown = set(judgments) - set(severities)
        if unknown:
            raise InspectionError(
                "Semantic review references unknown finding ids: "
                + ", ".join(sorted(unknown))
            )
        evidence = _evidence_verdict(
            upstream if isinstance(upstream, Mapping) else None,
            severities,
            judgments,
            scanner_complete=scanner_complete,
        )
        expected_verdict = max(
            normalized_semantic["verdict"], evidence, key=_verdict_rank
        )
        expected_gate = {
            "APPROVE": "ALLOW",
            "CAUTION": "PROMPT",
            "REJECT": "BLOCK",
        }[expected_verdict]
        if verdict != expected_verdict or gate != expected_gate:
            raise InspectionError("Final verdict or gate contradicts the preserved evidence")
        if gate == "ALLOW" and status != "complete":
            raise InspectionError("ALLOW requires a complete scanner line")
    if status == "error" and not isinstance(raw.get("failure"), Mapping):
        raise InspectionError("Error reports require bounded failure details")
    return 0, f"valid {SCHEMA_VERSION} report ({status}, gate={gate})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Run a local SkillSpector scan")
    scan_parser.add_argument("target", type=Path, help="Local skill directory, file, or archive")
    scan_parser.add_argument("--output-dir", type=Path, required=True)
    scan_parser.add_argument(
        "--scanner",
        default="skillspector",
        help="Already-installed trusted SkillSpector executable (default: skillspector on PATH)",
    )
    scan_parser.add_argument(
        "--allow-llm",
        action="store_true",
        help="Explicitly allow SkillSpector provider analysis and target-content egress",
    )
    scan_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=660.0,
        help="Aggregate scan deadline (default: 660 seconds; NVIDIA uses 600 seconds)",
    )

    finalize_parser = subparsers.add_parser(
        "finalize", help="Attach and validate a source-aware semantic review"
    )
    finalize_parser.add_argument("report", type=Path)
    finalize_parser.add_argument("semantic_review", type=Path)

    validate_parser = subparsers.add_parser("validate", help="Validate a normalized report")
    validate_parser.add_argument("report", type=Path)
    validate_parser.add_argument(
        "--target",
        type=Path,
        help="Recompute and verify the report identity against these exact local bytes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            code, path = scan_target(
                target=args.target,
                output_dir=args.output_dir,
                scanner_name=args.scanner,
                use_llm=args.allow_llm,
                timeout_seconds=args.timeout_seconds,
            )
            print(path)
            return code
        if args.command == "finalize":
            code, path = finalize_report(args.report, args.semantic_review)
            print(path)
            return code
        if args.command == "validate":
            code, message = validate_report(args.report, args.target)
            print(message)
            return code
    except (InspectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
