from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_evals
import catalog_skills
import init_eval_workspace
import init_skill
import package_skill
import scan_skill_opportunities
import skill_validation

VIEWER_PATH = Path(__file__).resolve().parents[1] / "eval-viewer" / "generate_review.py"
VIEWER_SPEC = importlib.util.spec_from_file_location("skill_creator_eval_viewer", VIEWER_PATH)
assert VIEWER_SPEC and VIEWER_SPEC.loader
eval_viewer = importlib.util.module_from_spec(VIEWER_SPEC)
VIEWER_SPEC.loader.exec_module(eval_viewer)


class CreatorToolingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _valid_skill(self, name: str = "demo-skill") -> Path:
        skill = self.root / name
        (skill / "agents").mkdir(parents=True)
        (skill / "scripts").mkdir()
        (skill / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: Review demo inputs when a user explicitly requests the demo workflow.\n"
            "---\n\n"
            "# Demo Skill\n\nInspect the input and return the requested result.\n",
            encoding="utf-8",
        )
        (skill / "agents" / "openai.yaml").write_text(
            "interface:\n"
            "  display_name: \"Demo Skill\"\n"
            "  short_description: \"Review inputs with the demo workflow\"\n"
            f"  default_prompt: \"Use ${name} for this request.\"\n",
            encoding="utf-8",
        )
        (skill / "scripts" / "helper.py").write_text(
            "def answer():\n    return 42\n", encoding="utf-8"
        )
        return skill

    def test_quick_validation_rejects_empty_required_fields(self) -> None:
        skill = self.root / "empty-fields"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: \"\"\ndescription: \"\"\n---\n\n# Empty\n",
            encoding="utf-8",
        )
        report = skill_validation.validate_path(skill, deep=False)
        self.assertFalse(report.passed())
        self.assertEqual(
            {finding.code for finding in report.errors},
            {"name-required", "description-required"},
        )

    def test_folder_name_must_match_frontmatter(self) -> None:
        skill = self._valid_skill("folder-name")
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        (skill / "SKILL.md").write_text(
            text.replace("name: folder-name", "name: different-name"),
            encoding="utf-8",
        )
        report = skill_validation.validate_path(skill, deep=False)
        self.assertIn("folder-name", {finding.code for finding in report.errors})

    def test_deep_strict_validation_accepts_valid_skill(self) -> None:
        report = skill_validation.validate_path(self._valid_skill(), deep=True)
        self.assertTrue(report.passed(strict=True), skill_validation.format_text_report(report, True))

    def test_deep_validation_rejects_links_outside_skill(self) -> None:
        skill = self._valid_skill()
        (self.root / "outside.md").write_text("outside", encoding="utf-8")
        with (skill / "SKILL.md").open("a", encoding="utf-8") as handle:
            handle.write("\n[Outside](../outside.md)\n")
        report = skill_validation.validate_path(skill, deep=True)
        self.assertIn("link-outside-skill", {finding.code for finding in report.errors})

    def test_secret_scan_covers_dotenv_and_reports_skipped_large_text(self) -> None:
        skill = self._valid_skill()
        (skill / ".env").write_text(
            "AWS_ACCESS_KEY_ID=" + "AK" + "IA" + "1234567890ABCDEF\n",
            encoding="utf-8",
        )
        (skill / "large.csv").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        report = skill_validation.validate_path(skill, deep=True)
        codes = {finding.code for finding in report.findings}
        self.assertIn("possible-secret", codes)
        self.assertIn("secret-scan-skipped", codes)

    def test_initializer_rolls_back_after_generation_failure(self) -> None:
        target = self.root / "rollback-skill"
        with mock.patch.object(
            init_skill, "write_openai_yaml", side_effect=RuntimeError("injected failure")
        ), redirect_stdout(io.StringIO()):
            result = init_skill.init_skill(
                "rollback-skill", self.root, [], False, []
            )
        self.assertIsNone(result)
        self.assertFalse(target.exists())
        self.assertEqual([], list(self.root.glob(".rollback-skill-*")))

    def test_public_cli_initializes_and_fails_closed_on_placeholders(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        created = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "init_skill.py"),
                "cli-smoke-skill",
                "--path",
                str(self.root),
                "--resources",
                "scripts,references,assets,evals",
                "--examples",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(0, created.returncode, created.stderr + created.stdout)
        skill = self.root / "cli-smoke-skill"
        quick = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "quick_validate.py"), str(skill)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(1, quick.returncode)
        deep = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "validate_skill.py"),
                str(skill),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        self.assertEqual(1, deep.returncode)
        report = json.loads(deep.stdout)
        self.assertFalse(report["passed"])
        self.assertIn(
            "body-placeholder", {finding["code"] for finding in report["findings"]}
        )

    def test_blueprint_defaults_are_merged_and_listed(self) -> None:
        with redirect_stdout(io.StringIO()):
            skill = init_skill.init_skill(
                "script-blueprint",
                self.root,
                ["evals"],
                False,
                [],
                "script-backed",
            )
        self.assertIsNotNone(skill)
        self.assertTrue((skill / "scripts").is_dir())
        self.assertTrue((skill / "references").is_dir())
        self.assertTrue((skill / "evals").is_dir())
        body = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("# Script Blueprint", body)
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / "init_skill.py"), "--list-blueprints"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("instruction-only: none", completed.stdout)
        self.assertIn("evaluation-ready: evals,references", completed.stdout)

    def test_every_blueprint_initializes_with_declared_resources(self) -> None:
        for blueprint, defaults in init_skill.BLUEPRINT_RESOURCES.items():
            with self.subTest(blueprint=blueprint), redirect_stdout(io.StringIO()):
                skill = init_skill.init_skill(
                    f"blueprint-{blueprint}",
                    self.root,
                    [],
                    False,
                    [],
                    blueprint,
                )
            self.assertIsNotNone(skill)
            self.assertIn(
                f"name: blueprint-{blueprint}",
                (skill / "SKILL.md").read_text(encoding="utf-8"),
            )
            for resource in defaults:
                self.assertTrue((skill / resource).is_dir())
            report = skill_validation.validate_path(skill, deep=False)
            self.assertIn("body-placeholder", {finding.code for finding in report.errors})

    def test_catalog_reports_duplicate_names_without_claiming_equivalence(self) -> None:
        first_root = self.root / "first"
        second_root = self.root / "second"
        for root in (first_root, second_root):
            skill = root / "same-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: same-skill\n"
                "description: Build a retained evidence report for repeated workflow requests.\n"
                "---\n\n# Same Skill\n\nFollow the declared workflow.\n",
                encoding="utf-8",
            )
        report = catalog_skills.build_catalog([first_root, second_root])
        self.assertEqual(2, report["skill_count"])
        self.assertEqual(1, len(report["duplicate_names"]))
        self.assertEqual("same-skill", report["duplicate_names"][0]["normalized_name"])
        self.assertIn("review lead", " ".join(report["notes"]).lower())

    def test_opportunity_scanner_is_explicit_and_redacts_snippets(self) -> None:
        history = self.root / "history.json"
        history.write_text(
            json.dumps(
                [
                    {"request": "Please export the weekly sales report to CSV for person@example.com"},
                    {"correction": "Redo the weekly sales report export as CSV; the columns were wrong"},
                    {"request": "Export this weekly sales report to CSV with the required columns"},
                ]
            ),
            encoding="utf-8",
        )
        report = scan_skill_opportunities.find_opportunities(
            [history], min_recurrence=2, similarity=0.2, include_snippets=True
        )
        self.assertGreaterEqual(report["candidate_count"], 1)
        serialized = json.dumps(report)
        self.assertNotIn("person@example.com", serialized)
        self.assertIn("<email>", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertEqual(["source-1"], report["sources"])
        self.assertEqual("content-always-on; local-paths-opt-in", report["redaction"])
        self.assertIn(
            report["candidates"][0]["recommended_action"],
            {"create", "augment", "connect-or-create-orchestrator"},
        )

    def test_opportunity_scanner_source_paths_are_explicit_opt_in(self) -> None:
        history = self.root / "private-customer-history.md"
        history.write_text(
            "- Export the weekly report as CSV\n- Redo the weekly report as CSV\n",
            encoding="utf-8",
        )
        report = scan_skill_opportunities.find_opportunities(
            [history], min_recurrence=2, similarity=0.2, include_source_paths=True
        )
        self.assertEqual([str(history.resolve())], report["sources"])
        self.assertTrue(report["source_paths_included"])

    def test_opportunity_scanner_hides_catalog_paths_by_default(self) -> None:
        catalog_root = self.root / "private-catalog"
        skill = catalog_root / "weekly-report"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: weekly-report\n"
            "description: Export recurring weekly sales reports to CSV.\n"
            "---\n\n# Weekly Report\n\nExport the report.\n",
            encoding="utf-8",
        )
        history = self.root / "private-history.md"
        history.write_text(
            "- Export the weekly sales report to CSV\n"
            "- Redo the weekly sales report CSV export\n",
            encoding="utf-8",
        )

        report = scan_skill_opportunities.find_opportunities(
            [history], [catalog_root], min_recurrence=2, similarity=0.2
        )

        self.assertEqual("augment", report["candidates"][0]["recommended_action"])
        self.assertEqual(
            {"name": "weekly-report"}, report["candidates"][0]["target_skill"]
        )
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_opportunity_scanner_accepts_plain_text_bullets(self) -> None:
        history = self.root / "history.md"
        history.write_text(
            "- Export the weekly inventory report as CSV\n"
            "- Please export the weekly inventory report to CSV\n"
            "- Redo the weekly inventory report CSV export\n",
            encoding="utf-8",
        )
        report = scan_skill_opportunities.find_opportunities(
            [history], min_recurrence=2, similarity=0.2
        )
        self.assertEqual(3, report["signal_count"])
        self.assertGreaterEqual(report["candidate_count"], 1)

    def test_eval_workspace_initializer_creates_isolated_run_plan(self) -> None:
        skill = self._valid_skill("evaluation-skill")
        (skill / "evals").mkdir()
        (skill / "evals" / "fixture.txt").write_text("input", encoding="utf-8")
        (skill / "evals" / "evals.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "skill": "evaluation-skill",
                    "scenarios": [
                        {
                            "id": "core-case",
                            "prompt": "Run the evaluated workflow.",
                            "files": ["evals/fixture.txt"],
                            "should_trigger": True,
                            "risk": "low",
                            "holdout": False,
                            "assertions": [
                                {
                                    "id": "artifact",
                                    "kind": "outcome",
                                    "description": "The expected artifact exists.",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        target = init_eval_workspace.initialize_workspace(
            skill, self.root / "evaluation-workspace", iteration=2, runs_per_variant=2
        )
        plan = json.loads((target / "run-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(4, len(plan["runs"]))
        self.assertEqual(["baseline", "with_skill"], plan["variants"])
        self.assertEqual(64, len(plan["skill_md_sha256"]))
        for run in plan["runs"]:
            self.assertTrue((target / run["outputs_dir"]).is_dir())
        self.assertEqual(
            "input",
            (target / "fixtures" / "core-case" / "evals" / "fixture.txt").read_text(
                encoding="utf-8"
            ),
        )

    def test_eval_workspace_rejects_fixture_escape(self) -> None:
        skill = self._valid_skill("unsafe-eval-skill")
        (skill / "evals").mkdir()
        (skill / "evals" / "evals.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "skill": "unsafe-eval-skill",
                    "scenarios": [
                        {
                            "id": "unsafe",
                            "prompt": "Use an unsafe fixture.",
                            "files": ["../outside.txt"],
                            "should_trigger": True,
                            "risk": "low",
                            "holdout": False,
                            "assertions": [
                                {
                                    "id": "safe",
                                    "kind": "safety",
                                    "description": "Fixture remains confined.",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(init_eval_workspace.WorkspaceError):
            init_eval_workspace.load_evals(skill)

    def test_eval_contract_requires_holdout_and_accepts_critical_risk(self) -> None:
        skill = self._valid_skill("critical-eval-skill")
        (skill / "evals").mkdir()
        evals_path = skill / "evals" / "evals.json"
        scenario = {
            "id": "critical-case",
            "prompt": "Exercise the critical workflow.",
            "should_trigger": True,
            "risk": "critical",
            "assertions": [
                {
                    "id": "safe-result",
                    "kind": "safety",
                    "description": "The workflow remains inside its authority boundary.",
                }
            ],
        }
        evals_path.write_text(
            json.dumps({"version": 1, "skill": "critical-eval-skill", "scenarios": [scenario]}),
            encoding="utf-8",
        )
        report = skill_validation.validate_path(skill, deep=True)
        self.assertIn("eval-holdout-required", {finding.code for finding in report.errors})
        with self.assertRaises(init_eval_workspace.WorkspaceError):
            init_eval_workspace.load_evals(skill)

        scenario["holdout"] = True
        evals_path.write_text(
            json.dumps({"version": 1, "skill": "critical-eval-skill", "scenarios": [scenario]}),
            encoding="utf-8",
        )
        report = skill_validation.validate_path(skill, deep=True)
        self.assertNotIn("eval-risk", {finding.code for finding in report.errors})
        _, loaded, _ = init_eval_workspace.load_evals(skill)
        self.assertEqual("critical", loaded["scenarios"][0]["risk"])

    def test_viewer_loads_canonical_runs_and_escapes_script_data(self) -> None:
        workspace = self.root / "iteration-1"
        outputs = workspace / "danger-case" / "with_skill" / "run-1" / "outputs"
        outputs.mkdir(parents=True)
        (outputs / "artifact.txt").write_text("safe artifact", encoding="utf-8")
        evals = workspace / "evals.snapshot.json"
        evals.write_text(
            json.dumps(
                {
                    "version": 1,
                    "skill": "viewer-skill",
                    "scenarios": [
                        {
                            "id": "danger-case",
                            "prompt": "Render a potentially hostile result.",
                            "should_trigger": True,
                            "risk": "low",
                            "holdout": False,
                            "assertions": [
                                {
                                    "id": "safe",
                                    "kind": "safety",
                                    "description": "Output is shown as inert text.",
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        hostile = "</script><script>alert(1)</script>&\u2028"
        results = workspace / "results.jsonl"
        results.write_text(
            json.dumps(
                {
                    "scenario_id": "danger-case",
                    "variant": "with_skill",
                    "run_id": "run-1",
                    "passed": True,
                    "outputs_dir": "danger-case/with_skill/run-1/outputs",
                    "final_output": hostile,
                    "assertions": [
                        {"id": "safe", "passed": True, "evidence": "Rendered inertly"}
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        runs, skill_name = eval_viewer.load_canonical_runs(workspace, results, evals)
        self.assertEqual("viewer-skill", skill_name)
        self.assertEqual("danger-case--with_skill--run-1", runs[0]["id"])
        self.assertEqual("danger-case", runs[0]["scenario_id"])
        page = eval_viewer.generate_html(runs, skill_name, iteration=1)
        self.assertNotIn(hostile, page)
        self.assertIn("\\u003c/script\\u003e", page)
        self.assertIn("\\u0026", page)
        self.assertIn("\\u2028", page)
        self.assertNotIn("fonts.googleapis.com", page)
        source = VIEWER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("_kill_port", source)
        self.assertNotIn("taskkill", source.lower())

    def test_viewer_rejects_output_paths_outside_workspace(self) -> None:
        workspace = self.root / "iteration-1"
        workspace.mkdir()
        results = workspace / "results.jsonl"
        results.write_text(
            json.dumps(
                {
                    "scenario_id": "unsafe",
                    "variant": "baseline",
                    "run_id": "run-1",
                    "passed": False,
                    "outputs_dir": "../outside",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(eval_viewer.ReviewError):
            eval_viewer.load_canonical_runs(workspace, results, None)

    def test_static_viewer_requires_force_for_collisions(self) -> None:
        output_dir = self.root / "static-review"
        output_dir.mkdir()
        review = output_dir / "review.html"
        stylesheet = output_dir / "viewer.css"
        stylesheet.write_text("user-owned", encoding="utf-8")

        with self.assertRaises(eval_viewer.ReviewError):
            eval_viewer.write_static_review(review, "<html>first</html>")

        self.assertFalse(review.exists())
        self.assertEqual("user-owned", stylesheet.read_text(encoding="utf-8"))

        eval_viewer.write_static_review(review, "<html>second</html>", force=True)
        self.assertEqual("<html>second</html>", review.read_text(encoding="utf-8"))
        self.assertNotEqual("user-owned", stylesheet.read_text(encoding="utf-8"))
        self.assertTrue((output_dir / "viewer.js").is_file())
        self.assertTrue((output_dir / "assets" / "skill-creator.png").is_file())
        self.assertTrue((output_dir / "assets" / "skill-creator-small.svg").is_file())

    def test_example_scaffold_is_release_blocking(self) -> None:
        with redirect_stdout(io.StringIO()):
            skill = init_skill.init_skill(
                "unfinished-skill", self.root, ["evals"], True, []
            )
        self.assertIsNotNone(skill)
        report = skill_validation.validate_path(skill, deep=True)
        codes = {finding.code for finding in report.errors}
        self.assertIn("body-placeholder", codes)
        self.assertIn("eval-placeholder", codes)

    def test_packages_are_deterministic_and_self_verifying(self) -> None:
        skill = self._valid_skill()
        first = self.root / "first.skill"
        second = self.root / "second.skill"
        result_one = package_skill.create_package(skill, first, strict=True)
        result_two = package_skill.create_package(skill, second, strict=True)
        self.assertEqual(result_one.sha256, result_two.sha256)
        self.assertEqual(
            hashlib.sha256(first.read_bytes()).hexdigest(),
            hashlib.sha256(second.read_bytes()).hexdigest(),
        )
        verified = package_skill.verify_archive(first)
        self.assertEqual("demo-skill", verified.skill_name)
        self.assertGreater(verified.files, 0)

    def test_package_rejects_link_target_excluded_by_skillignore(self) -> None:
        skill = self._valid_skill()
        (skill / "references").mkdir()
        (skill / "references" / "private.md").write_text("private", encoding="utf-8")
        with (skill / "SKILL.md").open("a", encoding="utf-8") as handle:
            handle.write("\n[Required reference](references/private.md)\n")
        (skill / ".skillignore").write_text("references/private.md\n", encoding="utf-8")
        with self.assertRaises(package_skill.PackageError):
            package_skill.create_package(skill, self.root / "excluded-link.skill", strict=True)

    def test_verifier_rejects_path_traversal(self) -> None:
        archive = self.root / "malicious.skill"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../escape.txt", "bad")
        with self.assertRaises(package_skill.PackageError):
            package_skill.verify_archive(archive)

    def test_verifier_rejects_case_collisions(self) -> None:
        archive = self.root / "collision.skill"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("demo-skill/SKILL.md", "one")
            handle.writestr("Demo-Skill/skill.md", "two")
        with self.assertRaises(package_skill.PackageError):
            package_skill.verify_archive(archive)

    def test_verifier_rejects_control_and_non_normalized_names(self) -> None:
        with self.assertRaises(package_skill.PackageError):
            package_skill._validate_member_name("demo-skill/line\nbreak.txt")
        with self.assertRaises(package_skill.PackageError):
            package_skill._validate_member_name("demo-skill/e\u0301.txt")

    def test_verifier_rejects_manifest_tampering(self) -> None:
        skill = self._valid_skill()
        original = self.root / "original.skill"
        tampered = self.root / "tampered.skill"
        package_skill.create_package(skill, original, strict=True)
        with zipfile.ZipFile(original, "r") as source, zipfile.ZipFile(
            tampered, "w"
        ) as target:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename.endswith("scripts/helper.py"):
                    payload = b"def answer():\n    return 0\n"
                target.writestr(info, payload)
        with self.assertRaises(package_skill.PackageError):
            package_skill.verify_archive(tampered)

    def test_force_package_replace_is_atomic_on_failure(self) -> None:
        skill = self._valid_skill()
        output = self.root / "existing.skill"
        output.write_bytes(b"previous release")
        with mock.patch.object(
            package_skill.os, "replace", side_effect=OSError("injected replace failure")
        ):
            with self.assertRaises(OSError):
                package_skill.create_package(skill, output, strict=True, force=True)
        self.assertEqual(b"previous release", output.read_bytes())

    def test_benchmark_reports_deltas_and_escapes_html(self) -> None:
        results = self.root / "results.jsonl"
        records = [
            {"scenario_id": "case-one", "variant": "baseline", "run_id": "b1", "passed": False, "score": 0.2, "duration_seconds": 10, "tokens": 100, "notes": "<script>|", "assertions": [{"id": "artifact", "passed": False, "evidence": "<img src=x>| absent"}]},
            {"scenario_id": "case-one", "variant": "baseline", "run_id": "b2", "passed": True, "score": 0.6, "duration_seconds": 14, "tokens": 120},
            {"scenario_id": "case-one", "variant": "with_skill", "run_id": "s1", "passed": True, "score": 0.8, "duration_seconds": 12, "tokens": 130},
            {"scenario_id": "case-one", "variant": "with_skill", "run_id": "s2", "passed": True, "score": 1.0, "duration_seconds": 16, "tokens": 150},
        ]
        results.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        report = benchmark_evals.aggregate(benchmark_evals._read_jsonl(results))
        self.assertAlmostEqual(0.5, report["delta_with_skill_minus_baseline"]["pass_rate"])
        self.assertAlmostEqual(0.5, report["delta_with_skill_minus_baseline"]["score_mean"])
        self.assertGreater(report["variants"]["baseline"]["score_stdev"], 0)
        markdown = benchmark_evals.render_markdown(report)
        self.assertNotIn("<script>", markdown)
        self.assertIn("&lt;script&gt;\\|", markdown)
        self.assertNotIn("<img", markdown)
        self.assertIn("&lt;img src=x&gt;\\| absent", markdown)

    def test_single_observation_has_unknown_sample_standard_deviation(self) -> None:
        self.assertIsNone(benchmark_evals._stdev([1.0]))

    def test_benchmark_exposes_pairing_and_unmatched_runs(self) -> None:
        records = [
            {
                "scenario_id": "paired",
                "variant": "baseline",
                "run_id": "run-1",
                "passed": False,
                "score": 0.2,
                "duration_seconds": 10,
                "tokens": 100,
            },
            {
                "scenario_id": "paired",
                "variant": "with_skill",
                "run_id": "run-1",
                "passed": True,
                "score": 0.8,
                "duration_seconds": 12,
                "tokens": 130,
            },
            {
                "scenario_id": "unmatched",
                "variant": "baseline",
                "run_id": "only-baseline",
                "passed": True,
                "score": 0.7,
            },
        ]
        report = benchmark_evals.aggregate(records)
        self.assertEqual(1, report["pairing"]["complete_pairs"])
        self.assertEqual(1, len(report["pairing"]["unmatched_runs"]))
        self.assertAlmostEqual(
            0.6, report["pairing"]["paired_delta_summary"]["score"]["mean"]
        )
        self.assertIn("unmatched", report["incomplete_scenarios"])
        markdown = benchmark_evals.render_markdown(report)
        self.assertIn("## Pairing integrity", markdown)
        self.assertIn("only-baseline", markdown)

    def test_eval_definitions_require_complete_assertion_evidence(self) -> None:
        definitions_path = self.root / "evals.json"
        definitions_path.write_text(
            json.dumps(
                {
                    "scenarios": [
                        {
                            "id": "case-one",
                            "assertions": [{"id": "artifact"}, {"id": "scope"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        definitions = benchmark_evals._load_scenarios(definitions_path)
        records = [
            {
                "scenario_id": "case-one",
                "variant": "baseline",
                "run_id": "b1",
                "passed": True,
                "score": 1.0,
                "assertions": [
                    {"id": "artifact", "passed": True, "evidence": "present"}
                ],
            }
        ]
        with self.assertRaises(benchmark_evals.BenchmarkError):
            benchmark_evals._validate_against_definitions(records, definitions)

    def test_benchmark_public_cli_accepts_paired_evidence(self) -> None:
        definitions = self.root / "evals.json"
        definitions.write_text(
            json.dumps(
                {
                    "scenarios": [
                        {
                            "id": "case-one",
                            "assertions": [{"id": "artifact"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        results = self.root / "results.jsonl"
        records = [
            {
                "scenario_id": "case-one",
                "variant": "baseline",
                "run_id": "b1",
                "passed": False,
                "score": 0.2,
                "assertions": [
                    {"id": "artifact", "passed": False, "evidence": "absent"}
                ],
            },
            {
                "scenario_id": "case-one",
                "variant": "with_skill",
                "run_id": "s1",
                "passed": True,
                "score": 0.9,
                "assertions": [
                    {"id": "artifact", "passed": True, "evidence": "present"}
                ],
            },
        ]
        results.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        output = self.root / "benchmark.md"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS / "benchmark_evals.py"),
                str(results),
                "--evals",
                str(definitions),
                "--format",
                "markdown",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        report = output.read_text(encoding="utf-8")
        self.assertIn("## Assertion evidence", report)
        self.assertIn("0.700", report)


if __name__ == "__main__":
    unittest.main()
