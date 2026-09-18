# Skill-Inspector Installation Gate

Use this contract when installation policy requires security evidence. The boundary is deliberately split: the installer controls source acquisition, structural validation, byte identity, and the filesystem transaction; `$skill-inspector` controls read-only security analysis and emits the versioned decision evidence.

## Ordered handoff

```text
installer fetches candidate
  -> installer structurally validates and stages exact bytes
  -> inspector scans those staged bytes and performs semantic review
  -> inspector emits finalized skill-inspection/v1
  -> installer rehashes bytes and verifies completeness plus verdict
  -> installer transactionally installs the whole batch
```

Neither skill invokes the other implicitly. The owning workflow calls each in order and explicitly passes the report path back to the installer. Store plans and reports outside the untrusted target.

## Policy

`--inspection-policy auto|required|skip` has these meanings:

| Policy | Required evidence |
|---|---|
| `auto` | Skip only an official `openai/skills` path beneath `skills/.curated/`; require inspection for every other source. |
| `required` | Require inspection for every selected skill, including official curated skills. |
| `skip` | Explicitly bypass only the security gate. Transport, path, schema, and transaction checks still apply. |

The exemption is source- and path-specific. A similarly named repository, a fork, `skills/.experimental/`, or a copied curated folder is not exempt. Do not infer `skip` merely because the scanner is unavailable.

## Preferred two-phase workflow

Prepare exact candidates without installing them:

```text
python scripts/install-skill-from-github.py \
  --repo OWNER/REPOSITORY \
  --ref IMMUTABLE_REF \
  --path PATH/TO/SKILL \
  --prepare-only \
  --plan-output INSTALL_PLAN.json
```

The plan uses schema `skill-install-plan/v1`. Treat it as installer state, not as target-authored input. It records source provenance, destination root, retained staging location, each selected skill, and its `sha256-tree-v2` identity. The v2 identity covers content, relative file and directory paths, empty directories, and executable bits. Do not edit the plan or move files into its staging tree.

For each staged path recorded by the plan:

1. Run `$skill-inspector` in its default static mode.
2. Perform source-aware semantic review.
3. Finalize and validate `inspection.v1.json` against the staged target.
4. Retain the report and raw evidence outside the target.

Commit the unchanged plan:

```text
python scripts/install-skill-from-github.py \
  --commit-plan INSTALL_PLAN.json \
  --inspection-report SKILL_ONE/inspection.v1.json \
  --inspection-report SKILL_TWO/inspection.v1.json
```

Commit-plan mode performs no new source fetch. The installer validates the plan, revalidates and rehashes every staged skill, maps explicitly supplied reports by target digest, checks the gates, and only then starts the destination transaction.

## Direct report workflow

When a complete report already covers the same source bytes the installer will fetch, pass it directly:

```text
python scripts/install-skill-from-github.py \
  --repo OWNER/REPOSITORY \
  --ref IMMUTABLE_REF \
  --path PATH/TO/SKILL \
  --inspection-report inspection.v1.json
```

Repeat `--inspection-report` for a batch. Direct mode still stages and hashes the downloaded candidates before consuming reports. A revision label by itself is not byte identity; any digest mismatch blocks the batch. The two-phase workflow is preferable when exact reproducibility matters.

## Report acceptance

Only an explicitly supplied JSON document with `schema_version: skill-inspection/v1` is eligible. Never auto-discover a report in the target. Before relying on one, verify all of the following:

- the report is valid, bounded UTF-8 JSON and has the expected schema;
- `target.algorithm` is `sha256-tree-v2` and the digest, entry count, and byte count match fresh hashes of the exact staged skill and retained inspection snapshot;
- the raw upstream report and retained snapshot still match their recorded digests;
- `status` is `complete`;
- `scanner.available` and `scanner.analysis_complete` are true;
- upstream execution and coverage are complete and no file is entirely uninspected;
- `semantic_review.status` is `complete`;
- the canonical semantic-review digest matches the embedded finalized review;
- the upstream recommendation, maximum issue severity, semantic verdict, combined verdict, and gate decision are mutually consistent;
- the gate is not invalidated by malformed, missing, error, stale, or suppressed high-severity evidence.

Do not use SkillSpector's process exit code as an install decision. Upstream exit `0` can include `CAUTION`, and exit `1` can be a completed adverse result. Consume the normalized fields.

## Gate matrix

| Combined verdict | Gate | Result |
|---|---|---|
| `APPROVE` | `ALLOW` | Eligible to install. |
| `CAUTION` | `PROMPT` | Block unless the user explicitly accepts the reported risk and the invocation includes `--allow-caution`. |
| `REJECT` | `BLOCK` | Block without override. |
| Any verdict or no verdict | `BLOCK_PENDING_REVIEW` | Block until inspection is complete. |

`--allow-caution` is narrow. It cannot accept stale evidence, failed execution, incomplete coverage, malformed reports, `REJECT`, `BLOCK`, HIGH or CRITICAL evidence, or any inconsistency.

## Atomicity, freshness, and recovery

- Validate all selected skills, all target digests, and all inspection gates before creating any final destination.
- Stage destination copies, verify their content identity, then reserve destinations and commit the batch. If a commit copy fails, roll back destinations created by that batch and report any incomplete rollback.
- One rejected, missing, or invalid report prevents every skill in the batch from being installed.
- A changed file, added file, removed file, symlink, special file, or path change invalidates prior evidence. Re-prepare and re-inspect; never reuse the stale approval.
- A target-shipped report, author-shipped baseline, scanner score, repository reputation, or matching skill name is not approval.
- Retry a clearly transient inspection failure at most once against the unchanged digest. Do not retry deterministic adverse findings to seek a different result.

## Data and authority boundaries

Static inspection avoids provider-backed target-content analysis, but SkillSpector dependency checks may send dependency coordinates to OSV.dev. Provider-backed LLM inspection requires explicit content-egress authorization. The installer does not grant either form of egress merely because installation was requested.

An accepted gate authorizes only the requested installation transaction. It does not authorize executing target code, installing scanner dependencies, publishing the skill, exposing credentials, or granting the installed skill broader tool permissions.
