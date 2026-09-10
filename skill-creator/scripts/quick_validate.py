#!/usr/bin/env python3
"""Fast compatibility wrapper for basic Codex skill validation."""

from __future__ import annotations

import sys

from skill_validation import validate_path


def validate_skill(skill_path):
    """Return the historical ``(valid, message)`` result shape."""
    report = validate_path(skill_path, deep=False)
    if report.errors:
        first = report.errors[0]
        location = f" ({first.path}:{first.line})" if first.line else ""
        return False, f"{first.message}{location}"
    return True, "Skill is valid!"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: python quick_validate.py <skill_directory>")
        return 2
    valid, message = validate_skill(argv[0])
    print(message)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
