#!/usr/bin/env python3
"""
Skill Initializer - Creates a new skill from template

Usage:
    python init_skill.py <skill-name> --path <path> [--blueprint name] [--resources scripts,references,assets,evals] [--examples] [--interface key=value]
    python init_skill.py --list-blueprints

Examples:
    python init_skill.py my-new-skill --path skills/public --blueprint instruction-only
    python init_skill.py my-api-helper --path skills/private --blueprint script-backed
    python init_skill.py my-new-skill --path skills/public --resources scripts,references
    python init_skill.py my-api-helper --path skills/private --resources scripts --examples
    python init_skill.py custom-skill --path /custom/location
    python init_skill.py my-skill --path skills/public --interface short_description="Short UI label"
"""

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

from generate_openai_yaml import write_openai_yaml

MAX_SKILL_NAME_LENGTH = 64
ALLOWED_RESOURCES = {"scripts", "references", "assets", "evals"}
BLUEPRINT_DIR = Path(__file__).resolve().parents[1] / "assets" / "blueprints"
BLUEPRINT_RESOURCES = {
    "instruction-only": (),
    "reference-guided": ("references",),
    "script-backed": ("scripts", "references"),
    "artifact-producing": ("assets", "scripts"),
    "orchestrator": ("references",),
    "evaluation-ready": ("evals", "references"),
}

SKILL_TEMPLATE = """---
name: {skill_name}
description: "[TODO: Briefly describe what this skill does and when it applies.]"
---

# {skill_title}

[TODO: Add the task-specific guidance Codex needs. Reference supporting files only when they are relevant.]
"""

EXAMPLE_SCRIPT = '''#!/usr/bin/env python3
"""
Example helper script for {skill_name}

This is a placeholder script that can be executed directly.
Replace with actual implementation or delete if not needed.

Example real scripts from other skills:
- pdf/scripts/fill_fillable_fields.py - Fills PDF form fields
- pdf/scripts/convert_pdf_to_images.py - Converts PDF pages to images
"""

def main():
    print("This is an example script for {skill_name}")
    # TODO: Add actual script logic here
    # This could be data processing, file conversion, API calls, etc.

if __name__ == "__main__":
    main()
'''

EXAMPLE_REFERENCE = """# Reference for {skill_title}

Replace this placeholder with maintained, task-specific details that Codex
would not reliably know, such as operational constraints, local schemas, or
fragile integration behavior.

Delete this file if no supported workflow needs it.
"""

EXAMPLE_ASSET = """# Example Asset File

This placeholder represents where asset files would be stored.
Replace with actual asset files (templates, images, fonts, etc.) or delete if not needed.

Asset files are NOT intended to be loaded into context, but rather used within
the output Codex produces.

Example asset files from other skills:
- Brand guidelines: logo.png, slides_template.pptx
- Frontend builder: hello-world/ directory with HTML/React boilerplate
- Typography: custom-font.ttf, font-family.woff2
- Data: sample_data.csv, test_dataset.json

## Common Asset Types

- Templates: .pptx, .docx, boilerplate directories
- Images: .png, .jpg, .svg, .gif
- Fonts: .ttf, .otf, .woff, .woff2
- Boilerplate code: Project directories, starter files
- Icons: .ico, .svg
- Data files: .csv, .json, .xml, .yaml

Note: This is a text placeholder. Actual assets can be any file type.
"""

EVALS_TEMPLATE = """{{
  "version": 1,
  "skill": "{skill_name}",
  "scenarios": [
    {{
      "id": "replace-example",
      "prompt": "[TODO: Replace with a realistic request.]",
      "should_trigger": true,
      "risk": "low",
      "holdout": false,
      "assertions": [
        {{
          "id": "replace-assertion",
          "kind": "outcome",
          "description": "[TODO: Replace with an observable success criterion.]"
        }}
      ]
    }}
  ]
}}
"""


def normalize_skill_name(skill_name):
    """Normalize a skill name to lowercase hyphen-case."""
    normalized = skill_name.strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = normalized.strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized


def title_case_skill_name(skill_name):
    """Convert hyphenated skill name to Title Case for display."""
    return " ".join(word.capitalize() for word in skill_name.split("-"))


def parse_resources(raw_resources):
    if not raw_resources:
        return []
    resources = [item.strip() for item in raw_resources.split(",") if item.strip()]
    invalid = sorted({item for item in resources if item not in ALLOWED_RESOURCES})
    if invalid:
        allowed = ", ".join(sorted(ALLOWED_RESOURCES))
        print(f"[ERROR] Unknown resource type(s): {', '.join(invalid)}")
        print(f"   Allowed: {allowed}")
        sys.exit(1)
    deduped = []
    seen = set()
    for resource in resources:
        if resource not in seen:
            deduped.append(resource)
            seen.add(resource)
    return deduped


def merge_blueprint_resources(blueprint, resources):
    if blueprint not in BLUEPRINT_RESOURCES:
        raise ValueError(f"Unknown blueprint: {blueprint}")
    merged = []
    for resource in (*BLUEPRINT_RESOURCES[blueprint], *resources):
        if resource not in merged:
            merged.append(resource)
    return merged


def load_blueprint(blueprint, skill_name, skill_title):
    if blueprint not in BLUEPRINT_RESOURCES:
        raise ValueError(f"Unknown blueprint: {blueprint}")
    template_path = BLUEPRINT_DIR / f"{blueprint}.md.tmpl"
    if not template_path.is_file():
        raise FileNotFoundError(f"Blueprint template not found: {template_path}")
    return template_path.read_text(encoding="utf-8").format(
        skill_name=skill_name,
        skill_title=skill_title,
    )


def create_resource_dirs(
    skill_dir, skill_name, skill_title, resources, include_examples
):
    for resource in resources:
        resource_dir = skill_dir / resource
        resource_dir.mkdir(exist_ok=True)
        if resource == "scripts":
            if include_examples:
                example_script = resource_dir / "example.py"
                example_script.write_text(
                    EXAMPLE_SCRIPT.format(skill_name=skill_name), encoding="utf-8"
                )
                example_script.chmod(0o755)
                print("[OK] Created scripts/example.py")
            else:
                print("[OK] Created scripts/")
        elif resource == "references":
            if include_examples:
                example_reference = resource_dir / "api_reference.md"
                example_reference.write_text(
                    EXAMPLE_REFERENCE.format(skill_title=skill_title), encoding="utf-8"
                )
                print("[OK] Created references/api_reference.md")
            else:
                print("[OK] Created references/")
        elif resource == "assets":
            if include_examples:
                example_asset = resource_dir / "example_asset.txt"
                example_asset.write_text(EXAMPLE_ASSET, encoding="utf-8")
                print("[OK] Created assets/example_asset.txt")
            else:
                print("[OK] Created assets/")
        elif resource == "evals":
            if include_examples:
                evals_path = resource_dir / "evals.json"
                evals_path.write_text(
                    EVALS_TEMPLATE.format(skill_name=skill_name), encoding="utf-8"
                )
                print("[OK] Created evals/evals.json (unfinished and release-blocking)")
            else:
                print("[OK] Created evals/")


def init_skill(
    skill_name,
    path,
    resources,
    include_examples,
    interface_overrides,
    blueprint="instruction-only",
):
    """
    Initialize a new skill directory with template SKILL.md.

    Args:
        skill_name: Name of the skill
        path: Path where the skill directory should be created
        resources: Resource directories to create in addition to blueprint defaults
        include_examples: Whether to create example files in resource directories
        blueprint: Blueprint template name

    Returns:
        Path to created skill directory, or None if error
    """
    try:
        merged_resources = merge_blueprint_resources(blueprint, resources)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return None
    parent_dir = Path(path).resolve()
    skill_dir = parent_dir / skill_name
    if skill_dir.exists():
        print(f"[ERROR] Skill directory already exists: {skill_dir}")
        return None
    temporary_dir = None
    try:
        parent_dir.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{skill_name}-", dir=parent_dir)
        )
        print(f"[OK] Preparing skill transaction for: {skill_dir}")
    except Exception as e:
        print(f"[ERROR] Error preparing skill directory: {e}")
        return None
    try:
        skill_title = title_case_skill_name(skill_name)
        skill_content = load_blueprint(blueprint, skill_name, skill_title)
        skill_md_path = temporary_dir / "SKILL.md"
        skill_md_path.write_text(skill_content, encoding="utf-8")
        print("[OK] Created SKILL.md")
        result = write_openai_yaml(temporary_dir, skill_name, interface_overrides)
        if not result:
            raise RuntimeError("agents/openai.yaml generation failed")
        if merged_resources:
            create_resource_dirs(
                temporary_dir,
                skill_name,
                skill_title,
                merged_resources,
                include_examples,
            )
        temporary_dir.replace(skill_dir)
        temporary_dir = None
        print(f"[OK] Committed skill directory: {skill_dir}")
    except Exception as e:
        print(f"[ERROR] Skill initialization failed; no target was committed: {e}")
        return None
    finally:
        if temporary_dir is not None and temporary_dir.exists():
            shutil.rmtree(temporary_dir, ignore_errors=True)

    # Print next steps
    print(f"\n[OK] Skill '{skill_name}' initialized successfully at {skill_dir}")
    print("\nNext steps:")
    print("1. Edit SKILL.md to complete the TODO items and update the description")
    if merged_resources:
        if include_examples:
            print(
                "2. Customize or delete every generated example in the selected resource directories"
            )
        else:
            print("2. Add content to the selected resource directories as needed")
    else:
        print(
            "2. Create resource directories only if needed (scripts/, references/, assets/, evals/)"
        )
    print("3. Update agents/openai.yaml if the UI metadata should differ")
    print("4. Run python scripts/validate_skill.py <skill-directory> when ready")
    print(
        "5. Consider independent forward-testing only when complexity or risk warrants it"
    )

    return skill_dir


def main():
    parser = argparse.ArgumentParser(
        description="Create a new skill directory with a SKILL.md template.",
    )
    parser.add_argument("skill_name", nargs="?", help="Skill name (normalized to hyphen-case)")
    parser.add_argument("--path", help="Output directory for the skill")
    parser.add_argument(
        "--blueprint",
        choices=sorted(BLUEPRINT_RESOURCES),
        default="instruction-only",
        help="Reusable skill structure (default: instruction-only)",
    )
    parser.add_argument(
        "--list-blueprints",
        action="store_true",
        help="List available blueprints and their default resources, then exit",
    )
    parser.add_argument(
        "--resources",
        default="",
        help="Comma-separated list: scripts,references,assets,evals",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="Create example files inside the selected resource directories",
    )
    parser.add_argument(
        "--interface",
        action="append",
        default=[],
        help="Interface override in key=value format (repeatable)",
    )
    args = parser.parse_args()

    if args.list_blueprints:
        for name, defaults in BLUEPRINT_RESOURCES.items():
            resources_label = ",".join(defaults) if defaults else "none"
            print(f"{name}: {resources_label}")
        return
    if not args.skill_name:
        parser.error("skill_name is required unless --list-blueprints is used")
    if not args.path:
        parser.error("--path is required unless --list-blueprints is used")

    raw_skill_name = args.skill_name
    skill_name = normalize_skill_name(raw_skill_name)
    if not skill_name:
        print("[ERROR] Skill name must include at least one letter or digit.")
        sys.exit(1)
    if len(skill_name) > MAX_SKILL_NAME_LENGTH:
        print(
            f"[ERROR] Skill name '{skill_name}' is too long ({len(skill_name)} characters). "
            f"Maximum is {MAX_SKILL_NAME_LENGTH} characters."
        )
        sys.exit(1)
    if skill_name != raw_skill_name:
        print(f"Note: Normalized skill name from '{raw_skill_name}' to '{skill_name}'.")

    explicit_resources = parse_resources(args.resources)
    resources = merge_blueprint_resources(args.blueprint, explicit_resources)
    if args.examples and not resources:
        print("[ERROR] --examples requires --resources to be set.")
        sys.exit(1)

    path = args.path

    print(f"Initializing skill: {skill_name}")
    print(f"   Location: {path}")
    print(f"   Blueprint: {args.blueprint}")
    if resources:
        print(f"   Resources: {', '.join(resources)}")
        if args.examples:
            print("   Examples: enabled")
    else:
        print("   Resources: none (create as needed)")
    print()

    result = init_skill(
        skill_name,
        path,
        explicit_resources,
        args.examples,
        args.interface,
        args.blueprint,
    )

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
