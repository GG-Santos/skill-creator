# Skill Ecosystem Operations

Use this guide when deciding whether to create, augment, synthesize, connect, deprecate, or merely reuse skills.

## Decision taxonomy

| Decision | Choose it when | Reject it when |
|---|---|---|
| **Use existing** | One skill already covers the intent and constraints | The match is only lexical or requires recurring workarounds |
| **Augment** | The core purpose is right and the missing behavior belongs within that coherent scope | The requested behavior has a different audience, lifecycle, dependency, or trigger |
| **Create** | A repeated or high-value gap remains after catalog review | The baseline already succeeds reliably or an existing skill can be configured |
| **Synthesize** | Skills duplicate outcomes, inputs, and trigger territory; one scope can replace them without ambiguity | They only share tools or a broad domain, or their constraints conflict |
| **Connect** | Skills have distinct ownership but one produces a stable artifact the other can consume | The handoff is implicit, circular, or requires loading every skill for ordinary work |
| **Retire** | A replacement has equivalent coverage, migration guidance, and no unresolved dependents | Evidence is incomplete or users still rely on unique behavior |

Lexical similarity is only a discovery signal. Read the full skills and compare behavior before changing lifecycle state.

## Catalog audit

Run `scripts/catalog_skills.py` over explicit roots. Review:

1. exact duplicate names and runtime precedence;
2. near-identical names that may confuse users;
3. description overlap and plausible trigger collisions;
4. broken local references from deep validation, plus a manual review for unused resources;
5. unsupported or runtime-specific frontmatter;
6. duplicated scripts, templates, or policy text;
7. stale dependencies and unverified platform claims;
8. missing evals for important shared behavior.

For each overlap pair, build a capability table:

| Dimension | Skill A | Skill B | Decision relevance |
|---|---|---|---|
| Intended user outcome | | | Same outcome favors synthesis |
| Positive triggers | | | Overlap can create routing collisions |
| Near-miss boundary | | | Conflicts may require narrower descriptions |
| Inputs and outputs | | | Stable output-to-input mapping favors connection |
| Dependencies and permissions | | | Different trust domains favor separation |
| Unique procedures/resources | | | Unique value must survive augmentation or synthesis |
| Release owner/lifecycle | | | Different owners often favor connection |
| Existing evals and users | | | Migration must preserve observed behavior |

## Opportunity scanning (“instinct”)

Opportunity discovery is evidence gathering, not automatic skill creation. Scan only user-supplied files or paths the user explicitly placed in scope.

Useful signals include:

- the same correction appears across sessions;
- a multi-step workflow is repeatedly reconstructed;
- the user repeatedly specifies the same output structure or policy;
- several runs create nearly identical helper scripts;
- tool failures recur with the same recovery sequence;
- users ask which of several skills should handle the same request;
- a skill is repeatedly invoked and then manually overridden;
- a task has stable inputs and outputs but no maintained owner.

The scanner reports clusters, counts, opaque source IDs, redacted keywords, and evidence hashes by default. Raw snippets and absolute input or catalog paths are separate opt-ins because task logs and directory names may contain private data. Use `--include-local-paths` (or the compatibility alias `--include-source-paths`) only when the report needs those locations. Do not scan hidden history, credential stores, or unrelated directories.

Before recommending a new skill, test four alternatives:

1. the base model already handles the task;
2. an existing skill can be used or augmented;
3. a deterministic script, template, or project document is a better abstraction;
4. the task is too rare, unstable, or personal to justify maintained skill context.

## Augmentation protocol

1. Snapshot the original skill and identify its callers.
2. State the observed deficiency and evidence.
3. Classify the change: routing, instructions, reference knowledge, script, asset, metadata, eval, or release.
4. Define what must remain unchanged.
5. Make the smallest coherent change.
6. Run affected tests plus near-miss and holdout cases.
7. Compare old and new behavior if the change is consequential or disputed.
8. Record any migration or changed assumption.

Do not rename the skill, broaden its trigger, replace its license, or regenerate `agents/openai.yaml` wholesale unless the task requires it.

## Synthesis protocol

Synthesis replaces two or more skills with one. Require:

- a shared outcome and compatible audience;
- a capability-union table with no silent loss;
- conflict resolution for instructions, scripts, dependencies, and permissions;
- one discriminating description rather than a union of every keyword;
- a mapping from each legacy scenario to the merged skill;
- a rollback and deprecation plan;
- regression runs for all unique behaviors and near-misses.

Choose a new name only if neither existing name accurately covers the merged scope. Keep old copies until the merged candidate passes the agreed gate. Do not delete or disable originals without explicit authority.

## Connection protocol

Connection preserves independent skills and defines a composition:

```text
request -> owner skill -> handoff artifact -> consumer skill -> final acceptance
```

Document:

- ownership of each stage;
- activation rule and order;
- exact handoff schema and path;
- required and optional fields;
- error and partial-result behavior;
- authority boundaries at each stage;
- idempotency or retry behavior where relevant;
- termination condition and final output owner;
- how to prevent cycles and duplicate work.

Prefer explicit artifacts over shared conversational assumptions. Test each skill independently, validate the handoff contract, then run an end-to-end scenario. A connector should not copy both skills’ instructions into a third monolith.

## Lifecycle record

For material ecosystem changes, retain a small decision record outside the runtime-critical instructions:

```json
{
  "decision": "augment|create|synthesize|connect|retire|no-change",
  "skills": ["skill-a", "skill-b"],
  "evidence": ["catalog finding", "eval run id"],
  "preserved_behaviors": ["behavior id"],
  "migration": "path or null",
  "rollback": "condition and action",
  "status": "proposed|accepted|rejected"
}
```

Do not treat a heuristic score as a lifecycle decision.
