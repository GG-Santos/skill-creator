#!/usr/bin/env python3
"""Codex-native structural and release validation for skill directories."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import yaml

from eval_contract import ASSERTION_KINDS, EVAL_VERSION, IDENTIFIER_RE, RISK_LEVELS


MAX_SKILL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_MD_BYTES = 2 * 1024 * 1024
MAX_FILE_COUNT = 500
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024

ALLOWED_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "allowed-tools",
    "metadata",
}
ALLOWED_OPENAI_TOP_LEVEL = {"interface", "dependencies", "policy"}
ALLOWED_INTERFACE_KEYS = {
    "display_name",
    "short_description",
    "icon_small",
    "icon_large",
    "brand_color",
    "default_prompt",
}
UNWANTED_DIRECTORY_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache"}
UNWANTED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
UNWANTED_FILE_SUFFIXES = {".pyc", ".pyo"}
TEXT_SUFFIXES = {
    ".cfg", ".conf", ".csv", ".ini", ".js", ".json", ".jsx", ".md",
    ".properties", ".ps1", ".py", ".sh", ".toml", ".ts", ".tsx", ".txt",
    ".xml", ".yaml", ".yml",
}
SECRET_TEXT_NAMES = {".env", ".npmrc", ".pypirc"}

FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<yaml>.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL
)
FENCE_RE = re.compile(
    r"^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?(?P<fence>`{3,}|~{3,})(?P<tail>.*)$"
)
LOCAL_LINK_RE = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)]+)\)")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EVAL_ID_RE = IDENTIFIER_RE
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None


@dataclass
class ParsedSkill:
    content: str
    frontmatter: dict[str, Any]
    body: str
    body_start_line: int


class ValidationReport:
    def __init__(self, skill_path: Path):
        self.skill_path = skill_path
        self.skill_name: str | None = None
        self.findings: list[Finding] = []
        self.checks: list[str] = []

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        path: Path | str | None = None,
        line: int | None = None,
    ) -> None:
        rendered_path: str | None = None
        if path is not None:
            candidate = Path(path) if not isinstance(path, Path) else path
            try:
                rendered_path = candidate.resolve().relative_to(self.skill_path).as_posix()
            except (OSError, ValueError):
                rendered_path = str(path)
        self.findings.append(Finding(severity, code, message, rendered_path, line))

    def error(self, code: str, message: str, path=None, line=None) -> None:
        self.add("error", code, message, path, line)

    def warning(self, code: str, message: str, path=None, line=None) -> None:
        self.add("warning", code, message, path, line)

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "warning"]

    def passed(self, strict: bool = False) -> bool:
        return not self.errors and (not strict or not self.warnings)

    def as_dict(self, strict: bool = False) -> dict[str, Any]:
        return {
            "skill_path": str(self.skill_path),
            "skill_name": self.skill_name,
            "passed": self.passed(strict),
            "strict": strict,
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "checks_completed": len(self.checks),
            },
            "checks": self.checks,
            "findings": [asdict(item) for item in self.findings],
        }


def _read_utf8(path: Path, report: ValidationReport) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        report.error("encoding", f"File is not valid UTF-8: {exc}", path)
    except OSError as exc:
        report.error("read-failed", f"Could not read file: {exc}", path)
    return None


def _parse_skill_md(skill_md: Path, report: ValidationReport) -> ParsedSkill | None:
    content = _read_utf8(skill_md, report)
    if content is None:
        return None
    if content.startswith("\ufeff"):
        report.warning("utf8-bom", "Remove the UTF-8 BOM from SKILL.md.", skill_md, 1)
        content = content.lstrip("\ufeff")
    match = FRONTMATTER_RE.match(content)
    if not match:
        report.error(
            "frontmatter-format",
            "SKILL.md must begin with YAML frontmatter delimited by --- lines.",
            skill_md,
            1,
        )
        return None
    try:
        frontmatter = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        report.error("frontmatter-yaml", f"Invalid YAML frontmatter: {exc}", skill_md, 1)
        return None
    if not isinstance(frontmatter, dict):
        report.error("frontmatter-type", "Frontmatter must be a YAML mapping.", skill_md, 1)
        return None
    body = content[match.end() :]
    body_start_line = content[: match.end()].count("\n") + 1
    return ParsedSkill(content, frontmatter, body, body_start_line)


def _non_fenced_lines(text: str) -> Iterable[tuple[int, str]]:
    fence_char: str | None = None
    fence_length = 0
    for number, line in enumerate(text.splitlines(), start=1):
        fence = FENCE_RE.match(line)
        if fence:
            marker = fence.group("fence")
            if fence_char is None:
                fence_char = marker[0]
                fence_length = len(marker)
            elif (
                marker[0] == fence_char
                and len(marker) >= fence_length
                and not fence.group("tail").strip()
            ):
                fence_char = None
                fence_length = 0
            continue
        if fence_char is None:
            yield number, line


def _has_unfinished_marker(text: str) -> tuple[int, str] | None:
    for line_number, line in _non_fenced_lines(text):
        if re.search(r"\[TODO:[^\]]*\]", line, re.IGNORECASE):
            return line_number, "[TODO: ...]"
        if re.search(r"\b(?:TODO_REPLACE|REPLACE_ME)\b", line):
            return line_number, "unfinished replacement marker"
    return None


def _validate_frontmatter(
    parsed: ParsedSkill, skill_path: Path, skill_md: Path, report: ValidationReport
) -> None:
    frontmatter = parsed.frontmatter
    unexpected = sorted(set(frontmatter) - ALLOWED_FRONTMATTER)
    if unexpected:
        report.error(
            "frontmatter-unsupported",
            "Unsupported Codex skill frontmatter key(s): " + ", ".join(unexpected),
            skill_md,
            1,
        )

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        report.error("name-required", "Frontmatter 'name' must be a non-empty string.", skill_md, 1)
    else:
        name = name.strip()
        report.skill_name = name
        if len(name) > MAX_SKILL_NAME_LENGTH:
            report.error(
                "name-length",
                f"Skill name has {len(name)} characters; maximum is {MAX_SKILL_NAME_LENGTH}.",
                skill_md,
                1,
            )
        if not NAME_RE.fullmatch(name):
            report.error(
                "name-format",
                "Skill name must use lowercase letters, digits, and single hyphens.",
                skill_md,
                1,
            )
        if skill_path.name != name:
            report.error(
                "folder-name",
                f"Skill folder '{skill_path.name}' must match frontmatter name '{name}'.",
                skill_md,
                1,
            )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        report.error(
            "description-required",
            "Frontmatter 'description' must be a non-empty string.",
            skill_md,
            1,
        )
    else:
        description = description.strip()
        if len(description) > MAX_DESCRIPTION_LENGTH:
            report.error(
                "description-length",
                f"Description has {len(description)} characters; maximum is {MAX_DESCRIPTION_LENGTH}.",
                skill_md,
                1,
            )
        if "<" in description or ">" in description:
            report.error(
                "description-angle-brackets",
                "Description cannot contain angle brackets.",
                skill_md,
                1,
            )
        if re.search(r"\bTODO\b|\[TODO:", description, re.IGNORECASE):
            report.error(
                "description-placeholder",
                "Description contains an unfinished TODO marker.",
                skill_md,
                1,
            )

    for field in ("license", "compatibility"):
        value = frontmatter.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            report.error(
                f"{field}-type",
                f"Frontmatter '{field}' must be a non-empty string when present.",
                skill_md,
                1,
            )
    metadata = frontmatter.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        report.error("metadata-type", "Frontmatter 'metadata' must be a mapping.", skill_md, 1)

    if not parsed.body.strip():
        report.error("body-required", "SKILL.md must contain instructions after frontmatter.", skill_md)
    marker = _has_unfinished_marker(parsed.body)
    if marker:
        relative_line, label = marker
        report.error(
            "body-placeholder",
            f"Skill instructions contain an unfinished {label}.",
            skill_md,
            parsed.body_start_line + relative_line - 1,
        )


def _safe_local_target(source: Path, raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = re.split(r"\s+[\"']", target, maxsplit=1)[0]
    target = unquote(target.strip())
    if not target or target.startswith("#"):
        return None
    parsed = urlparse(target)
    if parsed.scheme or target.startswith("//"):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    if "{{" in target or "}}" in target:
        return target
    return str((source.parent / target).resolve())


def _validate_markdown_links(skill_path: Path, files: list[Path], report: ValidationReport) -> None:
    resolved_skill_path = skill_path.resolve()
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        text = _read_utf8(path, report)
        if text is None:
            continue
        for line_number, line in _non_fenced_lines(text):
            without_code = re.sub(r"`[^`]*`", "", line)
            for match in LOCAL_LINK_RE.finditer(without_code):
                target = match.group("target")
                resolved = _safe_local_target(path, target)
                if resolved is None:
                    continue
                if "{{" in resolved or "}}" in resolved:
                    report.warning(
                        "templated-link",
                        f"Templated link target was not resolved: {target}",
                        path,
                        line_number,
                    )
                    continue
                resolved_path = Path(resolved)
                try:
                    resolved_path.relative_to(resolved_skill_path)
                except ValueError:
                    report.error(
                        "link-outside-skill",
                        f"Local link target must stay inside the skill folder: {target}",
                        path,
                        line_number,
                    )
                    continue
                if not resolved_path.exists():
                    report.error(
                        "broken-link",
                        f"Local link target does not exist: {target}",
                        path,
                        line_number,
                    )
    report.checks.append("markdown-links")


def _validate_openai_yaml(skill_path: Path, report: ValidationReport) -> None:
    path = skill_path / "agents" / "openai.yaml"
    if not path.exists():
        report.warning(
            "openai-yaml-missing",
            "agents/openai.yaml is optional, but recommended for Codex UI metadata.",
            path,
        )
        return
    text = _read_utf8(path, report)
    if text is None:
        return
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        report.error("openai-yaml-invalid", f"Invalid YAML: {exc}", path)
        return
    if not isinstance(data, dict):
        report.error("openai-yaml-type", "agents/openai.yaml must contain a mapping.", path)
        return
    unknown_top = sorted(set(data) - ALLOWED_OPENAI_TOP_LEVEL)
    if unknown_top:
        report.warning(
            "openai-yaml-unknown",
            "Unrecognized top-level key(s): " + ", ".join(unknown_top),
            path,
        )

    interface = data.get("interface")
    if interface is not None:
        if not isinstance(interface, dict):
            report.error("interface-type", "interface must be a mapping.", path)
        else:
            unknown = sorted(set(interface) - ALLOWED_INTERFACE_KEYS)
            if unknown:
                report.warning(
                    "interface-unknown",
                    "Unrecognized interface key(s): " + ", ".join(unknown),
                    path,
                )
            for key, value in interface.items():
                if key in ALLOWED_INTERFACE_KEYS and (
                    not isinstance(value, str) or not value.strip()
                ):
                    report.error(
                        "interface-value",
                        f"interface.{key} must be a non-empty string.",
                        path,
                    )
            short_description = interface.get("short_description")
            if isinstance(short_description, str) and not (25 <= len(short_description) <= 64):
                report.warning(
                    "interface-short-description",
                    "interface.short_description is normally 25-64 characters.",
                    path,
                )
            brand_color = interface.get("brand_color")
            if isinstance(brand_color, str) and not re.fullmatch(r"#[0-9A-Fa-f]{6}", brand_color):
                report.error(
                    "interface-brand-color",
                    "interface.brand_color must be a six-digit hex color.",
                    path,
                )
            default_prompt = interface.get("default_prompt")
            if (
                isinstance(default_prompt, str)
                and report.skill_name
                and f"${report.skill_name}" not in default_prompt
            ):
                report.warning(
                    "interface-default-prompt",
                    f"interface.default_prompt should mention ${report.skill_name}.",
                    path,
                )
            for icon_key in ("icon_small", "icon_large"):
                icon = interface.get(icon_key)
                if not isinstance(icon, str):
                    continue
                icon_path = (skill_path / icon).resolve()
                try:
                    icon_path.relative_to(skill_path)
                except ValueError:
                    report.error(
                        "interface-icon-scope",
                        f"interface.{icon_key} must stay inside the skill folder.",
                        path,
                    )
                    continue
                if not icon_path.is_file():
                    report.error(
                        "interface-icon-missing",
                        f"interface.{icon_key} does not exist: {icon}",
                        path,
                    )

    policy = data.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            report.error("policy-type", "policy must be a mapping.", path)
        elif "allow_implicit_invocation" in policy and not isinstance(
            policy["allow_implicit_invocation"], bool
        ):
            report.error(
                "policy-implicit-type",
                "policy.allow_implicit_invocation must be true or false.",
                path,
            )

    dependencies = data.get("dependencies")
    if dependencies is not None:
        if not isinstance(dependencies, dict):
            report.error("dependencies-type", "dependencies must be a mapping.", path)
        else:
            tools = dependencies.get("tools")
            if tools is not None and not isinstance(tools, list):
                report.error("dependencies-tools-type", "dependencies.tools must be a list.", path)
            elif isinstance(tools, list):
                for index, tool in enumerate(tools):
                    if not isinstance(tool, dict):
                        report.error(
                            "dependency-tool-type",
                            f"dependencies.tools[{index}] must be a mapping.",
                            path,
                        )
                    elif tool.get("type") != "mcp":
                        report.warning(
                            "dependency-tool-kind",
                            f"dependencies.tools[{index}].type is not the documented 'mcp' value.",
                            path,
                        )
    report.checks.append("agents-openai-yaml")


def _collect_files(skill_path: Path, report: ValidationReport) -> list[Path]:
    files: list[Path] = []
    total_bytes = 0
    for root, directories, names in os.walk(skill_path):
        root_path = Path(root)
        for directory in list(directories):
            path = root_path / directory
            if path.is_symlink():
                report.error("symlink", "Symbolic-link directories are not allowed in release packages.", path)
                directories.remove(directory)
            elif directory in UNWANTED_DIRECTORY_NAMES:
                report.error("generated-directory", f"Remove generated directory '{directory}'.", path)
                directories.remove(directory)
        for name in names:
            path = root_path / name
            if path.is_symlink():
                report.error("symlink", "Symbolic-link files are not allowed in release packages.", path)
                continue
            if not path.is_file():
                report.error("unsupported-file", "Unsupported filesystem entry.", path)
                continue
            files.append(path)
            try:
                size = path.stat().st_size
            except OSError as exc:
                report.error("stat-failed", f"Could not inspect file: {exc}", path)
                continue
            total_bytes += size
            if size > MAX_FILE_BYTES:
                report.warning(
                    "large-file",
                    f"File is {size} bytes; review whether it belongs in the skill package.",
                    path,
                )
            if name in UNWANTED_FILE_NAMES or path.suffix.lower() in UNWANTED_FILE_SUFFIXES:
                report.error("generated-file", "Remove generated/cache file from the skill.", path)
    if len(files) > MAX_FILE_COUNT:
        report.warning(
            "file-count",
            f"Skill contains {len(files)} files; review package scope (recommended maximum {MAX_FILE_COUNT}).",
        )
    if total_bytes > MAX_TOTAL_BYTES:
        report.warning(
            "skill-size",
            f"Skill contains {total_bytes} bytes; review package scope.",
        )
    report.checks.append("filesystem-hygiene")
    return sorted(files)


def _validate_python(files: list[Path], report: ValidationReport) -> None:
    for path in files:
        if path.suffix.lower() != ".py":
            continue
        text = _read_utf8(path, report)
        if text is None:
            continue
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            report.error(
                "python-syntax",
                f"Python syntax error: {exc.msg}",
                path,
                exc.lineno,
            )
    report.checks.append("python-syntax")


def _validate_secrets(files: list[Path], report: ValidationReport) -> None:
    for path in files:
        lower_name = path.name.lower()
        if (
            path.suffix.lower() not in TEXT_SUFFIXES
            and lower_name not in SECRET_TEXT_NAMES
            and not lower_name.startswith(".env.")
        ):
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                report.warning(
                    "secret-scan-skipped",
                    "Text-like file exceeds the 2 MiB secret-scan limit; review it manually.",
                    path,
                )
                continue
        except OSError:
            continue
        text = _read_utf8(path, report)
        if text is None:
            continue
        for pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                report.error(
                    "possible-secret",
                    "Possible credential or private key material found; remove or replace it.",
                    path,
                    line,
                )
                break
    report.checks.append("secret-patterns")


def _validate_skillignore(skill_path: Path, report: ValidationReport) -> None:
    path = skill_path / ".skillignore"
    if not path.exists():
        return
    text = _read_utf8(path, report)
    if text is None:
        return
    for line_number, raw in enumerate(text.splitlines(), start=1):
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        if pattern.startswith("!"):
            report.error(
                "skillignore-negation",
                "Negated .skillignore patterns are not supported.",
                path,
                line_number,
            )
            continue
        normalized = pattern.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", normalized):
            report.error(
                "skillignore-scope",
                ".skillignore patterns must be relative and stay inside the skill.",
                path,
                line_number,
            )
    report.checks.append("skillignore")


def _validate_eval_assertion(
    assertion: Any,
    scenario_index: int,
    assertion_index: int,
    path: Path,
    report: ValidationReport,
) -> str | None:
    label = f"scenarios[{scenario_index}].assertions[{assertion_index}]"
    if not isinstance(assertion, dict):
        report.error("eval-assertion-type", f"{label} must be a mapping.", path)
        return None
    assertion_id = assertion.get("id")
    if not isinstance(assertion_id, str) or not EVAL_ID_RE.fullmatch(assertion_id):
        report.error(
            "eval-assertion-id",
            f"{label}.id must match {EVAL_ID_RE.pattern}.",
            path,
        )
        assertion_id = None
    kind = assertion.get("kind")
    if kind not in ASSERTION_KINDS:
        report.error(
            "eval-assertion-kind",
            f"{label}.kind must be one of: {', '.join(sorted(ASSERTION_KINDS))}.",
            path,
        )
    description = assertion.get("description")
    if not isinstance(description, str) or not description.strip():
        report.error(
            "eval-assertion-description",
            f"{label}.description must be a non-empty string.",
            path,
        )
    elif re.search(r"\bTODO\b|REPLACE_ME", description, re.IGNORECASE):
        report.error("eval-placeholder", f"{label}.description contains a placeholder.", path)
    return assertion_id


def _validate_evals(skill_path: Path, report: ValidationReport) -> None:
    evals_dir = skill_path / "evals"
    path = evals_dir / "evals.json"
    if evals_dir.exists() and not path.is_file():
        report.error("eval-file-missing", "evals/ exists but evals/evals.json is missing.", evals_dir)
        return
    if not path.exists():
        return
    text = _read_utf8(path, report)
    if text is None:
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        report.error("eval-json", f"Invalid JSON: {exc.msg}", path, exc.lineno)
        return
    if not isinstance(data, dict):
        report.error("eval-root", "evals/evals.json must contain a JSON object.", path)
        return
    if data.get("version") != EVAL_VERSION:
        report.error(
            "eval-version",
            f"evals/evals.json version must be {EVAL_VERSION}.",
            path,
        )
    eval_skill = data.get("skill")
    if not isinstance(eval_skill, str) or eval_skill != report.skill_name:
        report.error(
            "eval-skill",
            f"evals/evals.json skill must equal '{report.skill_name}'.",
            path,
        )
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        report.error("eval-scenarios", "scenarios must be a non-empty list.", path)
        return
    scenario_ids: set[str] = set()
    positive = 0
    negative = 0
    holdout = 0
    for index, scenario in enumerate(scenarios):
        label = f"scenarios[{index}]"
        if not isinstance(scenario, dict):
            report.error("eval-scenario-type", f"{label} must be a mapping.", path)
            continue
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not EVAL_ID_RE.fullmatch(scenario_id):
            report.error("eval-scenario-id", f"{label}.id must match {EVAL_ID_RE.pattern}.", path)
        elif scenario_id in scenario_ids:
            report.error("eval-scenario-duplicate", f"Duplicate scenario id: {scenario_id}", path)
        else:
            scenario_ids.add(scenario_id)
        prompt = scenario.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            report.error("eval-prompt", f"{label}.prompt must be a non-empty string.", path)
        elif re.search(r"\bTODO\b|REPLACE_ME|\[TODO:", prompt, re.IGNORECASE):
            report.error("eval-placeholder", f"{label}.prompt contains a placeholder.", path)
        should_trigger = scenario.get("should_trigger")
        if not isinstance(should_trigger, bool):
            report.error("eval-trigger-type", f"{label}.should_trigger must be true or false.", path)
        elif should_trigger:
            positive += 1
        else:
            negative += 1
        risk = scenario.get("risk")
        if risk not in RISK_LEVELS:
            report.error(
                "eval-risk",
                f"{label}.risk must be one of: {', '.join(sorted(RISK_LEVELS))}.",
                path,
            )
        if "holdout" not in scenario:
            report.error("eval-holdout-required", f"{label}.holdout is required.", path)
        elif not isinstance(scenario["holdout"], bool):
            report.error("eval-holdout-type", f"{label}.holdout must be true or false.", path)
        elif scenario["holdout"]:
            holdout += 1
        assertions = scenario.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            report.error("eval-assertions", f"{label}.assertions must be a non-empty list.", path)
            continue
        assertion_ids: set[str] = set()
        for assertion_index, assertion in enumerate(assertions):
            assertion_id = _validate_eval_assertion(
                assertion, index, assertion_index, path, report
            )
            if assertion_id and assertion_id in assertion_ids:
                report.error(
                    "eval-assertion-duplicate",
                    f"Duplicate assertion id '{assertion_id}' in {label}.",
                    path,
                )
            elif assertion_id:
                assertion_ids.add(assertion_id)
    if positive == 0:
        report.warning("eval-positive-missing", "Eval suite has no positive trigger case.", path)
    if negative == 0:
        report.warning("eval-negative-missing", "Eval suite has no near-miss/non-trigger case.", path)
    if holdout == 0:
        report.warning("eval-holdout-missing", "Eval suite has no held-out scenario.", path)
    report.checks.append("eval-schema")


def validate_path(skill_path: str | Path, deep: bool = False) -> ValidationReport:
    path = Path(skill_path).resolve()
    report = ValidationReport(path)
    if not path.exists():
        report.error("skill-missing", "Skill directory does not exist.", path)
        return report
    if not path.is_dir():
        report.error("skill-not-directory", "Skill path is not a directory.", path)
        return report
    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        report.error("skill-md-missing", "SKILL.md not found.", skill_md)
        return report
    try:
        if skill_md.stat().st_size > MAX_SKILL_MD_BYTES:
            report.error(
                "skill-md-size",
                f"SKILL.md exceeds the {MAX_SKILL_MD_BYTES}-byte validation limit.",
                skill_md,
            )
            return report
    except OSError as exc:
        report.error("stat-failed", f"Could not inspect SKILL.md: {exc}", skill_md)
        return report
    parsed = _parse_skill_md(skill_md, report)
    if parsed is None:
        return report
    _validate_frontmatter(parsed, path, skill_md, report)
    report.checks.append("frontmatter-and-body")
    if not deep:
        return report

    files = _collect_files(path, report)
    _validate_python(files, report)
    _validate_markdown_links(path, files, report)
    _validate_openai_yaml(path, report)
    _validate_skillignore(path, report)
    _validate_evals(path, report)
    _validate_secrets(files, report)
    return report


def format_text_report(report: ValidationReport, strict: bool = False) -> str:
    lines = [f"Skill: {report.skill_name or report.skill_path.name}"]
    for finding in report.findings:
        location = finding.path or "."
        if finding.line is not None:
            location += f":{finding.line}"
        lines.append(
            f"[{finding.severity.upper()}] {finding.code} {location} - {finding.message}"
        )
    result = "PASS" if report.passed(strict) else "FAIL"
    lines.append(
        f"{result}: {len(report.errors)} error(s), {len(report.warnings)} warning(s), "
        f"{len(report.checks)} check group(s)."
    )
    return "\n".join(lines)


def format_json_report(report: ValidationReport, strict: bool = False) -> str:
    return json.dumps(report.as_dict(strict), indent=2, sort_keys=True)
