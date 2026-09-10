#!/usr/bin/env python3
"""Aggregate paired skill/baseline eval records into JSON or safe Markdown."""

from __future__ import annotations

import argparse
from collections import defaultdict
import html
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any


VARIANTS = {"with_skill", "baseline"}


class BenchmarkError(Exception):
    pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise BenchmarkError(f"Results file not found: {path}")
    records: list[dict[str, Any]] = []
    seen_runs: set[tuple[str, str, str]] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        raw = raw.removesuffix("\r")
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"Line {line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise BenchmarkError(f"Line {line_number}: each record must be a JSON object.")
        scenario_id = record.get("scenario_id")
        variant = record.get("variant")
        run_id = record.get("run_id")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise BenchmarkError(f"Line {line_number}: scenario_id must be a non-empty string.")
        if variant not in VARIANTS:
            raise BenchmarkError(
                f"Line {line_number}: variant must be 'with_skill' or 'baseline'."
            )
        if not isinstance(run_id, str) or not run_id.strip():
            raise BenchmarkError(f"Line {line_number}: run_id must be a non-empty string.")
        identity = (scenario_id, variant, run_id)
        if identity in seen_runs:
            raise BenchmarkError(f"Line {line_number}: duplicate run identity {identity}.")
        seen_runs.add(identity)
        if not isinstance(record.get("passed"), bool):
            raise BenchmarkError(f"Line {line_number}: passed must be true or false.")
        score = record.get("score", 1.0 if record["passed"] else 0.0)
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
            raise BenchmarkError(f"Line {line_number}: score must be between 0 and 1.")
        record["score"] = float(score)
        for field in ("duration_seconds", "tokens"):
            value = record.get(field)
            if value is not None and (
                not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0
            ):
                raise BenchmarkError(f"Line {line_number}: {field} must be non-negative.")
        notes = record.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise BenchmarkError(f"Line {line_number}: notes must be a string when present.")
        assertions = record.get("assertions")
        if assertions is not None:
            if not isinstance(assertions, list) or not assertions:
                raise BenchmarkError(
                    f"Line {line_number}: assertions must be a non-empty list when present."
                )
            assertion_ids: set[str] = set()
            for index, assertion in enumerate(assertions):
                label = f"Line {line_number}: assertions[{index}]"
                if not isinstance(assertion, dict):
                    raise BenchmarkError(f"{label} must be an object.")
                assertion_id = assertion.get("id")
                if not isinstance(assertion_id, str) or not assertion_id.strip():
                    raise BenchmarkError(f"{label}.id must be a non-empty string.")
                if assertion_id in assertion_ids:
                    raise BenchmarkError(f"{label}.id is duplicated: {assertion_id}")
                assertion_ids.add(assertion_id)
                if not isinstance(assertion.get("passed"), bool):
                    raise BenchmarkError(f"{label}.passed must be true or false.")
                evidence = assertion.get("evidence")
                if not isinstance(evidence, str) or not evidence.strip():
                    raise BenchmarkError(f"{label}.evidence must be a non-empty string.")
        records.append(record)
    if not records:
        raise BenchmarkError("Results file contains no records.")
    present_variants = {record["variant"] for record in records}
    missing = sorted(VARIANTS - present_variants)
    if missing:
        raise BenchmarkError("Paired comparison requires variant(s): " + ", ".join(missing))
    return records


def _load_scenarios(path: Path | None) -> dict[str, set[str]] | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"Could not read eval definitions: {exc}") from exc
    scenarios = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(scenarios, list):
        raise BenchmarkError("Eval definitions must contain a scenarios list.")
    definitions: dict[str, set[str]] = {}
    for index, item in enumerate(scenarios):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise BenchmarkError(f"Eval scenario {index} has no valid id.")
        scenario_id = item["id"]
        if scenario_id in definitions:
            raise BenchmarkError(f"Eval definitions duplicate scenario id: {scenario_id}")
        assertions = item.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise BenchmarkError(f"Eval scenario {scenario_id} has no assertions.")
        assertion_ids: set[str] = set()
        for assertion_index, assertion in enumerate(assertions):
            if not isinstance(assertion, dict) or not isinstance(assertion.get("id"), str):
                raise BenchmarkError(
                    f"Eval scenario {scenario_id} assertion {assertion_index} has no valid id."
                )
            assertion_id = assertion["id"]
            if assertion_id in assertion_ids:
                raise BenchmarkError(
                    f"Eval scenario {scenario_id} duplicates assertion id: {assertion_id}"
                )
            assertion_ids.add(assertion_id)
        definitions[scenario_id] = assertion_ids
    return definitions


def _validate_against_definitions(
    records: list[dict[str, Any]], definitions: dict[str, set[str]]
) -> None:
    unknown = sorted({record["scenario_id"] for record in records} - set(definitions))
    if unknown:
        raise BenchmarkError("Results reference unknown scenario ids: " + ", ".join(unknown))
    for record in records:
        identity = f"{record['scenario_id']}/{record['variant']}/{record['run_id']}"
        assertions = record.get("assertions")
        if not isinstance(assertions, list):
            raise BenchmarkError(
                f"Run {identity} must include assertion-level evidence when --evals is used."
            )
        actual = {assertion["id"] for assertion in assertions}
        expected = definitions[record["scenario_id"]]
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise BenchmarkError(
                f"Run {identity} assertion mismatch; missing={missing}, extra={extra}."
            )
        assertion_pass = all(assertion["passed"] for assertion in assertions)
        if record["passed"] != assertion_pass:
            raise BenchmarkError(
                f"Run {identity} passed value must equal the conjunction of its assertions."
            )


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [record["score"] for record in records]
    durations = [float(record["duration_seconds"]) for record in records if record.get("duration_seconds") is not None]
    tokens = [float(record["tokens"]) for record in records if record.get("tokens") is not None]
    pass_rate = sum(1 for record in records if record["passed"]) / len(records)
    return {
        "runs": len(records),
        "passed": sum(1 for record in records if record["passed"]),
        "pass_rate": pass_rate,
        "pass_rate_standard_error": math.sqrt(
            pass_rate * (1 - pass_rate) / len(records)
        ),
        "score_mean": _mean(scores),
        "score_stdev": _stdev(scores),
        "duration_seconds_mean": _mean(durations),
        "duration_seconds_stdev": _stdev(durations),
        "tokens_mean": _mean(tokens),
        "tokens_stdev": _stdev(tokens),
    }


def _difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_scenario: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for record in records:
        by_variant[record["variant"]].append(record)
        by_scenario[record["scenario_id"]][record["variant"]].append(record)

    variants = {variant: _summary(by_variant[variant]) for variant in sorted(VARIANTS)}
    skill = variants["with_skill"]
    baseline = variants["baseline"]
    delta = {
        key: _difference(skill.get(key), baseline.get(key))
        for key in ("pass_rate", "score_mean", "duration_seconds_mean", "tokens_mean")
    }
    scenarios: dict[str, Any] = {}
    incomplete: list[str] = []
    for scenario_id in sorted(by_scenario):
        variants_for_scenario = by_scenario[scenario_id]
        counts = {
            variant: len(variants_for_scenario.get(variant, []))
            for variant in VARIANTS
        }
        if set(variants_for_scenario) != VARIANTS or len(set(counts.values())) != 1:
            incomplete.append(scenario_id)
        scenario_summaries = {
            variant: _summary(variants_for_scenario.get(variant, []))
            for variant in sorted(VARIANTS)
            if variants_for_scenario.get(variant)
        }
        if set(scenario_summaries) == VARIANTS:
            scenario_summaries["delta"] = {
                key: _difference(
                    scenario_summaries["with_skill"].get(key),
                    scenario_summaries["baseline"].get(key),
                )
                for key in ("pass_rate", "score_mean", "duration_seconds_mean", "tokens_mean")
            }
        scenarios[scenario_id] = scenario_summaries

    indexed = {
        (record["scenario_id"], record["run_id"], record["variant"]): record
        for record in records
    }
    pair_keys = sorted(
        {
            (record["scenario_id"], record["run_id"])
            for record in records
            if (record["scenario_id"], record["run_id"], "baseline") in indexed
            and (record["scenario_id"], record["run_id"], "with_skill") in indexed
        }
    )
    paired_deltas: dict[str, list[float]] = defaultdict(list)
    for scenario_id, run_id in pair_keys:
        baseline_run = indexed[(scenario_id, run_id, "baseline")]
        skill_run = indexed[(scenario_id, run_id, "with_skill")]
        paired_deltas["pass"].append(
            float(skill_run["passed"]) - float(baseline_run["passed"])
        )
        paired_deltas["score"].append(skill_run["score"] - baseline_run["score"])
        for field in ("duration_seconds", "tokens"):
            if baseline_run.get(field) is not None and skill_run.get(field) is not None:
                paired_deltas[field].append(
                    float(skill_run[field]) - float(baseline_run[field])
                )
    paired_summary = {
        metric: {"mean": _mean(values), "stdev": _stdev(values), "pairs": len(values)}
        for metric, values in paired_deltas.items()
    }
    paired_identities = {
        (scenario_id, run_id, variant)
        for scenario_id, run_id in pair_keys
        for variant in VARIANTS
    }
    unmatched = [
        {
            "scenario_id": record["scenario_id"],
            "variant": record["variant"],
            "run_id": record["run_id"],
        }
        for record in records
        if (record["scenario_id"], record["run_id"], record["variant"])
        not in paired_identities
    ]
    return {
        "schema_version": 1,
        "records": len(records),
        "variants": variants,
        "delta_with_skill_minus_baseline": delta,
        "incomplete_scenarios": incomplete,
        "pairing": {
            "key": ["scenario_id", "run_id"],
            "complete_pairs": len(pair_keys),
            "unmatched_runs": unmatched,
            "paired_delta_summary": paired_summary,
        },
        "scenarios": scenarios,
        "runs": records,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _escape_cell(value: Any) -> str:
    escaped = html.escape(str(value), quote=False)
    return escaped.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Skill Evaluation Benchmark",
        "",
        "Paired comparison; deltas are `with_skill - baseline`.",
        "",
        "## Overall results",
        "",
        "| Variant | Runs | Passed | Pass rate | Mean score | Score stdev | Mean seconds | Mean tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in ("baseline", "with_skill"):
        item = report["variants"][variant]
        lines.append(
            "| "
            + " | ".join(
                [
                    variant,
                    str(item["runs"]),
                    str(item["passed"]),
                    _fmt(item["pass_rate"]),
                    _fmt(item["score_mean"]),
                    _fmt(item["score_stdev"]),
                    _fmt(item["duration_seconds_mean"]),
                    _fmt(item["tokens_mean"]),
                ]
            )
            + " |"
        )
    delta = report["delta_with_skill_minus_baseline"]
    lines.extend(
        [
            "",
            "| Delta | Pass rate | Mean score | Mean seconds | Mean tokens |",
            "|---|---:|---:|---:|---:|",
            "| with_skill - baseline | "
            + " | ".join(
                _fmt(delta[key])
                for key in ("pass_rate", "score_mean", "duration_seconds_mean", "tokens_mean")
            )
            + " |",
            "",
            "## Variability",
            "",
            "| Variant | Pass-rate SE | Score stdev | Seconds stdev | Tokens stdev |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for variant in ("baseline", "with_skill"):
        item = report["variants"][variant]
        lines.append(
            "| "
            + " | ".join(
                [
                    variant,
                    _fmt(item.get("pass_rate_standard_error")),
                    _fmt(item.get("score_stdev")),
                    _fmt(item.get("duration_seconds_stdev")),
                    _fmt(item.get("tokens_stdev")),
                ]
            )
            + " |"
        )
    pairing = report["pairing"]
    lines.extend(
        [
            "",
            "## Pairing integrity",
            "",
            f"- Complete pairs by `scenario_id` and `run_id`: {pairing['complete_pairs']}",
            f"- Unmatched runs: {len(pairing['unmatched_runs'])}",
            "",
            "| Metric delta | Pairs | Mean | Stdev |",
            "|---|---:|---:|---:|",
        ]
    )
    for metric in ("pass", "score", "duration_seconds", "tokens"):
        item = pairing["paired_delta_summary"].get(metric, {})
        lines.append(
            f"| {metric} | {item.get('pairs', 0)} | "
            f"{_fmt(item.get('mean'))} | {_fmt(item.get('stdev'))} |"
        )
    if pairing["unmatched_runs"]:
        lines.extend(["", "Unmatched run identities:", ""])
        for item in pairing["unmatched_runs"]:
            lines.append(
                f"- `{_escape_cell(item['scenario_id'])}` / "
                f"`{_escape_cell(item['variant'])}` / `{_escape_cell(item['run_id'])}`"
            )
    lines.extend(
        [
            "",
            "## Scenario results",
            "",
            "| Scenario | Baseline pass rate | Skill pass rate | Pass-rate delta | Baseline score | Skill score | Score delta |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scenario_id, item in report["scenarios"].items():
        baseline = item.get("baseline", {})
        skill = item.get("with_skill", {})
        scenario_delta = item.get("delta", {})
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(scenario_id),
                    _fmt(baseline.get("pass_rate")),
                    _fmt(skill.get("pass_rate")),
                    _fmt(scenario_delta.get("pass_rate")),
                    _fmt(baseline.get("score_mean")),
                    _fmt(skill.get("score_mean")),
                    _fmt(scenario_delta.get("score_mean")),
                ]
            )
            + " |"
        )
    if report["incomplete_scenarios"]:
        lines.extend(
            [
                "",
                "**Incomplete paired scenarios:** "
                + ", ".join(_escape_cell(item) for item in report["incomplete_scenarios"]),
            ]
        )
    lines.extend(
        [
            "",
            "## Run evidence",
            "",
            "| Scenario | Variant | Run | Passed | Score | Seconds | Tokens | Notes |",
            "|---|---|---|---|---:|---:|---:|---|",
        ]
    )
    for record in report["runs"]:
        lines.append(
            "| "
            + " | ".join(
                _escape_cell(value)
                for value in (
                    record["scenario_id"],
                    record["variant"],
                    record["run_id"],
                    record["passed"],
                    _fmt(record.get("score")),
                    _fmt(record.get("duration_seconds")),
                    _fmt(record.get("tokens")),
                    record.get("notes", ""),
                )
            )
            + " |"
        )
    assertion_rows = [
        (record, assertion)
        for record in report["runs"]
        for assertion in record.get("assertions", [])
    ]
    if assertion_rows:
        lines.extend(
            [
                "",
                "## Assertion evidence",
                "",
                "| Scenario | Variant | Run | Assertion | Passed | Evidence |",
                "|---|---|---|---|---:|---|",
            ]
        )
        for record, assertion in assertion_rows:
            lines.append(
                "| "
                + " | ".join(
                    _escape_cell(value)
                    for value in (
                        record["scenario_id"],
                        record["variant"],
                        record["run_id"],
                        assertion["id"],
                        assertion["passed"],
                        assertion["evidence"],
                    )
                )
                + " |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate paired Codex skill eval JSONL records."
    )
    parser.add_argument("results", help="JSONL file containing one record per run.")
    parser.add_argument(
        "--evals",
        help="Optional evals/evals.json used to reject unknown scenario ids.",
    )
    parser.add_argument(
        "--format", choices=["json", "markdown"], default="markdown"
    )
    parser.add_argument("--output", help="Write output to this path instead of stdout.")
    args = parser.parse_args(argv)
    try:
        records = _read_jsonl(Path(args.results).resolve())
        definitions = _load_scenarios(Path(args.evals).resolve() if args.evals else None)
        if definitions is not None:
            _validate_against_definitions(records, definitions)
        report = aggregate(records)
        rendered = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.format == "json"
            else render_markdown(report)
        )
        if args.output:
            output = Path(args.output).resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (BenchmarkError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
