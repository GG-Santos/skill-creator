#!/usr/bin/env python3
"""Minimal fake SkillSpector CLI used only by adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time


def _option(arguments: list[str], name: str) -> str:
    try:
        return arguments[arguments.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"missing {name}") from exc


def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("skillspector 2.11.2-fake")
        return 0
    if not arguments or arguments[0] != "scan":
        print("unsupported fake command", file=sys.stderr)
        return 2
    target = Path(arguments[1])
    output = Path(_option(arguments, "--output"))
    mode = target.name.lower()
    if mode == "error":
        print("simulated scanner failure", file=sys.stderr)
        return 2
    if mode == "malformed":
        output.write_text("{not valid json", encoding="utf-8")
        return 0
    if mode == "oversized_raw":
        output.write_bytes(b"x" * 4096)
        return 0
    if mode == "slow":
        time.sleep(5)

    is_dangerous = mode == "dangerous"
    is_partial = mode == "partial"
    is_transitive_truncated = mode == "transitive_truncated"
    is_execution_failure = mode == "execution_failure"
    is_suppressed = mode in {"suppressed", "suppressed_critical", "suppressed-critical"}
    is_error_with_raw = mode == "error_with_raw"
    llm_requested = "--no-llm" not in arguments
    issues = []
    if is_dangerous:
        issues.append(
            {
                "id": "TT3",
                "finding_id": "fake-credential-exfiltration",
                "category": "Data Exfiltration",
                "severity": "CRITICAL",
                "confidence": 0.99,
                "location": {
                    "file": "SKILL.md",
                    "start_line": 6,
                    "end_line": 6
                },
                "finding": "Credential value is sent to a remote endpoint.",
                "explanation": "The fixture contains a simulated exfiltration path.",
                "remediation": "Remove the outbound credential transfer.",
                "tags": [],
                "evidence": {},
                "match_fingerprint": "fake-critical-fingerprint",
                "occurrences": [
                    {"file": "SKILL.md", "start_line": 6, "end_line": 6}
                ]
            }
        )
    suppressed = (
        [
            {
                "id": "PI1",
                "finding_id": "fake-suppressed",
                "category": "Prompt Injection",
                "severity": (
                    "critical"
                    if mode in {"suppressed_critical", "suppressed-critical"}
                    else "MEDIUM"
                ),
                "confidence": 0.8,
                "location": {"file": "SKILL.md", "start_line": 1, "end_line": 1},
                "tags": [],
                "evidence": {},
                "match_fingerprint": "fake-suppressed-fingerprint",
                "occurrences": [
                    {"file": "SKILL.md", "start_line": 1, "end_line": 1}
                ]
            }
        ]
        if is_suppressed
        else []
    )
    raw = {
        "skill": {
            "name": f"fixture-{mode}",
            "source": str(target),
            "scanned_at": datetime.now(UTC).isoformat()
        },
        "risk_assessment": {
            "score": 92 if is_dangerous else (21 if is_partial else 4),
            "severity": "CRITICAL" if is_dangerous else ("MEDIUM" if is_partial else "LOW"),
            "recommendation": (
                "DO_NOT_INSTALL"
                if is_dangerous
                else "CAUTION"
                if is_partial or is_execution_failure or is_transitive_truncated
                else "SAFE"
            ),
            "max_issue_severity": "CRITICAL" if is_dangerous else "NONE"
        },
        "components": [],
        "structured_summaries": [],
        "issues": issues,
        "suppressed_count": len(suppressed),
        "suppressed": suppressed,
        "metadata": {
            "has_executable_scripts": False,
            "skillspector_version": "2.11.2-fake",
            "llm_requested": llm_requested,
            "llm_available": llm_requested,
            "meta_analysis_applied": False,
            "inference_usage": [],
            "filtering_mode": "heuristic",
            "fake_received_fail_on_incomplete": "--fail-on-incomplete" in arguments
        },
        "analysis_completeness": {
            "total_components": 1,
            "scanned_components": 0 if is_partial else 1,
            "coverage_percent": 0.0 if is_partial else 100.0,
            "is_complete": not (is_partial or is_execution_failure),
            "status": "failed" if is_execution_failure else ("partial" if is_partial else "complete"),
            "execution_successful": not is_execution_failure,
            "fully_inspected_files": 0 if is_partial else 1,
            "partially_inspected_files": 0,
            "entirely_uninspected_files": 1 if is_partial else 0,
            "ledger_exceptions": (
                [{"reason_code": "fake_limit", "fatal": is_execution_failure}]
                if is_partial or is_execution_failure
                else []
            ),
            "scope_exclusions": [],
            "analyzer_statuses": (
                [{"analyzer_id": "fake_analyzer", "status": "degraded"}]
                if is_partial
                else []
            ),
            "references": [],
            "limitations": ["A fake component was not inspected."] if is_partial else [],
            "findings_before_filtering": len(issues),
            "findings_after_filtering": len(issues)
        },
        "execution_successful": not is_execution_failure
    }
    if is_transitive_truncated:
        raw["metadata"].update(
            {
                "transitive_targets_scanned": 1,
                "transitive_bytes_scanned": 128,
                "transitive_truncated": True,
                "transitive_truncation_reasons": ["target budget 1 reached"],
            }
        )
    output.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    if mode == "oversized_stdout":
        sys.stdout.buffer.write(b"x" * (5 * 1024 * 1024))
        sys.stdout.buffer.flush()
    print("fake scan complete")
    if is_execution_failure or is_error_with_raw:
        return 2
    return 1 if is_dangerous or is_partial or is_transitive_truncated else 0


if __name__ == "__main__":
    raise SystemExit(main())
