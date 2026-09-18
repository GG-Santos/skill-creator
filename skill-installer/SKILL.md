---
name: skill-installer
description: List and install Codex skills into $CODEX_HOME/skills from the OpenAI skills catalog or a GitHub repository path. Use when a user asks what installable skills exist or requests installation from a public or private GitHub repository. Stages and validates exact bytes, and connects policy-required pre-install security gates to skill-inspector.
license: Apache-2.0
metadata:
  short-description: List and safely install Codex skills
---

# Codex Skill Installer

List or install Codex skills from `openai/skills` or another user-specified GitHub repository. Preserve the requested source, revision, destination, and scope. For sources that require security inspection, stage and validate the exact candidate bytes, obtain a digest-bound `skill-inspection/v1` report from `$skill-inspector`, verify that report, and only then commit the installation transaction.

## Choose the operation

- When the user asks what is available or invokes this skill without naming a skill, list the curated catalog.
- When the user names a curated skill, install it from `skills/.curated/<skill-name>` in `openai/skills`.
- When the user requests experimental skills, use `skills/.experimental` and label that source.
- When the user provides another GitHub repository or URL, use that exact source and path. Private repositories can use existing Git credentials or `GITHUB_TOKEN`/`GH_TOKEN`.
- Skills under `skills/.system` are bundled with Codex. Explain that they are already installed unless the user explicitly requests a separate copy.

Catalog listing and source-acquisition commands use the network. Commit-plan and local report verification do not. Follow the current host's network and authorization rules.
The bundled scripts require Python 3.10 or newer and PyYAML; Git transport also requires Git on `PATH`.

## Commands

Run the bundled scripts with Python:

```bash
python scripts/list-skills.py
python scripts/list-skills.py --format json
python scripts/list-skills.py --path skills/.experimental

python scripts/install-skill-from-github.py --repo <owner>/<repo> --path <path/to/skill>
python scripts/install-skill-from-github.py --url https://github.com/<owner>/<repo>/tree/<ref>/<path>
python scripts/install-skill-from-github.py --repo openai/skills --path skills/.experimental/<skill-name>
python scripts/install-skill-from-github.py --repo <owner>/<repo> --ref feature/topic --path <path/to/skill>

# Recommended two-phase flow for a source that requires inspection
python scripts/install-skill-from-github.py --repo <owner>/<repo> --path <path/to/skill> --prepare-only --plan-output <install-plan.json>
python ../skill-inspector/scripts/run_inspection.py scan <staged-skill> --output-dir <inspection-directory>
python ../skill-inspector/scripts/run_inspection.py finalize <inspection-directory>/inspection.v1.json <semantic-review.json>
python ../skill-inspector/scripts/run_inspection.py validate <inspection-directory>/inspection.v1.json --target <staged-skill>
python scripts/install-skill-from-github.py --commit-plan <install-plan.json> --inspection-report <inspection-directory>/inspection.v1.json

# Direct commit when a final report already covers the fetched bytes
python scripts/install-skill-from-github.py --repo <owner>/<repo> --path <path/to/skill> --inspection-report <inspection.v1.json>
```

The default destination is `$CODEX_HOME/skills`, or `~/.codex/skills` when `CODEX_HOME` is unset. Use `--dest <path>` only when the user requests another location. `--expect-name` can assert the name of one selected skill; it validates the folder and frontmatter and never renames the skill. The former `--name` spelling remains a compatibility alias.

For refs containing `/`, prefer the explicit `--repo ... --ref ... --path ...` form. A pasted GitHub `/tree/<ref>/<path>` URL does not encode where a slash-containing ref ends and the repository path begins.

## Security inspection gate

Read [the inspection-gate contract](references/inspection-gate.md) before installing a source that is not automatically exempt. The installer owns staging and installation; `$skill-inspector` owns read-only security evidence and its gate decision. The installer never treats transport checks, a scanner process exit code, or an aggregate score as approval.

`--inspection-policy` controls the gate:

- `auto` is the default. It skips inspection only for paths under `skills/.curated/` in the official `openai/skills` repository and requires a final report for every other source, including `skills/.experimental/`.
- `required` requires a final report even for the official curated catalog.
- `skip` is an explicit inspection bypass. Use it only when the user or governing policy has accepted that the install will have structural and transport checks but no inspector security verdict.

The recommended two-phase flow avoids time-of-check/time-of-use ambiguity:

1. Run with `--prepare-only --plan-output <plan>` to fetch, validate, hash, and retain the exact candidate bytes without installing them.
2. Read the plan, inspect each staged skill with `$skill-inspector`, complete its source-aware semantic review, and retain each final `inspection.v1.json` outside the untrusted skill.
3. Run `--commit-plan <plan>` and pass one `--inspection-report <report>` per selected skill. Commit-plan mode rehashes the staged candidates, validates every report, and performs no network fetch.

A direct source install may also accept repeatable `--inspection-report` arguments. Each supplied report must cover the exact bytes fetched during that invocation. Never search the target tree for a report or trust a report shipped by the target itself.

Consume reports fail closed:

| Inspector result | Installer action |
|---|---|
| `APPROVE` + `ALLOW` | Install when the report is complete and its digest matches the exact staged bytes. |
| `CAUTION` + `PROMPT` | Block by default. Install only when the user explicitly accepts the documented risk and `--allow-caution` is supplied. |
| `REJECT` + `BLOCK` | Block. `--allow-caution` cannot override it. |
| Missing, stale, malformed, error, incomplete, or `BLOCK_PENDING_REVIEW` | Block. Obtain valid, complete evidence for the unchanged candidate. |

For a batch, validate and inspect every selected skill before installing any of them. One failed gate blocks the entire batch. Any source change after inspection invalidates the digest and requires a fresh report.

## Source and transfer options

- `--ref <ref>` selects the source revision; the default is `main`.
- `--method auto|download|git` selects transport. `auto` downloads public repositories and falls back to sparse Git checkout only for authentication or not-found responses. Git tries SSH only when the HTTPS failure appears authentication-related, and reports both failures if neither works.
- `--archive-sha256 <digest>` verifies the exact codeload ZIP before extraction. It requires `--method download`, which prevents a silent Git fallback from bypassing the expected hash.
- `--max-download-mib`, `--max-entries`, `--max-unpacked-mib`, and `--max-compression-ratio` adjust defensive limits. Defaults are 100 MiB downloaded, 5,000 archive or selected-tree entries, 250 MiB unpacked, and a 1,000:1 per-file ratio. `--max-files` remains an alias for `--max-entries`. Raise a limit only for a source the user intends to install.

After a successful install, report the repository, requested ref, resolved archive digest or Git commit, installed names, and destinations. Tell the user the skills will be available on their next turn.

After a successful prepare-only run, report the plan path, staging path, resolved source identity, and each skill digest, and state plainly that nothing was installed.

## Integrity and installation behavior

The installer:

- streams responses under a configured byte cap and checks a supplied SHA-256 digest before opening the archive;
- rejects traversal, absolute paths, common cross-platform path hazards, Unicode/case-colliding names, file/directory collisions, symbolic links or junctions (including selected-path ancestors), special files, encryption, excessive entry counts or expansion, and suspicious compression ratios;
- requires UTF-8 `SKILL.md` frontmatter with non-empty `name` and `description` and requires the folder, frontmatter, and destination names to match;
- validates every selected skill before copying any of them and, when inspection is required, verifies a complete `skill-inspection/v1` report against the staged tree digest;
- stages all copies beside their final destinations, reserves each destination without replacement, and rolls back destinations created by the batch if a copy fails;
- rechecks candidate identity during the transaction so a post-inspection mutation cannot silently become the installed content;
- never overwrites an existing path, including a dangling symbolic link, and reports any incomplete rollback.

## Trust boundary

Transport and schema checks do not prove that a third-party skill is trustworthy. A skill can contain instructions or scripts that request sensitive tools, external mutations, or data access while remaining structurally valid. Inspection reduces risk but is not a guarantee or sandbox.

For every source covered by the `auto` or `required` inspection policy, use `$skill-inspector` to inspect `SKILL.md`, executable scripts, `agents/openai.yaml`, dependencies, requested tools, and sensitive behavior before installation. Prefer an immutable commit ref and compare a SHA-256 digest received through an authenticated channel. Installation does not grant the skill permission to perform later actions; normal Codex authorization boundaries still apply.

## Listing output

For a human-readable catalog, use a compact numbered list and annotate already installed entries:

```text
Skills from <repo/path@ref>:
1. skill-one
2. skill-two (already installed)
```

If the GitHub API, credentials, Git client, validation, or defensive limits block the request, report the concrete error and do not claim a partial installation succeeded.
