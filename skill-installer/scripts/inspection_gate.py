#!/usr/bin/env python3
"""Verify digest-bound skill-inspector reports without executing target content."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Mapping


SCHEMA_VERSION = "skill-inspection/v1"
TREE_HASH_ALGORITHM = "sha256-tree-v2"
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_RAW_EVIDENCE_BYTES = 64 * 1024 * 1024
HIGH_SEVERITIES = {"HIGH", "CRITICAL"}
SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
MAX_SEVERITIES = {"NONE", *SEVERITIES}
RECOMMENDATIONS = {"SAFE", "CAUTION", "DO_NOT_INSTALL"}
VERDICTS = {"APPROVE", "CAUTION", "REJECT"}
DISPOSITIONS = {"explained", "false_positive", "mitigated", "unresolved"}
VERDICT_RANK = {"APPROVE": 0, "CAUTION": 1, "REJECT": 2}


class InspectionGateError(ValueError):
    """Raised when inspection evidence cannot authorize installation."""


@dataclass(frozen=True)
class TargetIdentity:
    algorithm: str
    digest: str
    entries: int
    bytes: int


@dataclass(frozen=True)
class LoadedReport:
    path: Path
    data: Mapping[str, Any]
    target_digest: str


@dataclass(frozen=True)
class InspectionDecision:
    report_path: Path
    verdict: str
    gate_decision: str
    target_digest: str


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: Path, digest: "hashlib._Hash") -> int:
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size


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


def _reject_link_components(path: Path, *, label: str = "skill target") -> None:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if os.path.lexists(current) and _is_link_like(current):
            raise InspectionGateError(
                f"Refusing {label} with a symbolic-link or junction component: {current}"
            )


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


def hash_skill_tree(path: Path) -> TargetIdentity:
    """Compute the skill-inspector ``sha256-tree-v2`` identity."""

    path = path.expanduser()
    _reject_link_components(path)
    if _is_link_like(path):
        raise InspectionGateError(f"Refusing symbolic-link or junction skill target: {path}")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise InspectionGateError(f"Could not resolve skill target {path}: {exc}") from exc
    if not root.is_dir():
        raise InspectionGateError(f"Inspection gate target must be a directory: {root}")

    digest = hashlib.sha256()
    digest.update(b"skill-inspector-directory-v2\0")
    entries = 0
    total_bytes = 0
    def walk_error(error: OSError) -> None:
        raise InspectionGateError(f"Could not enumerate skill target: {error}") from error

    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        if _is_link_like(current_path):
            raise InspectionGateError(
                f"Refusing symbolic link or junction inside skill target: {current_path}"
            )
        directories.sort()
        files.sort()
        for directory_name in directories:
            directory_path = current_path / directory_name
            if _is_link_like(directory_path):
                raise InspectionGateError(
                    f"Refusing symbolic link or junction inside skill target: {directory_path}"
                )
            relative = directory_path.relative_to(root).as_posix().encode("utf-8")
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
                raise InspectionGateError(
                    f"Refusing symbolic link or junction inside skill target: {file_path}"
                )
            if not file_path.is_file():
                raise InspectionGateError(
                    f"Refusing non-regular entry inside skill target: {file_path}"
                )
            relative = file_path.relative_to(root).as_posix().encode("utf-8")
            _hash_entry_header(
                digest,
                b"F",
                relative,
                file_path.stat(follow_symlinks=False).st_mode,
            )
            total_bytes += _hash_file(file_path, digest)
            digest.update(b"\0")
            entries += 1
    return TargetIdentity(
        algorithm=TREE_HASH_ALGORITHM,
        digest=digest.hexdigest(),
        entries=entries,
        bytes=total_bytes,
    )


def _load_json(path: Path) -> Mapping[str, Any]:
    path = path.expanduser()
    _reject_link_components(path, label="inspection report")
    if _is_link_like(path):
        raise InspectionGateError(f"Refusing symbolic-link or junction inspection report: {path}")
    try:
        resolved = path.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as exc:
        raise InspectionGateError(f"Could not read inspection report {path}: {exc}") from exc
    if size > MAX_REPORT_BYTES:
        raise InspectionGateError(
            f"Inspection report exceeds the {MAX_REPORT_BYTES}-byte limit: {resolved}"
        )
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectionGateError(f"Inspection report is not valid UTF-8 JSON: {resolved}") from exc
    if not isinstance(value, Mapping):
        raise InspectionGateError("Inspection report must be a JSON object.")
    return value


def load_report(path: Path) -> LoadedReport:
    path = path.expanduser()
    data = _load_json(path)
    resolved = path.resolve(strict=True)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise InspectionGateError(
            f"Unsupported inspection schema; expected {SCHEMA_VERSION!r}."
        )
    target = data.get("target")
    if not isinstance(target, Mapping):
        raise InspectionGateError("Inspection report has no target identity.")
    digest = target.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise InspectionGateError("Inspection report target digest is invalid.")
    try:
        bytes.fromhex(digest)
    except ValueError as exc:
        raise InspectionGateError("Inspection report target digest is invalid.") from exc
    return LoadedReport(path=resolved, data=data, target_digest=digest.lower())


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InspectionGateError(f"Inspection report field {field!r} must be an object.")
    return value


def _validate_upstream_evidence(
    upstream: Mapping[str, Any],
) -> tuple[bool, dict[str, str], str, int]:
    risk = _mapping(upstream.get("risk_assessment"), "upstream.risk_assessment")
    recommendation = risk.get("recommendation")
    score = risk.get("score")
    risk_severity = risk.get("severity")
    max_severity = risk.get("max_issue_severity")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise InspectionGateError("Inspection report risk score is invalid.")
    if risk_severity not in SEVERITIES:
        raise InspectionGateError("Inspection report risk severity is invalid.")
    if recommendation not in RECOMMENDATIONS:
        raise InspectionGateError("Inspection report recommendation is invalid.")
    if max_severity not in MAX_SEVERITIES:
        raise InspectionGateError("Inspection report maximum issue severity is invalid.")
    expected_severity = (
        "CRITICAL"
        if score >= 81
        else "HIGH"
        if score >= 51
        else "MEDIUM"
        if score >= 21
        else "LOW"
    )
    expected_recommendation = {
        "LOW": "SAFE",
        "MEDIUM": "CAUTION",
        "HIGH": "DO_NOT_INSTALL",
        "CRITICAL": "DO_NOT_INSTALL",
    }[expected_severity]
    if risk_severity != expected_severity or recommendation != expected_recommendation:
        raise InspectionGateError("Inspection report risk fields are internally inconsistent.")
    blocking = (
        recommendation == "DO_NOT_INSTALL"
        or risk_severity in HIGH_SEVERITIES
        or max_severity in HIGH_SEVERITIES
    )
    issues = upstream.get("issues")
    if not isinstance(issues, list):
        raise InspectionGateError("Inspection report upstream.issues must be a list.")
    severities: dict[str, str] = {}
    severity_rank = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    calculated_max = "NONE"
    for issue in issues:
        if not isinstance(issue, Mapping) or issue.get("severity") not in SEVERITIES:
            raise InspectionGateError("Inspection report contains a malformed issue.")
        rule_id = issue.get("id")
        finding_id = issue.get("finding_id")
        confidence = issue.get("confidence")
        tags = issue.get("tags")
        evidence = issue.get("evidence")
        occurrences = issue.get("occurrences")
        location = issue.get("location")
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or not isinstance(finding_id, str)
            or not finding_id
            or not isinstance(tags, list)
            or any(not isinstance(tag, str) for tag in tags)
            or not isinstance(evidence, Mapping)
            or not isinstance(occurrences, list)
            or any(not isinstance(item, Mapping) for item in occurrences)
            or not isinstance(location, Mapping)
        ):
            raise InspectionGateError("Inspection report issue evidence is malformed.")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            raise InspectionGateError("Inspection report issue confidence is invalid.")
        previous = severities.get(finding_id)
        if previous is not None and previous != issue["severity"]:
            raise InspectionGateError("A finding_id has conflicting severities.")
        severities[finding_id] = issue["severity"]
        if severity_rank[issue["severity"]] > severity_rank[calculated_max]:
            calculated_max = issue["severity"]
        if issue["severity"] in HIGH_SEVERITIES:
            blocking = True
    if calculated_max != max_severity:
        raise InspectionGateError("Maximum issue severity contradicts active issues.")
    suppressed_count = upstream.get("suppressed_count")
    suppressed = upstream.get("suppressed")
    if (
        isinstance(suppressed_count, bool)
        or not isinstance(suppressed_count, int)
        or suppressed_count < 0
        or not isinstance(suppressed, list)
        or suppressed_count != len(suppressed)
    ):
        raise InspectionGateError("Inspection report suppression evidence is malformed.")
    seen_suppressed_ids: set[str] = set()
    for item in suppressed:
        if not isinstance(item, Mapping) or item.get("severity") not in SEVERITIES:
            raise InspectionGateError("Inspection report contains malformed suppressed evidence.")
        finding_id = item.get("finding_id")
        confidence = item.get("confidence")
        if (
            not isinstance(item.get("id"), str)
            or not item.get("id")
            or not isinstance(finding_id, str)
            or not finding_id
            or finding_id in severities
            or finding_id in seen_suppressed_ids
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
            or not isinstance(item.get("tags"), list)
            or any(not isinstance(tag, str) for tag in item["tags"])
            or not isinstance(item.get("evidence"), Mapping)
            or not isinstance(item.get("occurrences"), list)
            or any(not isinstance(value, Mapping) for value in item["occurrences"])
            or not isinstance(item.get("location"), Mapping)
        ):
            raise InspectionGateError("Suppressed finding evidence is malformed.")
        seen_suppressed_ids.add(finding_id)
        if item["severity"] in HIGH_SEVERITIES:
            blocking = True
    return blocking, severities, recommendation, suppressed_count


def _validate_string_list(value: object, field: str, *, non_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (non_empty and not value):
        raise InspectionGateError(f"Inspection report field {field!r} must be a list.")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise InspectionGateError(f"Inspection report field {field!r} is malformed.")
    return list(value)


def _validate_semantic_review(
    semantic: Mapping[str, Any], severities: Mapping[str, str]
) -> tuple[str, dict[str, str]]:
    verdict = semantic.get("verdict")
    summary = semantic.get("summary")
    if verdict not in VERDICTS or not isinstance(summary, str) or not summary.strip():
        raise InspectionGateError("Inspection report semantic verdict or summary is invalid.")
    if not isinstance(semantic.get("reviewed_at"), str) or not semantic.get("reviewed_at"):
        raise InspectionGateError("Inspection report semantic review has no review timestamp.")
    _validate_string_list(semantic.get("reviewed_files"), "reviewed_files", non_empty=True)
    for field in ("sensitive_surfaces", "guardrails", "limitations"):
        _validate_string_list(semantic.get(field), field)
    raw_judgments = semantic.get("finding_judgments")
    if not isinstance(raw_judgments, list):
        raise InspectionGateError("Inspection report finding_judgments must be a list.")
    judgments: dict[str, str] = {}
    for item in raw_judgments:
        if not isinstance(item, Mapping):
            raise InspectionGateError("Inspection report contains a malformed finding judgment.")
        finding_id = item.get("id")
        disposition = item.get("disposition")
        rationale = item.get("rationale")
        if (
            not isinstance(finding_id, str)
            or not finding_id
            or disposition not in DISPOSITIONS
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise InspectionGateError("Inspection report contains a malformed finding judgment.")
        if finding_id in judgments:
            raise InspectionGateError("Inspection report repeats a finding judgment.")
        if finding_id not in severities:
            raise InspectionGateError("Inspection report judges an unknown finding_id.")
        judgments[finding_id] = disposition
    return verdict, judgments


def _verify_raw_artifact(report: LoadedReport) -> Mapping[str, Any]:
    artifacts = _mapping(report.data.get("artifacts"), "artifacts")
    raw_name = artifacts.get("raw_report")
    expected_digest = artifacts.get("raw_report_sha256")
    if not isinstance(raw_name, str) or Path(raw_name).name != raw_name:
        raise InspectionGateError("Inspection report raw-evidence filename is invalid.")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise InspectionGateError("Inspection report raw-evidence digest is invalid.")
    try:
        bytes.fromhex(expected_digest)
    except ValueError as exc:
        raise InspectionGateError("Inspection report raw-evidence digest is invalid.") from exc
    raw_path = report.path.parent / raw_name
    _reject_link_components(raw_path, label="raw inspection evidence")
    if _is_link_like(raw_path):
        raise InspectionGateError("Refusing symbolic-link or junction raw inspection evidence.")
    digest = hashlib.sha256()
    try:
        if raw_path.stat().st_size > MAX_RAW_EVIDENCE_BYTES:
            raise InspectionGateError(
                f"Raw inspection evidence exceeds the {MAX_RAW_EVIDENCE_BYTES}-byte limit."
            )
        with raw_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise InspectionGateError(f"Could not read preserved raw inspection evidence: {exc}") from exc
    if digest.hexdigest() != expected_digest.lower():
        raise InspectionGateError("Preserved raw inspection evidence has changed.")
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectionGateError("Preserved raw inspection evidence is not valid JSON.") from exc
    if not isinstance(raw, Mapping):
        raise InspectionGateError("Preserved raw inspection evidence must be an object.")
    return raw


def _verify_snapshot_artifact(report: LoadedReport, expected: TargetIdentity) -> None:
    artifacts = _mapping(report.data.get("artifacts"), "artifacts")
    relative = artifacts.get("snapshot_target")
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise InspectionGateError("Inspection snapshot path is invalid.")
    snapshot_lexical = report.path.parent / relative
    _reject_link_components(snapshot_lexical, label="inspection snapshot")
    try:
        snapshot = snapshot_lexical.resolve(strict=True)
        snapshot.relative_to(report.path.parent)
    except (OSError, ValueError) as exc:
        raise InspectionGateError("Inspection snapshot escapes or is unavailable.") from exc
    actual = hash_skill_tree(snapshot)
    if actual != expected:
        raise InspectionGateError("Preserved inspection snapshot does not match staged bytes.")


def verify_loaded_report(
    report: LoadedReport,
    expected: TargetIdentity,
    *,
    allow_caution: bool = False,
) -> InspectionDecision:
    """Validate a final report and return the authorized gate decision."""

    data = report.data
    target = _mapping(data.get("target"), "target")
    reported_entries = target.get("entries")
    reported_bytes = target.get("bytes")
    if (
        isinstance(reported_entries, bool)
        or not isinstance(reported_entries, int)
        or reported_entries < 0
        or isinstance(reported_bytes, bool)
        or not isinstance(reported_bytes, int)
        or reported_bytes < 0
    ):
        raise InspectionGateError("Inspection report target inventory is invalid.")
    if target.get("algorithm") != expected.algorithm:
        raise InspectionGateError("Inspection report uses a different target-hash algorithm.")
    if report.target_digest != expected.digest:
        raise InspectionGateError("Inspection report is stale or belongs to different skill bytes.")
    if reported_entries != expected.entries or reported_bytes != expected.bytes:
        raise InspectionGateError("Inspection report target inventory does not match the staged skill.")
    if data.get("status") != "complete":
        raise InspectionGateError("Inspection report is incomplete; installation remains blocked.")
    if data.get("failure") is not None:
        raise InspectionGateError("Inspection report records a scanner failure.")

    scanner = _mapping(data.get("scanner"), "scanner")
    if (
        scanner.get("available") is not True
        or scanner.get("analysis_complete") is not True
        or scanner.get("exit_code") not in {0, 1}
        or scanner.get("mode") not in {"static", "llm"}
        or not isinstance(scanner.get("version"), str)
        or not scanner.get("version")
    ):
        raise InspectionGateError("A complete NVIDIA SkillSpector scan is required for installation.")
    raw_upstream = _verify_raw_artifact(report)
    _verify_snapshot_artifact(report, expected)
    semantic = _mapping(data.get("semantic_review"), "semantic_review")
    if semantic.get("status") != "complete":
        raise InspectionGateError("Source-aware semantic review is incomplete.")

    upstream = _mapping(data.get("upstream"), "upstream")
    for field in (
        "skill",
        "risk_assessment",
        "components",
        "structured_summaries",
        "issues",
        "suppressed_count",
        "suppressed",
        "metadata",
        "execution_successful",
        "analysis_completeness",
    ):
        if raw_upstream.get(field) != upstream.get(field):
            raise InspectionGateError(
                f"Normalized field {field!r} contradicts preserved raw evidence."
            )
    if upstream.get("execution_successful") is not True:
        raise InspectionGateError("SkillSpector did not report successful execution.")
    if upstream.get("analysis_complete") is not True:
        raise InspectionGateError("SkillSpector analysis coverage is incomplete.")
    completeness = _mapping(
        upstream.get("analysis_completeness"), "upstream.analysis_completeness"
    )
    count_fields = (
        "total_components",
        "scanned_components",
        "fully_inspected_files",
        "partially_inspected_files",
        "entirely_uninspected_files",
        "findings_before_filtering",
        "findings_after_filtering",
    )
    for field in count_fields:
        value = completeness.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InspectionGateError(
                f"Inspection report completeness count {field!r} is invalid."
            )
    if (
        completeness.get("execution_successful") is not True
        or completeness.get("is_complete") is not True
        or completeness.get("status") != "complete"
        or completeness.get("coverage_percent") != 100.0
        or completeness.get("partially_inspected_files") != 0
        or completeness.get("entirely_uninspected_files") != 0
        or completeness.get("scanned_components") != completeness.get("total_components")
        or completeness.get("fully_inspected_files") != completeness.get("total_components")
        or completeness.get("ledger_exceptions") != []
        or completeness.get("limitations") != []
    ):
        raise InspectionGateError("SkillSpector completeness ledger is not fully complete.")
    components = upstream.get("components")
    summaries = upstream.get("structured_summaries")
    metadata = upstream.get("metadata")
    if (
        not isinstance(components, list)
        or any(not isinstance(item, Mapping) for item in components)
        or not isinstance(summaries, list)
    ):
        raise InspectionGateError("Inspection report component evidence is malformed.")
    if not isinstance(metadata, Mapping):
        raise InspectionGateError("Inspection report scanner metadata is malformed.")
    if metadata.get("llm_requested") is not (scanner.get("mode") == "llm"):
        raise InspectionGateError("Inspection report scanner mode contradicts its metadata.")
    for field in (
        "has_executable_scripts",
        "llm_requested",
        "llm_available",
        "meta_analysis_applied",
    ):
        if not isinstance(metadata.get(field), bool):
            raise InspectionGateError(f"Inspection report metadata field {field!r} is invalid.")
    metadata_version = metadata.get("skillspector_version")
    if (
        not isinstance(metadata_version, str)
        or not metadata_version
        or metadata_version.casefold() not in scanner["version"].casefold()
    ):
        raise InspectionGateError("Inspection report scanner versions are inconsistent.")
    if not isinstance(metadata.get("inference_usage"), list):
        raise InspectionGateError("Inspection report inference usage is malformed.")
    if "llm_degraded" in metadata and not isinstance(metadata["llm_degraded"], bool):
        raise InspectionGateError("Inspection report LLM degradation metadata is malformed.")
    if "llm_error" in metadata and not isinstance(metadata["llm_error"], str):
        raise InspectionGateError("Inspection report LLM error metadata is malformed.")
    llm_calls_attempted = metadata.get("llm_calls_attempted")
    llm_calls_succeeded = metadata.get("llm_calls_succeeded")
    if llm_calls_attempted is not None and (
        isinstance(llm_calls_attempted, bool)
        or not isinstance(llm_calls_attempted, int)
        or llm_calls_attempted < 0
    ):
        raise InspectionGateError("Inspection report LLM attempt count is malformed.")
    if llm_calls_succeeded is not None and (
        isinstance(llm_calls_succeeded, bool)
        or not isinstance(llm_calls_succeeded, int)
        or llm_calls_succeeded < 0
        or llm_calls_attempted is None
        or llm_calls_succeeded > llm_calls_attempted
    ):
        raise InspectionGateError("Inspection report LLM success count is malformed.")
    for field in ("transitive_targets_scanned", "transitive_bytes_scanned"):
        value = metadata.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise InspectionGateError(
                f"Inspection report transitive metadata field {field!r} is malformed."
            )
    transitive_truncated = metadata.get("transitive_truncated", False)
    transitive_reasons = metadata.get("transitive_truncation_reasons", [])
    if (
        not isinstance(transitive_truncated, bool)
        or not isinstance(transitive_reasons, list)
        or any(not isinstance(reason, str) or not reason for reason in transitive_reasons)
        or transitive_truncated is not bool(transitive_reasons)
    ):
        raise InspectionGateError("Inspection report transitive-truncation metadata is malformed.")
    if transitive_truncated:
        raise InspectionGateError("SkillSpector transitive analysis was truncated.")
    analyzer_statuses = completeness.get("analyzer_statuses")
    if not isinstance(analyzer_statuses, list):
        raise InspectionGateError("Inspection report analyzer statuses are malformed.")
    optional_static = {
        "meta_analyzer",
        "semantic_security_discovery",
        "semantic_developer_intent",
        "semantic_quality_policy",
    }
    for item in analyzer_statuses:
        if not isinstance(item, Mapping):
            raise InspectionGateError("Inspection report analyzer status is malformed.")
        analyzer_id = item.get("analyzer_id")
        analyzer_status = item.get("status")
        allowed = analyzer_status in {"completed", "not_applicable"} or (
            scanner.get("mode") == "static"
            and analyzer_status == "disabled"
            and analyzer_id in optional_static
        )
        if not isinstance(analyzer_id, str) or not allowed:
            raise InspectionGateError("Inspection report contains incomplete analyzer status.")
    if scanner.get("mode") == "llm" and (
        metadata.get("llm_available") is not True or metadata.get("llm_degraded") is True
    ):
        raise InspectionGateError("Requested LLM analysis was unavailable or degraded.")
    data_egress = _mapping(data.get("data_egress"), "data_egress")
    remote = _mapping(data_egress.get("remote_target_fetch"), "remote_target_fetch")
    dependencies = _mapping(
        data_egress.get("dependency_coordinates"), "dependency_coordinates"
    )
    contents = _mapping(data_egress.get("target_file_contents"), "target_file_contents")
    removed_secrets = data_egress.get("secrets_removed_from_subprocess")
    expected_environment_policy = (
        "base allowlist plus documented SkillSpector/provider variables"
        if scanner.get("mode") == "llm"
        else "static allowlist without provider credentials"
    )
    if (
        remote.get("performed_by_adapter") is not False
        or dependencies.get("may_leave_machine") is not True
        or contents.get("may_leave_machine") is not (scanner.get("mode") == "llm")
        or data_egress.get("subprocess_environment_policy")
        != expected_environment_policy
        or not isinstance(removed_secrets, list)
        or any(not isinstance(name, str) or not name for name in removed_secrets)
    ):
        raise InspectionGateError("Inspection report data-egress facts are inconsistent.")

    blocking, severities, recommendation, suppressed_count = _validate_upstream_evidence(
        upstream
    )
    semantic_verdict, judgments = _validate_semantic_review(semantic, severities)
    semantic_digest = data.get("semantic_review_source_sha256")
    if not isinstance(semantic_digest, str) or len(semantic_digest) != 64:
        raise InspectionGateError("Inspection report semantic-review digest is invalid.")
    try:
        bytes.fromhex(semantic_digest)
    except ValueError as exc:
        raise InspectionGateError("Inspection report semantic-review digest is invalid.") from exc
    embedded_semantic_digest = data.get("semantic_review_sha256")
    if (
        not isinstance(embedded_semantic_digest, str)
        or len(embedded_semantic_digest) != 64
    ):
        raise InspectionGateError("Inspection report embedded semantic digest is invalid.")
    try:
        bytes.fromhex(embedded_semantic_digest)
    except ValueError as exc:
        raise InspectionGateError(
            "Inspection report embedded semantic digest is invalid."
        ) from exc
    if embedded_semantic_digest.lower() != _canonical_json_sha256(semantic):
        raise InspectionGateError("Inspection report semantic review has changed.")

    unresolved = {
        finding_id
        for finding_id in severities
        if finding_id not in judgments or judgments[finding_id] == "unresolved"
    }
    evidence_verdict = "REJECT" if blocking else "CAUTION" if (
        recommendation == "CAUTION" or suppressed_count > 0 or unresolved
    ) else "APPROVE"
    if semantic_verdict == "APPROVE" and evidence_verdict != "APPROVE":
        raise InspectionGateError("Semantic APPROVE contradicts adverse scanner evidence.")
    expected_verdict = max(
        semantic_verdict, evidence_verdict, key=lambda item: VERDICT_RANK[item]
    )
    expected_gate = {
        "APPROVE": "ALLOW",
        "CAUTION": "PROMPT",
        "REJECT": "BLOCK",
    }[expected_verdict]
    verdict = data.get("combined_verdict")
    gate = data.get("gate_decision")
    if verdict != expected_verdict or gate != expected_gate:
        raise InspectionGateError("Inspection verdict and gate contradict preserved evidence.")
    if gate == "ALLOW":
        return InspectionDecision(report.path, verdict, gate, expected.digest)
    if gate == "PROMPT":
        if not allow_caution:
            raise InspectionGateError(
                "Inspection verdict is CAUTION; explicit --allow-caution acceptance is required."
            )
        return InspectionDecision(report.path, verdict, gate, expected.digest)
    if gate == "BLOCK":
        raise InspectionGateError("Inspection gate blocked installation.")
    raise InspectionGateError("Inspection verdict and gate decision are inconsistent.")


def reports_by_digest(paths: list[Path]) -> dict[str, LoadedReport]:
    reports: dict[str, LoadedReport] = {}
    for path in paths:
        report = load_report(path)
        if report.target_digest in reports:
            raise InspectionGateError(
                f"Multiple inspection reports claim target digest {report.target_digest}."
            )
        reports[report.target_digest] = report
    return reports
