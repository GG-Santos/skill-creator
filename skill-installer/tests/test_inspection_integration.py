from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


INSTALLER_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SCRIPTS = INSTALLER_ROOT / "scripts"
WORKSPACE = INSTALLER_ROOT.parent
INSPECTOR_ROOT = WORKSPACE / "skill-inspector"
INSPECTOR_SCRIPT = INSPECTOR_ROOT / "scripts" / "run_inspection.py"
FAKE_SCANNER = INSPECTOR_ROOT / "tests" / "fixtures" / "fake_skillspector.py"

for directory in (INSTALLER_SCRIPTS, INSPECTOR_SCRIPT.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


installer = load_module(
    "codex_skill_installer_integration",
    INSTALLER_SCRIPTS / "install-skill-from-github.py",
)
inspector = load_module("skill_inspector_integration", INSPECTOR_SCRIPT)

import inspection_gate


class InspectionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _silenced_main(arguments: list[str]) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return installer.main(arguments)

    @staticmethod
    def _write_skill(parent: Path, name: str) -> Path:
        skill = parent / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: Integration fixture for the digest-bound security gate.\n"
            "---\n\n"
            "# Fixture\n\n"
            "This fixture is inert.\n",
            encoding="utf-8",
        )
        return skill

    def _prepare_plan(self, names: list[str]) -> tuple[Path, Path, Path]:
        repository = self.root / ("repository-" + "-".join(names))
        for name in names:
            self._write_skill(repository / "skills", name)
        destination = self.root / ("installed-" + "-".join(names))
        plan = self.root / ("plan-" + "-".join(names) + ".json")
        prepared = installer.PreparedRepo(str(repository), "download", "a" * 64)
        with mock.patch.object(installer, "_prepare_repo", return_value=prepared):
            code = self._silenced_main(
                [
                    "--repo",
                    "owner/repository",
                    "--path",
                    *(f"skills/{name}" for name in names),
                    "--dest",
                    str(destination),
                    "--prepare-only",
                    "--plan-output",
                    str(plan),
                ]
            )
        self.assertEqual(code, 0)
        return plan, plan.parent / f"{plan.name}.stage", destination

    def _final_report(self, target: Path, name: str, verdict: str) -> Path:
        report_dir = self.root / f"report-{name}-{verdict.lower()}"
        code, report_path = inspector.scan_target(
            target=target,
            output_dir=report_dir,
            scanner_command=[sys.executable, str(FAKE_SCANNER)],
            timeout_seconds=20,
        )
        self.assertEqual(code, 0)
        semantic = self.root / f"semantic-{name}-{verdict.lower()}.json"
        semantic.write_text(
            json.dumps(
                {
                    "verdict": verdict,
                    "summary": "The exact staged source was reviewed for the integration test.",
                    "reviewed_files": ["SKILL.md"],
                    "sensitive_surfaces": [],
                    "finding_judgments": [],
                    "guardrails": [],
                    "limitations": [],
                }
            ),
            encoding="utf-8",
        )
        inspector.finalize_report(report_path, semantic)
        return report_path

    def test_prepare_inspect_and_commit_installs_exact_bytes(self) -> None:
        plan, stage, destination = self._prepare_plan(["demo"])
        report = self._final_report(stage / "demo", "demo", "APPROVE")
        code = self._silenced_main(
            [
                "--commit-plan",
                str(plan),
                "--inspection-report",
                str(report),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            (destination / "demo" / "SKILL.md").read_bytes(),
            (stage / "demo" / "SKILL.md").read_bytes(),
        )

    def test_caution_requires_explicit_acceptance(self) -> None:
        plan, stage, destination = self._prepare_plan(["caution-demo"])
        report = self._final_report(stage / "caution-demo", "caution", "CAUTION")
        base = [
            "--commit-plan",
            str(plan),
            "--inspection-report",
            str(report),
        ]
        self.assertEqual(self._silenced_main(base), 1)
        self.assertFalse(destination.exists())
        self.assertEqual(self._silenced_main([*base, "--allow-caution"]), 0)
        self.assertTrue((destination / "caution-demo" / "SKILL.md").is_file())

    def test_stage_mutation_after_inspection_blocks_commit(self) -> None:
        plan, stage, destination = self._prepare_plan(["mutable-demo"])
        report = self._final_report(stage / "mutable-demo", "mutable", "APPROVE")
        (stage / "mutable-demo" / "SKILL.md").write_text(
            "changed after inspection", encoding="utf-8"
        )
        code = self._silenced_main(
            ["--commit-plan", str(plan), "--inspection-report", str(report)]
        )
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())

    def test_batch_rejection_installs_nothing(self) -> None:
        plan, stage, destination = self._prepare_plan(["first-demo", "second-demo"])
        first = self._final_report(stage / "first-demo", "first", "APPROVE")
        second = self._final_report(stage / "second-demo", "second", "REJECT")
        code = self._silenced_main(
            [
                "--commit-plan",
                str(plan),
                "--inspection-report",
                str(first),
                "--inspection-report",
                str(second),
                "--allow-caution",
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse((destination / "first-demo").exists())
        self.assertFalse((destination / "second-demo").exists())

    def test_raw_evidence_tampering_blocks_commit(self) -> None:
        plan, stage, destination = self._prepare_plan(["evidence-demo"])
        report = self._final_report(stage / "evidence-demo", "evidence", "APPROVE")
        (report.parent / inspector.RAW_REPORT_NAME).write_text("{}", encoding="utf-8")
        code = self._silenced_main(
            ["--commit-plan", str(plan), "--inspection-report", str(report)]
        )
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())

    def test_embedded_semantic_review_tampering_blocks_commit(self) -> None:
        plan, stage, destination = self._prepare_plan(["semantic-demo"])
        report = self._final_report(stage / "semantic-demo", "semantic", "APPROVE")
        forged = json.loads(report.read_text(encoding="utf-8"))
        forged["semantic_review"]["summary"] = "Changed after finalization."
        report.write_text(json.dumps(forged), encoding="utf-8")
        code = self._silenced_main(
            ["--commit-plan", str(plan), "--inspection-report", str(report)]
        )
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())

    def test_forged_semantic_shape_and_unfinalized_report_block_commit(self) -> None:
        plan, stage, destination = self._prepare_plan(["shape-demo"])
        report = self._final_report(stage / "shape-demo", "shape", "APPROVE")
        forged = json.loads(report.read_text(encoding="utf-8"))
        forged["semantic_review"] = {"status": "complete"}
        report.write_text(json.dumps(forged), encoding="utf-8")
        code = self._silenced_main(
            ["--commit-plan", str(plan), "--inspection-report", str(report)]
        )
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())

        second_plan, second_stage, second_destination = self._prepare_plan(
            ["pending-demo"]
        )
        scan_code, pending_report = inspector.scan_target(
            target=second_stage / "pending-demo",
            output_dir=self.root / "report-pending",
            scanner_command=[sys.executable, str(FAKE_SCANNER)],
            timeout_seconds=20,
        )
        self.assertEqual(scan_code, 0)
        code = self._silenced_main(
            [
                "--commit-plan",
                str(second_plan),
                "--inspection-report",
                str(pending_report),
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(second_destination.exists())

    def test_high_suppressed_evidence_cannot_be_overridden(self) -> None:
        plan, stage, destination = self._prepare_plan(["suppressed-critical"])
        report = self._final_report(
            stage / "suppressed-critical", "suppressed-critical", "CAUTION"
        )
        code = self._silenced_main(
            [
                "--commit-plan",
                str(plan),
                "--inspection-report",
                str(report),
                "--allow-caution",
            ]
        )
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())

    def test_direct_matching_report_can_gate_noncurated_install(self) -> None:
        repository = self.root / "direct-repository"
        skill = self._write_skill(repository / "skills", "direct-demo")
        report = self._final_report(skill, "direct", "APPROVE")
        destination = self.root / "direct-installed"
        prepared = installer.PreparedRepo(str(repository), "download", "d" * 64)
        with mock.patch.object(installer, "_prepare_repo", return_value=prepared):
            code = self._silenced_main(
                [
                    "--repo",
                    "owner/direct",
                    "--path",
                    "skills/direct-demo",
                    "--dest",
                    str(destination),
                    "--inspection-report",
                    str(report),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue((destination / "direct-demo" / "SKILL.md").is_file())

    def test_noncurated_auto_policy_never_discovers_target_shipped_report(self) -> None:
        repository = self.root / "untrusted-repository"
        skill = self._write_skill(repository / "skills", "untrusted-demo")
        (skill / "inspection.v1.json").write_text(
            '{"schema_version":"skill-inspection/v1","gate_decision":"ALLOW"}',
            encoding="utf-8",
        )
        destination = self.root / "untrusted-installed"
        prepared = installer.PreparedRepo(str(repository), "download", "b" * 64)
        with mock.patch.object(installer, "_prepare_repo", return_value=prepared):
            code = self._silenced_main(
                [
                    "--repo",
                    "unknown/source",
                    "--path",
                    "skills/untrusted-demo",
                    "--dest",
                    str(destination),
                ]
            )
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())

    def test_official_curated_auto_policy_can_install_without_report(self) -> None:
        repository = self.root / "curated-repository"
        self._write_skill(repository / "skills" / ".curated", "curated-demo")
        destination = self.root / "curated-installed"
        prepared = installer.PreparedRepo(str(repository), "download", "c" * 64)
        with mock.patch.object(installer, "_prepare_repo", return_value=prepared):
            code = self._silenced_main(
                [
                    "--repo",
                    "openai/skills",
                    "--path",
                    "skills/.curated/curated-demo",
                    "--dest",
                    str(destination),
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue((destination / "curated-demo" / "SKILL.md").is_file())

    def test_inspector_and_installer_share_the_same_tree_identity(self) -> None:
        target = self._write_skill(self.root / "identity", "identity-demo")
        (target / "empty-directory").mkdir()
        inspector_identity = inspector.hash_target(target)
        installer_identity = inspection_gate.hash_skill_tree(target)
        self.assertEqual(inspector_identity["algorithm"], installer_identity.algorithm)
        self.assertEqual(inspector_identity["digest"], installer_identity.digest)
        self.assertEqual(inspector_identity["entries"], installer_identity.entries)
        self.assertEqual(inspector_identity["bytes"], installer_identity.bytes)


if __name__ == "__main__":
    unittest.main()
