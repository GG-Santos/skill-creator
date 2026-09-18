# Codex Skill Creator, Inspector, and Installer

This repository contains enhanced, drop-in replacements for Codex's bundled
`skill-creator` and `skill-installer` skills, plus the connected
`skill-inspector` security skill.

## Layout

- `skill-creator/` — skill authoring, evaluation, benchmarking, and release tooling.
- `skill-inspector/` — fail-closed NVIDIA SkillSpector integration plus source-aware security review.
- `skill-installer/` — defensive GitHub skill discovery and installation tooling.
- `dist/` — deterministic, independently verified `.skill` archives.

The creator and installer folders intentionally retain their bundled system skill
names and are installed as replacements. Inspector is a new peer skill; do not
keep another active skill with any of these same names.

## Connected workflow

The three skills remain separate because they own different trust boundaries:

```text
skill-creator authors and structurally validates
  -> skill-inspector scans exact bytes and emits skill-inspection/v1
  -> skill-creator consumes the report for a release decision

skill-installer fetches, structurally validates, and stages exact bytes
  -> skill-inspector scans those staged bytes and emits skill-inspection/v1
  -> skill-installer verifies digest, completeness, and gate
  -> skill-installer transactionally installs the whole batch
```

The installer defaults to requiring inspection for every source except paths
beneath `skills/.curated/` in the official `openai/skills` repository. An
`APPROVE`/`ALLOW` report permits installation; `CAUTION`/`PROMPT` requires an
explicit `--allow-caution`; missing, stale, malformed, error, incomplete, or
rejected evidence blocks the entire batch. See
[`skill-installer/references/inspection-gate.md`](skill-installer/references/inspection-gate.md)
for the two-phase prepare, inspect, and commit protocol.

## Verification

```powershell
python -B skill-creator/scripts/validate_skill.py skill-creator --strict
python -B skill-creator/scripts/validate_skill.py skill-inspector --strict
python -B skill-creator/scripts/validate_skill.py skill-installer --strict
python -B skill-creator/scripts/verify_package.py dist/skill-creator.skill --json
python -B skill-creator/scripts/verify_package.py dist/skill-inspector.skill --json
python -B skill-creator/scripts/verify_package.py dist/skill-installer.skill --json
```

Each skill is distributed under Apache License 2.0. See
`skill-creator/license.txt`, `skill-inspector/LICENSE`, and
`skill-installer/LICENSE.txt`. `skill-inspector/NOTICE` records the NVIDIA
SkillSpector provenance and the local integration changes; the SkillSpector CLI
remains an external dependency. The reviewed baseline and the distinction
between source-level contract testing and a live upstream run are documented in
`skill-inspector/references/nvidia-parity.md`.
