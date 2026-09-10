#!/usr/bin/env python3
"""Run Codex-native release validation for a skill directory."""

from __future__ import annotations

import argparse

from skill_validation import format_json_report, format_text_report, validate_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a Codex skill, its resources, metadata, and optional evals."
    )
    parser.add_argument("skill_directory")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Check only SKILL.md frontmatter, naming, body, and placeholders.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as release-blocking failures.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    report = validate_path(args.skill_directory, deep=not args.quick)
    if args.json:
        print(format_json_report(report, args.strict))
    else:
        print(format_text_report(report, args.strict))
    return 0 if report.passed(args.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
