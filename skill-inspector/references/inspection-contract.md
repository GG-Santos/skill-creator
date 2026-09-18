# Inspection Contract v1

Use this reference when running the adapter, writing semantic-review input, consuming `inspection.v1.json`, or connecting an install or release gate.

## Artifact set

The caller selects one report directory outside the inspected target:

| File | Purpose | Trust posture |
|---|---|---|
| `inspection.v1.json` | Versioned normalized handoff and final gate | Validate before use |
| `snapshot/…` | Retained read-only bytes actually scanned | Hash and compare before use |
| `skillspector.raw.json` | Unmodified upstream JSON evidence | Untrusted data; never execute fields |
| `skillspector.stdout.txt` | Captured process stdout | Untrusted diagnostic text |
| `skillspector.stderr.txt` | Captured process stderr | Untrusted diagnostic text |
| semantic-review JSON | Agent-authored source review supplied to `finalize` | Validate; retain when auditability matters |

The normalized report records the SHA-256 of the raw upstream report, the original semantic-review input, and a canonical digest of the embedded normalized semantic review. It does not rewrite the upstream file. These digests detect mismatches and accidental tampering; they do not authenticate who performed the review.

## Commands and exits

```text
python scripts/run_inspection.py scan TARGET --output-dir OUTPUT_DIR
python scripts/run_inspection.py scan TARGET --output-dir OUTPUT_DIR --allow-llm
python scripts/run_inspection.py finalize OUTPUT_DIR/inspection.v1.json SEMANTIC_REVIEW.json
python scripts/run_inspection.py validate OUTPUT_DIR/inspection.v1.json --target TARGET
```

`scan` exit codes:

| Exit | Meaning |
|---:|---|
| `0` | A parseable upstream result was normalized; semantic review is still required |
| `2` | Inspection error; an error report is written when target hashing succeeded |

`finalize` exit codes:

| Exit | Meaning |
|---:|---|
| `0` | Final gate is `ALLOW` |
| `1` | Final gate is `PROMPT` or `BLOCK`; the report is valid evidence |
| `2` | Invalid/stale input or processing error; do not consume as a final decision |

`validate` returns `0` only for a state-consistent v1 report whose retained raw evidence and snapshot still match. An `ALLOW` report additionally requires `--target` so freshness is recomputed against the exact current bytes.

## Normalized report

The canonical discriminator is:

```json
{ "schema_version": "skill-inspection/v1" }
```

Required top-level fields are:

- `schema_version`: exactly `skill-inspection/v1`;
- `generated_at` and optional `updated_at`: UTC ISO-8601 timestamps;
- `status`: `pending_semantic_review`, `complete`, `partial`, or `error`;
- `target`: source, resolved local path, kind, digest algorithm, digest, entry count, and bytes. Directory identity uses `sha256-tree-v2`, which covers file contents, relative file and directory paths, empty directories, and executable bits;
- `artifacts`: report names and evidence digests;
- `scanner`: availability, executable, version, mode, upstream exit code, and completeness;
- `data_egress`: facts for remote fetching, OSV coordinates, provider contents, and removed static-mode secrets;
- `upstream`: skill identity, risk assessment including `max_issue_severity`, issues, suppression evidence, complete metadata including LLM state, top-level execution success, and the complete `analysis_completeness` object;
- `semantic_review`: pending marker or finalized review;
- `semantic_review_sha256`: canonical SHA-256 binding the embedded finalized review;
- `combined_verdict`: null or `APPROVE`, `CAUTION`, `REJECT`;
- `gate_decision`: `BLOCK_PENDING_REVIEW`, `ALLOW`, `PROMPT`, or `BLOCK`;
- `failure`: null or a bounded error record;
- `limitations`: explicit strings.

Paths in upstream findings are evidence strings, not authority to read outside the selected target. Resolve and verify any referenced file before opening it.

## Semantic-review input

Write UTF-8 JSON no larger than 2 MiB:

```json
{
  "verdict": "CAUTION",
  "summary": "The network call is purpose-aligned but requires a user-supplied token.",
  "reviewed_files": [
    "SKILL.md",
    "scripts/client.py",
    "requirements.txt"
  ],
  "sensitive_surfaces": [
    "Reads SERVICE_TOKEN",
    "Sends task data to api.example.test"
  ],
  "finding_judgments": [
    {
      "id": "finding-opaque-identifier",
      "disposition": "explained",
      "rationale": "scripts/client.py:42 sends only the requested document to the documented endpoint."
    }
  ],
  "guardrails": [
    "Use a scoped token and review the destination before first use."
  ],
  "limitations": []
}
```

All fields shown are required except the four lists may be empty. `reviewed_files` must be non-empty. Each finding judgment's `id` must equal the scanner issue's logical `finding_id`, not its reusable rule `id`; it also needs one disposition from `explained`, `false_positive`, `mitigated`, or `unresolved`, and a non-empty rationale. NVIDIA can repeat one logical `finding_id` across occurrence-expanded issue rows, and one judgment covers every row with that identity.

`APPROVE` requires a non-unresolved judgment for every scanner finding, an upstream `SAFE` recommendation, no suppression evidence, and no HIGH or CRITICAL evidence. Any HIGH, CRITICAL, suppressed HIGH/CRITICAL, or `DO_NOT_INSTALL` evidence forces `REJECT`/`BLOCK` even if semantic review labels it false positive or mitigated. Low- or medium-severity suppression evidence prevents `APPROVE` and yields at least `CAUTION`/`PROMPT`. `REJECT` always maps to `BLOCK`.

When scanner execution or coverage is incomplete, unavailable, or reports an entirely uninspected file, `APPROVE` is invalid. Manual fallback must use `CAUTION` or `REJECT` and maps only to `PROMPT` or `BLOCK`.

## Connection contract

```text
creator structural validation
  -> inspector scan
  -> source-aware semantic review
  -> finalized inspection.v1.json
  -> creator release decision
```

The target digest plus scanner version and mode form the inspection identity. Reuse is allowed only when all three match, the snapshot and raw evidence remain intact, and `validate --target` succeeds. A changed path, byte, empty directory, executable bit, or target type invalidates the handoff. Do not retry a deterministic adverse finding. Retry a timeout or transient scanner failure at most once against the unchanged digest, and retain the failed artifact.

The consumer owns the release or installation action. The inspector owns only evidence and the gate. Stop when the gate is accepted, the user declines a `PROMPT`, a `BLOCK` is reached, or cancellation occurs.

## Upstream compatibility

The adapter normalizes SkillSpector's skill identity, risk assessment, components, structured summaries, complete finding records, suppression evidence, metadata, execution status, and completeness ledger. It retains tags, evidence, fingerprints, occurrences, provenance, the complete `metadata`, `suppressed`, and `analysis_completeness` structures while consumers use only documented fields. Unknown extra top-level fields remain in `skillspector.raw.json`. Inconsistent risk bands, maximum severity, execution state, completeness counts, analyzer status, limitations, or suppression evidence fail closed instead of being guessed.

The subprocess environment is allowlisted. Static mode receives no provider credentials. Explicit LLM mode additionally receives documented NVIDIA SkillSpector/provider variables (including required provider credentials), but unrelated inherited tokens and secrets remain excluded. Tracing variables are disabled in both modes.

The initial compatibility target is NVIDIA SkillSpector `2.11.2`, the version declared by the reviewed local snapshot. Record the actual `--version` output on every run and rerun contract tests after an upstream CLI or JSON-schema change.
