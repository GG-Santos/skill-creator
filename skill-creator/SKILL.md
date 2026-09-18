---
name: skill-creator
description: Create and maintain Codex skills, including authoring, material improvement, evaluation, ecosystem analysis, connection, and release work. Use for general routing, quality, portability, or maintainability audits of SKILL.md-based skills. For explicit maliciousness, credential-exfiltration, over-permission, pre-install trust, or security-gate requests, use skill-inspector; consume its report here only when remediation or release is also requested. Do not use for ordinary software features or résumé “skills.”
license: Apache-2.0
metadata:
  short-description: Build and improve Codex skills
---

# Codex Skill Creator

Build skills as lifecycle-managed behavioral assets: discover the right scope, author only what changes Codex's decisions, test observable behavior, improve from evidence, and retain enough provenance to revisit the decision later.

This skill is an orchestrator. It supports small direct edits as well as specialist-agent workflows. Use only the roles and artifacts that materially improve the current task.

Bundled tooling requires Python 3.10 or newer and PyYAML.

## Governing principles

1. **Preserve the user's goal and authority.** A skill may guide work within the requested scope; it does not grant permission for external writes, destructive actions, purchases, credential use, or production mutations.
2. **Separate quality dimensions.** Treat specification conformance, routing accuracy, task behavior, efficiency, security, provenance, and human preference as distinct claims with distinct evidence.
3. **Add information the model lacks.** Favor domain procedures, local constraints, non-obvious failure modes, stable defaults, and reusable resources. Remove generic explanations, duplicated policy, decorative examples, and rules that do not change behavior.
4. **Match control to fragility.** Use judgment-oriented prose when several approaches are valid, parameterized procedures when structure matters, and deterministic scripts when correctness or safety depends on exact execution.
5. **Use progressive disclosure.** Keep shared routing and invariants here. Put conditional procedures, schemas, and examples in directly linked references. Read only the files needed for the selected route.
6. **Prefer evidence over ceremony.** A validator cannot prove usefulness, a panel score cannot prove correctness, and a larger file is not automatically better. Use behavioral deltas, assertion evidence, blind review, and human feedback when the decision warrants them.
7. **Compare neutrally.** When comparing implementations, score method quality independently from target-platform compatibility. Do not reward or penalize a skill merely for naming OpenAI, Anthropic, Claude, Codex, or another vendor.
8. **Design for change.** Record volatile dependencies, extension points, and conditions that should trigger reevaluation. Avoid dated model assumptions unless reproducibility requires an explicitly recorded test configuration.

## Resolve the request before editing

Inspect the current conversation and supplied artifacts first. Identify the requested target, output location, intended users, supported environment, expected outputs, and release audience. For an existing skill, read its complete `SKILL.md`, its routed references, relevant scripts, metadata, evals, and callers before changing it. Respect a user-specified output location; otherwise create new skills under `$CODEX_HOME/skills`, or `~/.codex/skills` when `CODEX_HOME` is unset.

Use a focused interview only for decisions that materially affect architecture or acceptance. Infer answers already present in the conversation. When the interface supports selectable choices, present two or three mutually exclusive choices with a recommended default first. Otherwise state the recommended assumption and continue unless it would change scope or authorize a consequential action.

For platform facts that may have changed, verify current official documentation before encoding them. Distinguish an open Agent Skills rule from a Codex-specific convention and from a local project policy.

## Route the work

Choose one primary route and add supporting routes only when needed.

| Route | Use when | Primary role | Required evidence or artifact |
|---|---|---|---|
| **Discover** | Repeated corrections, recurring workflows, or catalog gaps may indicate a missing skill | [Scout](agents/scout.md) | Opportunity report with recurrence evidence and a do-nothing alternative |
| **Create** | No existing skill adequately covers a demonstrated need | [Architect](agents/architect.md), then [Author](agents/author.md) | Skill specification, implementation, and validation record |
| **Blueprint** | The user wants a reusable starting pattern or several similar skills | [Architect](agents/architect.md) | Chosen blueprint and documented customization points |
| **Augment** | An existing skill is directionally correct but incomplete, unreliable, or inefficient | [Augmenter](agents/augmenter.md) | Baseline snapshot, scoped change map, and regression evidence |
| **Audit** | The user asks for routing, quality, portability, comparison, or maintainability findings | [Reviewer](agents/reviewer.md) | Evidence-linked findings; no silent implementation unless requested |
| **Security gate** | Creation, remediation, or release needs a maliciousness, permission, supply-chain, or pre-install trust decision | `$skill-inspector`, then the owning creator route | Digest-bound `skill-inspection/v1` report; no release from stale, incomplete, or blocked evidence |
| **Evaluate** | Triggering or task behavior needs retained tests | [Eval Designer](agents/eval-designer.md), [Executor](agents/executor.md), [Grader](agents/grader.md) | Evals, isolated runs, assertion evidence, and human review where useful |
| **Benchmark** | A decision requires repeated paired runs, variance, cost, or regression analysis | [Benchmark Analyst](agents/benchmark-analyst.md) | Paired records, aggregate statistics, anomalies, and limitations |
| **Optimize** | Trigger descriptions, instructions, resources, or execution cost should improve | [Optimizer](agents/optimizer.md) | Train/validation separation, candidate history, and stopping rationale |
| **Synthesize** | Two or more skills substantially duplicate purpose and should become one | [Synthesizer](agents/synthesizer.md) | Capability/conflict map, migration plan, merged skill, and equivalence tests |
| **Connect** | Distinct skills should remain independent but cooperate in a defined order | [Connector](agents/connector.md) | Composition contract, handoff schema, ordering rules, and end-to-end tests |
| **Release** | A skill will be distributed, shared, or trusted for higher-impact work | [Release Auditor](agents/release-auditor.md) | Strict validation, verified package, digest, provenance, and known limits |

Do not create a duplicate merely because the requested wording differs. A strong existing match normally routes to use or augment. A partial overlap may call for connection. Synthesis is appropriate only when one coherent scope can replace multiple skills without making discovery ambiguous or the body unwieldy.

## Choose the orchestration depth

Use the lightest level that supports the claim being made:

| Level | Suitable work | Execution style |
|---|---|---|
| **Direct** | Small edits, simple instruction-only skills, deterministic metadata fixes | Work inline; validate changed behavior and files |
| **Structured** | New skills, meaningful augmentation, blueprints, catalog analysis | Produce a short specification, use one or more specialist briefs, and retain decision evidence |
| **Independent** | High-impact releases, disputed comparisons, synthesis, optimization, subjective artifacts | Use fresh-context executors/reviewers when available, blind labels where possible, held-out cases, and human review |

Specialist briefs define roles, not mandatory process. When subagents are unavailable or not authorized, execute the role inline while keeping its inputs, outputs, and conflicts explicit. Never claim independence for a review performed in the author's full context. Do not use same-model unanimity as an approval gate.

## Specialist role protocol

Before delegating a role, read its complete brief and pass only the inputs it requires. Do not give evaluators the desired verdict, hidden labels, or the author's rationale unless their task requires it. Give each writing role a disjoint output scope.

| Role | Read when | Main output |
|---|---|---|
| [Architect](agents/architect.md) | Requirements, research, boundaries, blueprint choice, or a full specification is needed | `skill-spec.md` |
| [Scout](agents/scout.md) | Scanning a catalog or explicitly supplied work history for opportunities | `opportunities.json` or catalog findings |
| [Author](agents/author.md) | Creating a new skill from an approved or sufficiently grounded specification | New skill files |
| [Augmenter](agents/augmenter.md) | Improving an existing skill while preserving working behavior | Change map and updated files |
| [Executor](agents/executor.md) | Running one isolated evaluation scenario | Run directory and run record |
| [Eval Designer](agents/eval-designer.md) | Designing trigger, behavior, safety, or efficiency tests | `evals/evals.json` |
| [Grader](agents/grader.md) | Evaluating assertions against actual outputs and transcripts | `grading.json` |
| [Comparator](agents/comparator.md) | Blindly choosing between two outputs or versions | `comparison.json` |
| [Benchmark Analyst](agents/benchmark-analyst.md) | Interpreting repeated paired results and hidden patterns | Benchmark notes and decision memo |
| [Optimizer](agents/optimizer.md) | Revising triggers or instructions from measured failures | Candidate history and selected revision |
| [Synthesizer](agents/synthesizer.md) | Consolidating overlapping skills | Synthesis plan and merged skill |
| [Connector](agents/connector.md) | Defining cooperation between distinct skills | Composition contract |
| [Reviewer](agents/reviewer.md) | Refuting factual, routing, safety, structural, and maintenance claims | Findings with severity and evidence |
| [Release Auditor](agents/release-auditor.md) | Establishing a reproducible distributable release | Release record and verified package |

## End-to-end workflow

### 1. Inventory and triage

Resolve the exact skill roots and inspect existing candidates before creating anything. For a catalog, run:

```powershell
python scripts/catalog_skills.py <skill-root> [<skill-root> ...] --format markdown
```

Treat lexical overlap as a review lead, not proof that two skills are equivalent. Read [references/ecosystem.md](references/ecosystem.md) when evaluating duplicates, synthesis, connection, deprecation, or skill opportunities.

To scan explicitly supplied transcripts or task logs for repeated friction without automatically reading private history:

```powershell
python scripts/scan_skill_opportunities.py <input-file> [<input-file> ...] --format markdown
```

The scanner must receive explicit paths. It does not search home directories, hidden logs, or external services by default.

### 2. Establish a specification

For meaningful creation or redesign, define:

- problem and intended users;
- positive triggers, near-misses, and competing skills;
- inputs, outputs, side effects, dependencies, and supported environment;
- required behavior, optional behavior, and explicit non-goals;
- failure modes and corresponding instruction form;
- reusable scripts, references, assets, and templates;
- success criteria, evaluation scope, and release threshold;
- provenance, volatility, and extension points.

Use [references/blueprints.md](references/blueprints.md) to select a starting structure. A blueprint is a scaffold, not a quality guarantee. Remove every placeholder and unnecessary resource before release.

Initialize from a blueprint when it saves work:

```powershell
python scripts/init_skill.py my-skill --path <output-directory> --blueprint instruction-only
python scripts/init_skill.py my-skill --path <output-directory> --blueprint script-backed
python scripts/init_skill.py my-skill --path <output-directory> --blueprint evaluation-ready
python scripts/init_skill.py --list-blueprints
```

### 3. Author or augment

For a new skill, implement the specification rather than the analysis transcript. For an update, preserve the original name, invocation policy, dependencies, assets, license, and unrelated behavior unless the requested change requires otherwise.

Write the frontmatter `description` as a concise routing interface: state the capability and realistic contexts in which it should load, then add only boundaries that prevent plausible false positives. Keep it within 1,024 characters. Do not hide trigger information solely in the body because the body is loaded only after activation.

In the body:

- put core procedures and non-obvious gotchas where they are available on every invocation;
- provide a clear default when several approaches exist;
- use conditions tied to observable states;
- state explicit inputs and outputs for fragile workflows;
- route substantial conditional detail to references;
- use scripts for repeated or deterministic mechanics and test them;
- use assets for files copied or adapted into generated output;
- avoid deeply chained references and duplicate guidance.

Use `agents/openai.yaml` for Codex UI metadata, invocation policy, and supported dependencies. Read [references/openai_yaml.md](references/openai_yaml.md) before editing it. Automatic invocation remains enabled unless the user explicitly requests an explicit-only skill.

### 4. Validate structure and integration

Run checks from fast to deep:

```powershell
python scripts/quick_validate.py <skill-directory>
python scripts/validate_skill.py <skill-directory>
python scripts/validate_skill.py <skill-directory> --strict
python scripts/validate_skill.py <skill-directory> --json
```

Run each changed helper on representative success and failure inputs. Inspect its actual output. Validation proves only the invariants implemented by the validator; it does not prove that the skill helps an agent.

### 4a. Connect security inspection when required

Read [references/security-inspection.md](references/security-inspection.md) when the user explicitly asks for a security assessment or when a release candidate contains executable code, network or credential access, MCP definitions, persistence, external downloads, or other high-impact behavior.

Keep ownership explicit: `skill-creator` owns authoring, remediation, validation, packaging, and the final release decision; `$skill-inspector` owns read-only security evidence and the `skill-inspection/v1` report. Inspect the exact validated candidate, verify the report's target digest before relying on it, and rescan after every content change. Never convert a missing scanner, malformed report, incomplete scan, stale digest, `PROMPT`, or `BLOCK` gate into approval.

### 5. Evaluate behavior when warranted

Read [references/evaluation.md](references/evaluation.md) and [references/schemas.md](references/schemas.md) for new, complex, high-impact, repeatedly failing, synthesized, or optimized skills.

Start with realistic scenarios and meaningful assertions. Preserve a holdout for optimization or consequential comparisons. Keep paired conditions equivalent except for the skill treatment. For an existing skill, compare the candidate against an immutable snapshot of the prior version; for a new skill, compare against no skill when that baseline answers the value question.

Create a canonical workspace without running model calls:

```powershell
python scripts/init_eval_workspace.py <skill-directory> --output <workspace> --iteration 1 --runs 1
```

Executors populate isolated run directories. Graders evaluate evidence without repairing outputs. Aggregate paired JSONL records:

```powershell
python scripts/benchmark_evals.py <workspace>/iteration-1/results.jsonl --evals <skill-directory>/evals/evals.json --format json --output <workspace>/iteration-1/benchmark.json
```

Generate the local review UI:

```powershell
python eval-viewer/generate_review.py <workspace>/iteration-1 --results <workspace>/iteration-1/results.jsonl --evals <skill-directory>/evals/evals.json --benchmark <workspace>/iteration-1/benchmark.json
```

The viewer binds only to localhost, never terminates an existing process, and falls back to a free port. Static mode writes local files without external font or script dependencies:

```powershell
python eval-viewer/generate_review.py <workspace>/iteration-1 --static <workspace>/review.html
```

### 6. Optimize from measured failures

Read [references/optimization.md](references/optimization.md). Optimize one layer at a time: routing description, instruction body, resources, or execution cost. Do not tune all layers simultaneously because the source of improvement becomes unidentifiable.

Use training cases to propose changes, validation cases to select among candidates, and a fresh holdout for the final check. Keep candidate history, rejected regressions, run counts, model/tool configuration, and stopping reason. Stop when the acceptance threshold is met, improvement plateaus, cost exceeds the agreed budget, or remaining failures are caused by the eval rather than the skill.

### 7. Synthesize or connect only with a proof obligation

For synthesis, demonstrate that the proposed merged scope is coherent, preserves unique capabilities, has a clearer trigger boundary, and does not create conflicting instructions. Test legacy scenarios against the merged candidate and document migration or deprecation.

For connection, keep ownership separate. Define which skill runs first, the exact handoff artifact, required fields, failure behavior, and the condition that ends the chain. Avoid circular routing and silent loading of unrelated skills.

### 8. Review and release

Run mechanical checks before spending independent-review effort. Ask the reviewer to construct concrete failing cases rather than assign approval scores. Resolve blocker findings and rerun affected scenarios.

For distribution, read [references/release.md](references/release.md):

```powershell
python scripts/package_skill.py <skill-directory> --strict --output <dist/skill-name.skill>
python scripts/verify_package.py <dist/skill-name.skill> --json
```

When the security handoff applies, complete it after the final content change and before packaging. `ALLOW` may continue; `PROMPT` requires an explicit policy or user decision and recorded guardrails; `BLOCK`, incomplete evidence, or a target-digest mismatch stops release. Inspector findings return here for remediation only when the user requested changes, and every remediation requires a fresh inspection.

Retain the package digest, exact source revision when available, test configuration, known limits, and any external dependency assumptions. A valid archive and manifest establish integrity, not publisher identity or safe behavior.

## Acceptance contract

Before declaring completion, report:

1. the selected route and scope;
2. files created or changed;
3. validation and script results;
4. behavioral evidence actually collected, including whether review was independent;
5. benchmark deltas and run counts when measured;
6. security-inspection schema, target digest, completeness, and gate decision when that handoff applied;
7. unresolved limitations, untested claims, and release/provenance status.

Do not claim that a skill is “better” from document length, static lint, or one evaluator's preference. State exactly what was tested and what remains an inference.

## Resource map

| Resource | Read or run when |
|---|---|
| [Blueprint guide](references/blueprints.md) | Choosing or customizing a reusable skill template |
| [Ecosystem operations](references/ecosystem.md) | Discovery, opportunity scanning, augmentation, synthesis, connection, or retirement |
| [Evaluation workflow](references/evaluation.md) | Creating scenarios, paired runs, grades, benchmarks, and human review |
| [Data schemas](references/schemas.md) | Writing or consuming eval, run, grade, benchmark, comparison, or feedback data |
| [Optimization workflow](references/optimization.md) | Tuning descriptions, instructions, resources, or cost without overfitting |
| [Release hardening](references/release.md) | Packaging, provenance, CI, and distribution |
| [Security inspection connection](references/security-inspection.md) | Calling `$skill-inspector`, validating `skill-inspection/v1`, responding to gates, and invalidating stale reports |
| [Codex metadata](references/openai_yaml.md) | Creating or changing `agents/openai.yaml` |
