#!/usr/bin/env python3
"""Inventory explicitly named skill roots and flag review candidates."""

from __future__ import annotations

import argparse
from collections import defaultdict
from difflib import SequenceMatcher
import html
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import yaml


FRONTMATTER = re.compile(r"\A---\r?\n(?P<yaml>.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
TOKEN = re.compile(r"[a-z0-9]+")
SKIP_DIRS = {".git", "__pycache__", "node_modules"}
MAX_SKILL_BYTES = 2 * 1024 * 1024
STOP_WORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "use", "when",
    "with", "user", "users", "skill", "codex",
}


class CatalogError(Exception):
    pass


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _tokens(value: str) -> set[str]:
    return {word for word in TOKEN.findall(value.lower()) if word not in STOP_WORDS}


def _parse_skill(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not _within(resolved, root):
        raise CatalogError(f"Refusing SKILL.md outside explicit root: {path}")
    if resolved.stat().st_size > MAX_SKILL_BYTES:
        raise CatalogError(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes: {path}")
    text = resolved.read_text(encoding="utf-8-sig")
    match = FRONTMATTER.match(text)
    if not match:
        raise CatalogError(f"Missing YAML frontmatter: {path}")
    try:
        data = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise CatalogError(f"Invalid YAML frontmatter in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"Frontmatter must be a mapping: {path}")
    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not name.strip():
        raise CatalogError(f"Frontmatter name must be a non-empty string: {path}")
    if not isinstance(description, str) or not description.strip():
        raise CatalogError(f"Frontmatter description must be a non-empty string: {path}")
    return {
        "name": name.strip(),
        "normalized_name": _normalize_name(name),
        "description": description.strip(),
        "path": str(resolved),
        "root": str(root),
    }


def collect_skills(roots: list[Path]) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for raw_root in roots:
        try:
            root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise CatalogError(f"Skill root not found: {raw_root}") from exc
        if not root.is_dir():
            raise CatalogError(f"Skill root must be a directory: {root}")
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                directory
                for directory in dirs
                if directory not in SKIP_DIRS
                and not (current_path / directory).is_symlink()
            )
            if "SKILL.md" not in files:
                continue
            candidate = (current_path / "SKILL.md").resolve()
            if candidate in seen_paths:
                continue
            seen_paths.add(candidate)
            skills.append(_parse_skill(candidate, root))
    return sorted(skills, key=lambda item: (item["normalized_name"], item["path"].lower()))


def build_catalog(
    roots: list[Path], threshold: float = 0.45
) -> dict[str, Any]:
    if not 0 <= threshold <= 1:
        raise CatalogError("Similarity threshold must be between 0 and 1.")
    skills = collect_skills(roots)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for skill in skills:
        by_name[skill["normalized_name"]].append(skill)
    duplicates = [
        {
            "normalized_name": name,
            "skills": [
                {"name": item["name"], "path": item["path"]}
                for item in matches
            ],
        }
        for name, matches in sorted(by_name.items())
        if len(matches) > 1
    ]

    overlaps: list[dict[str, Any]] = []
    for left_index, left in enumerate(skills):
        left_tokens = _tokens(left["name"] + " " + left["description"])
        for right in skills[left_index + 1 :]:
            if left["normalized_name"] == right["normalized_name"]:
                continue
            right_tokens = _tokens(right["name"] + " " + right["description"])
            union = left_tokens | right_tokens
            token_overlap = len(left_tokens & right_tokens) / len(union) if union else 0.0
            name_similarity = SequenceMatcher(
                None, left["normalized_name"], right["normalized_name"]
            ).ratio()
            score = max(token_overlap, name_similarity)
            if token_overlap < threshold and name_similarity < max(0.85, threshold):
                continue
            overlaps.append(
                {
                    "left": {"name": left["name"], "path": left["path"]},
                    "right": {"name": right["name"], "path": right["path"]},
                    "score": round(score, 4),
                    "token_jaccard": round(token_overlap, 4),
                    "name_similarity": round(name_similarity, 4),
                    "interpretation": "review-lead-not-equivalence",
                }
            )
    overlaps.sort(key=lambda item: (-item["score"], item["left"]["name"], item["right"]["name"]))

    return {
        "schema_version": 1,
        "roots": [str(path.resolve()) for path in roots],
        "skill_count": len(skills),
        "skills": skills,
        "duplicate_names": duplicates,
        "overlap_candidates": overlaps,
        "notes": [
            "Similarity is lexical and is only a review lead for human inspection.",
            "No create, merge, connect, deprecate, or delete action is implied.",
        ],
    }


def _cell(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("|", r"\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Skill Catalog",
        "",
        f"- Skills: {report['skill_count']}",
        f"- Exact duplicate-name groups: {len(report['duplicate_names'])}",
        f"- Lexical overlap candidates: {len(report['overlap_candidates'])}",
        "",
        "## Skills",
        "",
        "| Name | Description | Path |",
        "|---|---|---|",
    ]
    for item in report["skills"]:
        lines.append(
            f"| {_cell(item['name'])} | {_cell(item['description'])} | {_cell(item['path'])} |"
        )
    lines.extend(["", "## Exact duplicate names", ""])
    if report["duplicate_names"]:
        for group in report["duplicate_names"]:
            lines.append(f"### {_cell(group['normalized_name'])}")
            lines.append("")
            for item in group["skills"]:
                lines.append(f"- {_cell(item['name'])}: {_cell(item['path'])}")
            lines.append("")
    else:
        lines.append("None.")
        lines.append("")
    lines.extend(
        [
            "## Lexical overlap candidates",
            "",
            "| Left | Right | Score | Token overlap | Name similarity |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for item in report["overlap_candidates"]:
        lines.append(
            f"| {_cell(item['left']['name'])} | {_cell(item['right']['name'])} | "
            f"{item['score']:.3f} | {item['token_jaccard']:.3f} | "
            f"{item['name_similarity']:.3f} |"
        )
    if not report["overlap_candidates"]:
        lines.append("| None | None | 0.000 | 0.000 | 0.000 |")
    lines.extend(
        [
            "",
            "> Similarity is a review lead, not evidence that skills are equivalent.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Catalog explicitly named skill roots and flag duplicate or overlapping candidates."
    )
    parser.add_argument("roots", nargs="+", type=Path, help="Skill root directories to scan")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="Minimum lexical token overlap (default: 0.45)",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path, help="Write output to this file")
    args = parser.parse_args()
    try:
        report = build_catalog(args.roots, args.threshold)
        output = (
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
            if args.format == "json"
            else render_markdown(report)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    except (CatalogError, OSError, UnicodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
