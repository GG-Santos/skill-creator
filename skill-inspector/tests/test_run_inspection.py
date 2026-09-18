from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_inspection.py"
FAKE_SCANNER = ROOT / "tests" / "fixtures" / "fake_skillspector.py"
TARGETS = ROOT / "tests" / "fixtures" / "targets"

SPEC = importlib.util.spec_from_file_location("skill_inspector_runner", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to import {SCRIPT}")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class InspectionAdapterTests(unittest.TestCase):
    def scanner_command(self) -> list[str]:
        return [sys.executable, str(FAKE_SCANNER)]

    def scan(
        self,
        target_name: str,
        output_dir: Path,
        *,
        use_llm: bool = False,
    ) -> tuple[int, Path]:
        return RUNNER.scan_target(
            target=TARGETS / target_name,
            output_dir=output_dir,
            scanner_command=self.scanner_command(),
            use_llm=use_llm,
            timeout_seconds=20,
        )

    @staticmethod
    def read_report(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_semantic(
        path: Path,
        *,
        verdict: str,
        judgments: list[dict[str, str]] | None = None,
    ) -> None:
        value = {
            "verdict": verdict,
            "summary": "Direct source review completed for the test fixture.",
            "reviewed_files": ["SKILL.md"],
            "sensitive_surfaces": [],
            "finding_judgments": judgments or [],
            "guardrails": [],
            "limitations": [],
        }
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_safe_scan_is_static_shell_free_and_pending_until_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report"
            real_popen = RUNNER.subprocess.Popen
            calls: list[dict[str, object]] = []

            def capture(*args: object, **kwargs: object) -> object:
                calls.append(dict(kwargs))
                return real_popen(*args, **kwargs)

            with mock.patch.object(RUNNER.subprocess, "Popen", side_effect=capture):
                code, report_path = self.scan("safe", output)

            self.assertEqual(code, 0)
            self.assertTrue(calls)
            self.assertTrue(all(call.get("shell") is False for call in calls))
            report = self.read_report(report_path)
            self.assertEqual(report["schema_version"], "skill-inspection/v1")
            self.assertEqual(report["status"], "pending_semantic_review")
            self.assertEqual(report["gate_decision"], "BLOCK_PENDING_REVIEW")
            self.assertEqual(report["scanner"]["exit_code"], 0)
            self.assertEqual(report["scanner"]["mode"], "static")
            self.assertTrue(report["scanner"]["analysis_complete"])
            self.assertTrue(report["upstream"]["metadata"]["fake_received_fail_on_incomplete"])
            self.assertFalse(report["upstream"]["metadata"]["llm_requested"])
            self.assertEqual(report["upstream"]["risk_assessment"]["max_issue_severity"], "NONE")
            self.assertTrue(report["upstream"]["execution_successful"])
            self.assertEqual(
                report["upstream"]["analysis_completeness"]["coverage_percent"], 100.0
            )
            self.assertTrue(report["data_egress"]["dependency_coordinates"]["may_leave_machine"])
            self.assertFalse(report["data_egress"]["target_file_contents"]["may_leave_machine"])

            raw_path = output / "skillspector.raw.json"
            raw_before = raw_path.read_bytes()
            self.assertEqual(
                report["artifacts"]["raw_report_sha256"], hashlib.sha256(raw_before).hexdigest()
            )
            semantic_path = Path(temporary) / "semantic.json"
            self.write_semantic(semantic_path, verdict="APPROVE")
            finalize_code, _ = RUNNER.finalize_report(report_path, semantic_path)
            self.assertEqual(finalize_code, 0)
            final = self.read_report(report_path)
            self.assertEqual(final["status"], "complete")
            self.assertEqual(final["combined_verdict"], "APPROVE")
            self.assertEqual(final["gate_decision"], "ALLOW")
            self.assertEqual(raw_path.read_bytes(), raw_before)
            validation_code, _ = RUNNER.validate_report(report_path, TARGETS / "safe")
            self.assertEqual(validation_code, 0)

    def test_upstream_exit_one_is_preserved_as_completed_adverse_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report"
            code, report_path = self.scan("dangerous", output)
            self.assertEqual(code, 0)
            report = self.read_report(report_path)
            self.assertEqual(report["scanner"]["exit_code"], 1)
            self.assertEqual(report["status"], "pending_semantic_review")
            self.assertEqual(report["upstream"]["risk_assessment"]["recommendation"], "DO_NOT_INSTALL")
            self.assertEqual(report["upstream"]["risk_assessment"]["max_issue_severity"], "CRITICAL")

            semantic_path = Path(temporary) / "semantic.json"
            self.write_semantic(
                semantic_path,
                verdict="REJECT",
                judgments=[
                    {
                        "id": "fake-credential-exfiltration",
                        "disposition": "unresolved",
                        "rationale": "The fixture directs credential exfiltration in SKILL.md:8."
                    }
                ],
            )
            finalize_code, _ = RUNNER.finalize_report(report_path, semantic_path)
            self.assertEqual(finalize_code, 1)
            final = self.read_report(report_path)
            self.assertEqual(final["status"], "complete")
            self.assertEqual(final["combined_verdict"], "REJECT")
            self.assertEqual(final["gate_decision"], "BLOCK")

    def test_incomplete_exit_one_retains_coverage_and_cannot_approve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report"
            code, report_path = self.scan("partial", output)
            self.assertEqual(code, 0)
            report = self.read_report(report_path)
            self.assertEqual(report["scanner"]["exit_code"], 1)
            self.assertFalse(report["scanner"]["analysis_complete"])
            self.assertEqual(report["status"], "partial")
            completeness = report["upstream"]["analysis_completeness"]
            self.assertFalse(completeness["is_complete"])
            self.assertEqual(completeness["coverage_percent"], 0.0)
            self.assertEqual(completeness["entirely_uninspected_files"], 1)
            self.assertEqual(completeness["analyzer_statuses"][0]["status"], "degraded")
            self.assertEqual(completeness["limitations"], ["A fake component was not inspected."])

            approve_path = Path(temporary) / "approve.json"
            self.write_semantic(approve_path, verdict="APPROVE")
            with self.assertRaisesRegex(RUNNER.InspectionError, "APPROVE is invalid"):
                RUNNER.finalize_report(report_path, approve_path)

            caution_path = Path(temporary) / "caution.json"
            self.write_semantic(caution_path, verdict="CAUTION")
            finalize_code, _ = RUNNER.finalize_report(report_path, caution_path)
            self.assertEqual(finalize_code, 1)
            final = self.read_report(report_path)
            self.assertEqual(final["status"], "partial")
            self.assertEqual(final["combined_verdict"], "CAUTION")
            self.assertEqual(final["gate_decision"], "PROMPT")

    def test_transitive_truncation_is_incomplete_even_with_full_local_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, report_path = self.scan(
                "transitive_truncated", Path(temporary) / "report"
            )
            self.assertEqual(code, 0)
            report = self.read_report(report_path)
            self.assertEqual(report["scanner"]["exit_code"], 1)
            self.assertFalse(report["scanner"]["analysis_complete"])
            self.assertEqual(report["status"], "partial")
            metadata = report["upstream"]["metadata"]
            self.assertTrue(metadata["transitive_truncated"])
            self.assertEqual(
                metadata["transitive_truncation_reasons"],
                ["target budget 1 reached"],
            )
            self.assertEqual(
                report["upstream"]["analysis_completeness"]["coverage_percent"],
                100.0,
            )

            approve = Path(temporary) / "approve.json"
            self.write_semantic(approve, verdict="APPROVE")
            with self.assertRaisesRegex(RUNNER.InspectionError, "APPROVE is invalid"):
                RUNNER.finalize_report(report_path, approve)

    def test_execution_failure_with_raw_json_is_retained_and_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, report_path = self.scan("execution_failure", Path(temporary) / "report")
            self.assertEqual(code, 2)
            report = self.read_report(report_path)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["gate_decision"], "BLOCK_PENDING_REVIEW")
            self.assertFalse(report["upstream"]["execution_successful"])
            self.assertFalse(report["upstream"]["analysis_completeness"]["execution_successful"])
            self.assertIsNotNone(report["artifacts"]["raw_report_sha256"])

    def test_suppression_and_llm_metadata_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, report_path = self.scan("suppressed", Path(temporary) / "report")
            self.assertEqual(code, 0)
            report = self.read_report(report_path)
            upstream = report["upstream"]
            self.assertEqual(upstream["suppressed_count"], 1)
            self.assertEqual(upstream["suppressed"][0]["id"], "PI1")
            self.assertIn("meta_analysis_applied", upstream["metadata"])
            self.assertIn("inference_usage", upstream["metadata"])

    def test_malformed_raw_report_is_preserved_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report"
            code, report_path = self.scan("malformed", output)
            self.assertEqual(code, 2)
            report = self.read_report(report_path)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["gate_decision"], "BLOCK_PENDING_REVIEW")
            self.assertEqual((output / "skillspector.raw.json").read_text(), "{not valid json")
            self.assertIsNotNone(report["artifacts"]["raw_report_sha256"])

    def test_missing_scanner_supports_only_caution_manual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report"
            code, report_path = RUNNER.scan_target(
                target=TARGETS / "safe",
                output_dir=output,
                scanner_name="skillspector-command-that-does-not-exist-for-test",
            )
            self.assertEqual(code, 2)
            report = self.read_report(report_path)
            self.assertFalse(report["scanner"]["available"])
            self.assertIsNone(report["artifacts"]["raw_report"])

            approve_path = Path(temporary) / "approve.json"
            self.write_semantic(approve_path, verdict="APPROVE")
            with self.assertRaisesRegex(RUNNER.InspectionError, "APPROVE is invalid"):
                RUNNER.finalize_report(report_path, approve_path)

            caution_path = Path(temporary) / "caution.json"
            self.write_semantic(caution_path, verdict="CAUTION")
            finalize_code, _ = RUNNER.finalize_report(report_path, caution_path)
            self.assertEqual(finalize_code, 1)
            final = self.read_report(report_path)
            self.assertEqual(final["status"], "partial")
            self.assertEqual(final["gate_decision"], "PROMPT")

    def test_explicit_llm_mode_records_content_egress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, report_path = self.scan("safe", Path(temporary) / "report", use_llm=True)
            self.assertEqual(code, 0)
            report = self.read_report(report_path)
            self.assertEqual(report["scanner"]["mode"], "llm")
            self.assertTrue(report["upstream"]["metadata"]["llm_requested"])
            self.assertTrue(report["data_egress"]["target_file_contents"]["may_leave_machine"])

    def test_target_change_invalidates_semantic_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            target = temporary_path / "target-copy"
            shutil.copytree(TARGETS / "safe", target)
            code, report_path = RUNNER.scan_target(
                target=target,
                output_dir=temporary_path / "report",
                scanner_command=self.scanner_command(),
            )
            self.assertEqual(code, 0)
            (target / "SKILL.md").write_text("changed after scan", encoding="utf-8")
            semantic_path = temporary_path / "semantic.json"
            self.write_semantic(semantic_path, verdict="APPROVE")
            with self.assertRaisesRegex(RUNNER.InspectionError, "changed after scanning"):
                RUNNER.finalize_report(report_path, semantic_path)

    def test_output_inside_target_and_symbolic_links_fail_before_execution(self) -> None:
        with self.assertRaisesRegex(RUNNER.InspectionError, "outside"):
            RUNNER.scan_target(
                target=TARGETS / "safe",
                output_dir=TARGETS / "safe" / "report",
                scanner_command=self.scanner_command(),
            )

    def test_error_exit_two_with_parseable_json_can_never_allow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, report_path = self.scan("error_with_raw", Path(temporary) / "report")
            self.assertEqual(code, 2)
            report = self.read_report(report_path)
            self.assertEqual(report["scanner"]["exit_code"], 2)
            self.assertIsNotNone(report["upstream"])
            approve = Path(temporary) / "approve.json"
            self.write_semantic(approve, verdict="APPROVE")
            with self.assertRaisesRegex(RUNNER.InspectionError, "APPROVE is invalid"):
                RUNNER.finalize_report(report_path, approve)
            caution = Path(temporary) / "caution.json"
            self.write_semantic(caution, verdict="CAUTION")
            final_code, _ = RUNNER.finalize_report(report_path, caution)
            self.assertEqual(final_code, 1)
            final = self.read_report(report_path)
            self.assertEqual(final["status"], "partial")
            self.assertEqual(final["gate_decision"], "PROMPT")

    def test_lowercase_suppressed_critical_evidence_forces_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, report_path = self.scan(
                "suppressed_critical", Path(temporary) / "report"
            )
            self.assertEqual(code, 0)
            report = self.read_report(report_path)
            self.assertEqual(report["upstream"]["suppressed"][0]["severity"], "CRITICAL")
            semantic = Path(temporary) / "semantic.json"
            self.write_semantic(semantic, verdict="CAUTION")
            final_code, _ = RUNNER.finalize_report(report_path, semantic)
            self.assertEqual(final_code, 1)
            final = self.read_report(report_path)
            self.assertEqual(final["combined_verdict"], "REJECT")
            self.assertEqual(final["gate_decision"], "BLOCK")

    def test_static_environment_scrubs_secrets_and_tracing(self) -> None:
        source = {
            "PATH": os.environ.get("PATH", ""),
            "OPENAI_API_KEY": "secret",
            "LANGSMITH_API_KEY": "trace-secret",
            "LANGSMITH_TRACING": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://trace.invalid",
            "UNRELATED_SECRET": "also-secret",
        }
        with mock.patch.dict(RUNNER.os.environ, source, clear=True):
            static_environment, removed = RUNNER._scanner_environment(False)
            llm_environment, _ = RUNNER._scanner_environment(True)
        self.assertNotIn("OPENAI_API_KEY", static_environment)
        self.assertNotIn("UNRELATED_SECRET", static_environment)
        self.assertIn("OPENAI_API_KEY", removed)
        self.assertEqual(static_environment["LANGSMITH_TRACING"], "false")
        self.assertEqual(static_environment["OTEL_SDK_DISABLED"], "true")
        self.assertEqual(llm_environment["OPENAI_API_KEY"], "secret")
        self.assertNotIn("UNRELATED_SECRET", llm_environment)
        self.assertNotIn("LANGSMITH_API_KEY", llm_environment)
        self.assertEqual(llm_environment["LANGSMITH_TRACING"], "false")

    def test_path_lookup_cannot_select_scanner_from_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "skillspector.exe"
            local.write_bytes(b"not an executable")
            with mock.patch.object(RUNNER.shutil, "which", return_value=str(local)):
                with mock.patch.object(RUNNER.Path, "cwd", return_value=Path(temporary)):
                    with self.assertRaisesRegex(RUNNER.InspectionError, "current working"):
                        RUNNER._resolve_scanner("skillspector")

    def test_windows_batch_scanner_is_rejected(self) -> None:
        with mock.patch.object(RUNNER.os, "name", "nt"):
            with self.assertRaisesRegex(RUNNER.InspectionError, "batch-file"):
                RUNNER._validate_scanner_executable(Path("C:/trusted/skillspector.cmd"))

    def test_file_target_is_not_overwritten_by_artifact_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "existing-output"
            parent.mkdir()
            target = parent / RUNNER.STDOUT_NAME
            target.write_text("must survive", encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.InspectionError, "new path"):
                RUNNER.scan_target(
                    target=target,
                    output_dir=parent,
                    scanner_command=self.scanner_command(),
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "must survive")

    def test_ancestor_symlink_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_parent = root / "real"
            target = real_parent / "skill"
            shutil.copytree(TARGETS / "safe", target)
            alias = root / "alias"
            try:
                alias.symlink_to(real_parent, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            with self.assertRaisesRegex(RUNNER.InspectionError, "component"):
                RUNNER.scan_target(
                    target=alias / "skill",
                    output_dir=root / "report",
                    scanner_command=self.scanner_command(),
                )

    def test_tree_identity_covers_empty_directories_and_executable_bits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            shutil.copytree(TARGETS / "safe", first)
            shutil.copytree(TARGETS / "safe", second)
            (first / "empty-a").mkdir()
            (second / "empty-b").mkdir()
            self.assertNotEqual(
                RUNNER.hash_target(first)["digest"], RUNNER.hash_target(second)["digest"]
            )

            executable = first / "tool.py"
            executable.write_text("print('safe')\n", encoding="utf-8")
            before = RUNNER.hash_target(first)["digest"]
            original_mode = executable.stat().st_mode
            executable.chmod(original_mode | stat.S_IXUSR)
            after = RUNNER.hash_target(first)["digest"]
            if executable.stat().st_mode & stat.S_IXUSR == original_mode & stat.S_IXUSR:
                self.skipTest("this filesystem does not expose executable-bit changes")
            self.assertNotEqual(before, after)

    def test_completeness_cross_field_inconsistencies_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, _ = self.scan("safe", Path(temporary) / "report")
            self.assertEqual(code, 0)
            raw_path = Path(temporary) / "report" / RUNNER.RAW_REPORT_NAME
            baseline = json.loads(raw_path.read_text(encoding="utf-8"))
            mutations = (
                ("ledger_exceptions", [{"fatal": False}]),
                ("limitations", ["missing coverage"]),
                ("analyzer_statuses", [{"analyzer_id": "fake", "status": "degraded"}]),
                ("scanned_components", 0),
            )
            for field, value in mutations:
                with self.subTest(field=field):
                    candidate = json.loads(json.dumps(baseline))
                    candidate["analysis_completeness"][field] = value
                    candidate["risk_assessment"]["recommendation"] = "CAUTION"
                    normalized = RUNNER.normalize_upstream(candidate)
                    self.assertFalse(normalized["analysis_complete"])

    def test_nvidia_2112_risk_band_boundaries_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, _ = self.scan("safe", Path(temporary) / "report")
            self.assertEqual(code, 0)
            raw_path = Path(temporary) / "report" / RUNNER.RAW_REPORT_NAME
            baseline = json.loads(raw_path.read_text(encoding="utf-8"))
            cases = (
                (0, "LOW", "SAFE"),
                (20, "LOW", "SAFE"),
                (21, "MEDIUM", "CAUTION"),
                (50, "MEDIUM", "CAUTION"),
                (51, "HIGH", "DO_NOT_INSTALL"),
                (80, "HIGH", "DO_NOT_INSTALL"),
                (81, "CRITICAL", "DO_NOT_INSTALL"),
                (100, "CRITICAL", "DO_NOT_INSTALL"),
            )
            for score, severity, recommendation in cases:
                with self.subTest(score=score):
                    candidate = json.loads(json.dumps(baseline))
                    candidate["risk_assessment"].update(
                        {
                            "score": score,
                            "severity": severity,
                            "recommendation": recommendation,
                        }
                    )
                    normalized = RUNNER.normalize_upstream(candidate)
                    self.assertEqual(
                        normalized["risk_assessment"]["severity"], severity
                    )

    def test_finding_identity_supports_occurrence_rows_and_preserves_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, _ = self.scan("safe", Path(temporary) / "report")
            self.assertEqual(code, 0)
            raw_path = Path(temporary) / "report" / RUNNER.RAW_REPORT_NAME
            raw = json.loads(raw_path.read_text(encoding="utf-8"))

            def finding(finding_id: str) -> dict[str, object]:
                return {
                    "id": "SC4",
                    "finding_id": finding_id,
                    "category": "Supply Chain",
                    "severity": "LOW",
                    "confidence": 0.8,
                    "location": {"file": "SKILL.md", "start_line": 1, "end_line": 1},
                    "finding": "Dependency coordinate",
                    "explanation": "Evidence retained",
                    "remediation": "Pin dependencies",
                    "tags": ["activation-metadata"],
                    "evidence": {"package": "example"},
                    "match_fingerprint": finding_id,
                    "occurrences": [
                        {"file": "SKILL.md", "start_line": 1, "end_line": 1}
                    ],
                }

            second_occurrence = finding("finding-a")
            second_occurrence["location"] = {
                "file": "SKILL.md",
                "start_line": 2,
                "end_line": 2,
            }
            raw["issues"] = [
                finding("finding-a"),
                finding("finding-b"),
                second_occurrence,
            ]
            raw["risk_assessment"].update(
                {"score": 10, "severity": "LOW", "recommendation": "SAFE", "max_issue_severity": "LOW"}
            )
            raw["analysis_completeness"]["findings_before_filtering"] = 2
            raw["analysis_completeness"]["findings_after_filtering"] = 2
            normalized = RUNNER.normalize_upstream(raw)
            severities, conflicts = RUNNER._issue_ids({"upstream": normalized})
            self.assertEqual(severities, {"finding-a": "LOW", "finding-b": "LOW"})
            self.assertFalse(conflicts)
            self.assertEqual(normalized["issues"][0]["tags"], ["activation-metadata"])
            self.assertEqual(normalized["issues"][0]["evidence"]["package"], "example")

            raw["issues"][-1]["severity"] = "MEDIUM"
            raw["risk_assessment"]["max_issue_severity"] = "MEDIUM"
            with self.assertRaisesRegex(RUNNER.InspectionError, "conflicting severities"):
                RUNNER.normalize_upstream(raw)

    def test_output_and_raw_report_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, report_path = self.scan("oversized_stdout", root / "stdout-report")
            self.assertEqual(code, 2)
            report = self.read_report(report_path)
            self.assertIn("log limit", report["failure"]["message"])
            self.assertLessEqual(
                (root / "stdout-report" / RUNNER.STDOUT_NAME).stat().st_size,
                RUNNER.MAX_LOG_BYTES,
            )
            with mock.patch.object(RUNNER, "MAX_REPORT_BYTES", 1024):
                code, report_path = self.scan("oversized_raw", root / "raw-report")
            self.assertEqual(code, 2)
            report = self.read_report(report_path)
            self.assertIn("raw report exceeded", report["failure"]["message"])

    def test_short_timeout_stops_scanner_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            code, report_path = RUNNER.scan_target(
                target=TARGETS / "slow",
                output_dir=Path(temporary) / "report",
                scanner_command=self.scanner_command(),
                timeout_seconds=0.1,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(code, 2)
            self.assertLess(elapsed, 4)
            self.assertIn("timeout", self.read_report(report_path)["failure"]["message"])

    def test_validate_requires_fresh_target_and_complete_semantic_shape_for_allow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, report_path = self.scan("safe", Path(temporary) / "report")
            self.assertEqual(code, 0)
            semantic = Path(temporary) / "semantic.json"
            self.write_semantic(semantic, verdict="APPROVE")
            RUNNER.finalize_report(report_path, semantic)
            with self.assertRaisesRegex(RUNNER.InspectionError, "requires --target"):
                RUNNER.validate_report(report_path)
            forged = self.read_report(report_path)
            forged["semantic_review"] = {"status": "complete"}
            report_path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.InspectionError, "Semantic review"):
                RUNNER.validate_report(report_path, TARGETS / "safe")


if __name__ == "__main__":
    unittest.main()
