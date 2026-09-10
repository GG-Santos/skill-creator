# Codex Skill Creator and Installer

This repository contains enhanced, drop-in replacements for Codex's bundled
`skill-creator` and `skill-installer` skills.

## Layout

- `skill-creator/` — skill authoring, evaluation, benchmarking, and release tooling.
- `skill-installer/` — defensive GitHub skill discovery and installation tooling.
- `dist/` — deterministic, independently verified `.skill` archives.

The folders intentionally retain the bundled system skill names. Install them as
documented replacements, not alongside another active skill with the same name.

## Verification

```powershell
python -B skill-creator/scripts/validate_skill.py skill-creator --strict
python -B skill-creator/scripts/validate_skill.py skill-installer --strict
python -B skill-creator/scripts/verify_package.py dist/skill-creator.skill --json
python -B skill-creator/scripts/verify_package.py dist/skill-installer.skill --json
```

Each skill is distributed under Apache License 2.0. See
`skill-creator/license.txt` and `skill-installer/LICENSE.txt`.
