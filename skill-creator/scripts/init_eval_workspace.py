#!/usr/bin/env python3
"""Create an isolated, canonical skill-evaluation workspace without running a model."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any

import yaml

from eval_contract import ASSERTION_KINDS, EVAL_VERSION, IDENTIFIER_RE, RISK_LEVELS


FRONTMATTER = re.compile(r"\A---\r?\n(?P<yaml>.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
VARIANTS = ("baseline", "with_skill")


class WorkspaceError(Exception):
    pass


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _skill_name(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise WorkspaceError(f"SKILL.md not found: {skill_md}")
    text = skill_md.read_text(encoding="utf-8-sig")
    match = FRONTMATTER.match(text)
    if not match:
        raise WorkspaceError(f"SKILL.md has no valid frontmatter: {skill_md}")
    try:
        data = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise WorkspaceError(f"Invalid SKILL.md frontmatter: {exc}") from exc
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise WorkspaceError("SKILL.md frontmatter name must be a non-empty string.")
    return name.strip()


def load_evals(skill_dir: Path) -> tuple[str, dict[str, Any], Path]:
    try:
        skill_root = skill_dir.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"Skill directory not found: {skill_dir}") from exc
    if not skill_root.is_dir():
        raise WorkspaceError(f"Skill path must be a directory: {skill_root}")
    name = _skill_name(skill_root)
    evals_path = skill_root / "evals" / "evals.json"
    if not evals_path.is_file():
        raise WorkspaceError(f"Evaluation file not found: {evals_path}")
    try:
        data = json.loads(evals_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"Invalid eval JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != EVAL_VERSION:
        raise WorkspaceError(
            f"evals/evals.json must be an object with version {EVAL_VERSION}."
        )
    declared_skill = data.get("skill")
    if declared_skill != name:
        raise WorkspaceError(
            f"Eval skill '{declared_skill}' does not match SKILL.md name '{name}'."
        )
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise WorkspaceError("Eval definitions require a non-empty scenarios list.")
    seen_scenarios: set[str] = set()
    for index, scenario in enumerate(scenarios):
        label = f"scenarios[{index}]"
        if not isinstance(scenario, dict):
            raise WorkspaceError(f"{label} must be an object.")
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not IDENTIFIER_RE.fullmatch(scenario_id):
            raise WorkspaceError(f"{label}.id must be a path-safe identifier.")
        if scenario_id in seen_scenarios:
            raise WorkspaceError(f"Duplicate scenario id: {scenario_id}")
        seen_scenarios.add(scenario_id)
        if not isinstance(scenario.get("prompt"), str) or not scenario["prompt"].strip():
            raise WorkspaceError(f"{label}.prompt must be a non-empty string.")
        if not isinstance(scenario.get("should_trigger"), bool):
            raise WorkspaceError(f"{label}.should_trigger must be Boolean.")
        if scenario.get("risk") not in RISK_LEVELS:
            raise WorkspaceError(f"{label}.risk must be one of {sorted(RISK_LEVELS)}.")
        if not isinstance(scenario.get("holdout"), bool):
            raise WorkspaceError(f"{label}.holdout must be Boolean.")
        assertions = scenario.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise WorkspaceError(f"{label}.assertions must be a non-empty list.")
        seen_assertions: set[str] = set()
        for assertion_index, assertion in enumerate(assertions):
            assertion_label = f"{label}.assertions[{assertion_index}]"
            if not isinstance(assertion, dict):
                raise WorkspaceError(f"{assertion_label} must be an object.")
            assertion_id = assertion.get("id")
            if not isinstance(assertion_id, str) or not IDENTIFIER_RE.fullmatch(assertion_id):
                raise WorkspaceError(f"{assertion_label}.id must be a path-safe identifier.")
            if assertion_id in seen_assertions:
                raise WorkspaceError(f"{label} duplicates assertion id: {assertion_id}")
            seen_assertions.add(assertion_id)
            if assertion.get("kind") not in ASSERTION_KINDS:
                raise WorkspaceError(
                    f"{assertion_label}.kind must be one of {sorted(ASSERTION_KINDS)}."
                )
            description = assertion.get("description")
            if not isinstance(description, str) or not description.strip():
                raise WorkspaceError(f"{assertion_label}.description must be non-empty.")
        files = scenario.get("files", [])
        if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
            raise WorkspaceError(f"{label}.files must be a list of relative file paths.")
        for file_ref in files:
            relative = Path(file_ref)
            if relative.is_absolute() or ".." in relative.parts:
                raise WorkspaceError(f"{label}.files contains unsafe path: {file_ref}")
            source = (skill_root / relative).resolve()
            if not _within(source, skill_root) or not source.is_file():
                raise WorkspaceError(f"{label}.files is missing or outside the skill: {file_ref}")
    return name, data, skill_root


def initialize_workspace(
    skill_dir: Path,
    output: Path,
    iteration: int = 1,
    runs_per_variant: int = 1,
) -> Path:
    if iteration < 1:
        raise WorkspaceError("Iteration must be at least 1.")
    if runs_per_variant < 1 or runs_per_variant > 100:
        raise WorkspaceError("Runs per variant must be between 1 and 100.")
    name, evals, skill_root = load_evals(skill_dir)
    output_root = output.resolve()
    target = output_root / f"iteration-{iteration}"
    if target.exists():
        raise WorkspaceError(f"Iteration directory already exists: {target}")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        temporary = Path(
            tempfile.mkdtemp(prefix=f".iteration-{iteration}-", dir=output_root)
        )
        snapshot_path = temporary / "evals.snapshot.json"
        snapshot_path.write_text(
            json.dumps(evals, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        runs = []
        for scenario in evals["scenarios"]:
            fixture_paths = []
            for file_ref in scenario.get("files", []):
                source = (skill_root / file_ref).resolve()
                destination = temporary / "fixtures" / scenario["id"] / Path(file_ref)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                fixture_paths.append(destination.relative_to(temporary).as_posix())
            for variant in VARIANTS:
                for run_number in range(1, runs_per_variant + 1):
                    run_id = f"run-{run_number}"
                    run_dir = Path(scenario["id"]) / variant / run_id
                    outputs_dir = run_dir / "outputs"
                    (temporary / outputs_dir).mkdir(parents=True, exist_ok=False)
                    runs.append(
                        {
                            "scenario_id": scenario["id"],
                            "variant": variant,
                            "run_id": run_id,
                            "run_dir": run_dir.as_posix(),
                            "outputs_dir": outputs_dir.as_posix(),
                            "prompt": scenario["prompt"],
                            "files": fixture_paths,
                        }
                    )
        skill_bytes = (skill_root / "SKILL.md").read_bytes()
        plan = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "skill": name,
            "skill_path": str(skill_root),
            "skill_md_sha256": hashlib.sha256(skill_bytes).hexdigest(),
            "evals_snapshot": "evals.snapshot.json",
            "iteration": iteration,
            "runs_per_variant": runs_per_variant,
            "variants": list(VARIANTS),
            "runs": runs,
        }
        (temporary / "run-plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (temporary / "results.jsonl").write_text("", encoding="utf-8")
        temporary.replace(target)
        temporary = None
        return target
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a canonical isolated skill-evaluation workspace."
    )
    parser.add_argument("skill_directory", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="Workspace root")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--runs", type=int, default=1, help="Runs per scenario and variant")
    args = parser.parse_args()
    try:
        target = initialize_workspace(
            args.skill_directory, args.output, args.iteration, args.runs
        )
        print(f"[OK] Created evaluation workspace: {target}")
        print(f"[OK] Run plan: {target / 'run-plan.json'}")
        print("[INFO] No model or evaluator was invoked.")
        return 0
    except (WorkspaceError, OSError, UnicodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
