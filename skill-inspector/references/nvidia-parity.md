# NVIDIA SkillSpector Compatibility and Hardening

This integration deliberately keeps NVIDIA SkillSpector as the detection engine instead of reimplementing its rules. With a trusted compatible CLI installed, finding generation, MCP analysis, supply-chain checks, risk scoring, suppression handling, and optional provider analysis therefore remain NVIDIA behavior. The local adapter adds orchestration and consumer-side controls around that engine.

## Reviewed baseline

- Reference tree: the user-supplied `SkillSpector-main` folder.
- Declared version: `2.11.2`.
- Source identity: no Git metadata was present, so no commit hash is claimed.
- License: Apache License 2.0.
- Live-runtime status during development: the CLI was not installed in the host environment, and the source checkout lacked its Python dependencies. Compatibility was established by reviewing the 2.11.2 CLI/report implementation and exercising a contract-faithful fake CLI; do not describe that as a live upstream scan.

Rerun the adapter and contract tests against the real CLI after installing or upgrading SkillSpector through an independently authorized workflow.

## Preserved behavior

| NVIDIA behavior | Local handling |
|---|---|
| Static scan with `--no-llm --format json` | Preserved as the default command line. |
| Manual source review after scanning | Required and represented as a structured semantic-review object. |
| Missing CLI falls back to manual review | Preserved, but manual fallback can never create automatic approval. |
| Exit status alone is not a verdict | Preserved; upstream exits `0` and `1` are interpreted with report fields. Exit `2` is an error. |
| Scores are posture rather than proof | Preserved; score, severity, recommendation, maximum finding severity, and source review are considered separately. |
| Complete finding and suppression evidence | Preserved in the untouched raw JSON and normalized projection, including occurrence-expanded rows. |
| LLM analysis is optional | Preserved behind explicit `--allow-llm`; static mode remains the default. |

## Additional controls

The adapter and connected installer add controls absent from the companion guide:

- shell-free, argv-based Windows/Linux/macOS execution;
- trusted scanner resolution and rejection of target- or current-directory scanner hijacking;
- an immutable retained snapshot and a versioned content identity covering bytes, paths, empty directories, and executable bits;
- bounded raw output, stdout, stderr, version probing, runtime, and child-process termination;
- a static environment allowlist, an LLM-provider allowlist, unrelated-secret removal, and tracing disablement;
- explicit OSV and provider data-egress disclosure;
- validation of execution success, completeness ledgers, analyzer states, LLM degradation, transitive truncation, risk bands, maximum severity, suppression counts, and finding identities;
- raw-report, snapshot, and canonical semantic-review integrity checks;
- a digest-bound `skill-inspection/v1` handoff for creator and installer;
- a two-phase prepare/inspect/commit install transaction with fresh hashing and batch rollback;
- hard blocking for HIGH, CRITICAL, suppressed HIGH/CRITICAL, or `DO_NOT_INSTALL` evidence. Semantic review cannot downgrade these install/release gates.

These controls are intentionally more conservative than NVIDIA's narrative rubric. They do not claim that static analysis is complete, that a digest authenticates the reviewer, or that a passing report makes runtime execution safe.

## Regression evidence

The local test suite covers safe, dangerous, incomplete, failed, malformed, suppressed, LLM-enabled, transitive-truncated, oversized-output, timeout, target-mutation, symlink/junction, scanner-hijack, risk-boundary, occurrence-expansion, semantic-tamper, staged-install, batch-rollback, and curated-exemption cases. The installer independently recomputes the same tree identity and revalidates report cross-fields rather than trusting the adapter's final labels.
