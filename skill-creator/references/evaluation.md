# Codex Skill Evaluation

Use this workflow for skills whose trigger boundary, operational behavior, or release risk deserves retained evidence. It is deliberately optional for small and low-risk edits.

## Evaluation contract

Evaluate three separate questions:

1. **Routing:** Does the skill activate for intended requests and stay out of plausible near-misses?
2. **Behavior:** When loaded, does it improve observable task outcomes without violating scope or safety constraints?
3. **Efficiency:** Is any quality gain worth the added time and token cost?

Static validation cannot answer these questions. Conversely, a successful live run does not replace syntax, metadata, or package-integrity checks.

## Preflight

Before running model evaluations:

- State the skill path or installed skill identity exactly. Duplicate names can load the wrong skill.
- Use a temporary workspace for generated artifacts and keep paired runs independent.
- Keep model, reasoning effort, input artifacts, and available tools equivalent between variants.
- Do not run production mutations, send messages, purchase resources, or incur material cost without the authorization those actions normally require.
- Define the run count and stopping condition before starting. Increase repetitions only when variance or decision importance warrants the expense.
- Record evidence that the skill-assisted run actually read the intended `SKILL.md`; a prompt that merely names the skill is insufficient proof.

## Retained scenario schema

Store maintained scenarios in `evals/evals.json`:

```json
{
  "version": 1,
  "skill": "example-skill",
  "scenarios": [
    {
      "id": "positive-basic",
      "prompt": "A realistic user request",
      "should_trigger": true,
      "risk": "low",
      "holdout": false,
      "assertions": [
        {
          "id": "observable-result",
          "kind": "outcome",
          "description": "A concrete, evidence-backed success criterion"
        }
      ]
    }
  ]
}
```

Supported assertion kinds are `outcome`, `process`, `safety`, and `trigger`. Assertions should be independently checkable from the final response, command output, or produced artifacts. Avoid exact phrasing requirements unless the wording is itself the contract.

Include, when relevant:

- ordinary positive cases;
- boundary and near-miss cases that should not trigger;
- malformed or adversarial inputs;
- permission and scope boundaries;
- platform-specific behavior;
- at least one held-out case not used while drafting the instructions.

Do not encode the expected answer in the prompt or give the evaluator the suspected bug. Keep fixtures minimal and raw.

## Paired execution

For each scenario, run two independent variants:

- `baseline`: Codex receives the request and equivalent artifacts but not the skill instructions.
- `with_skill`: Codex receives the same request and is explicitly given the intended skill path or installed identity.

Randomize order when practical. Use fresh task context and fresh output directories so one run cannot leak conclusions or artifacts into the other. For trigger-only tests, assess routing separately from task quality.

Grade each assertion from evidence. A human or blinded reviewer is preferable for subjective quality; deterministic checks are preferable for parseability, file existence, schema, safety invariants, and numeric outcomes. Record failures honestly rather than repairing outputs before grading.

## Results format and aggregation

Write one JSON object per run to a JSONL file:

```json
{"scenario_id":"positive-basic","variant":"baseline","run_id":"b1","passed":false,"score":0.4,"duration_seconds":31.2,"tokens":4200,"assertions":[{"id":"observable-result","passed":false,"evidence":"The required artifact is absent from the output directory."}],"notes":"Missed the required artifact"}
{"scenario_id":"positive-basic","variant":"with_skill","run_id":"s1","passed":true,"score":0.9,"duration_seconds":34.8,"tokens":4700,"assertions":[{"id":"observable-result","passed":true,"evidence":"artifact.json exists and passes the schema check."}],"notes":"All assertions supported by evidence"}
```

`score`, `duration_seconds`, `tokens`, and `notes` are optional; `score` must be between 0 and 1. When `--evals` is supplied, every run must record each scenario assertion with a Boolean result and non-empty evidence, and the overall `passed` value must equal the conjunction of those assertions. Aggregate without executing HTML or JavaScript:

```powershell
python scripts/benchmark_evals.py results.jsonl --evals <skill-directory>/evals/evals.json --format markdown --output benchmark.md
python scripts/benchmark_evals.py results.jsonl --evals <skill-directory>/evals/evals.json --format json
```

Review pass-rate and mean-score deltas alongside sample standard deviation, mean duration, mean tokens, and incomplete pairs. Do not claim an improvement from a single cherry-picked run or from unsupported evaluator prose.

## Acceptance decision

Set release thresholds before examining results. A reasonable threshold depends on impact: a formatting helper may tolerate an occasional miss, while a mutation or security workflow should require all safety assertions to pass.

If results regress, first determine whether the scenario, assertion, task environment, or skill is wrong. Make the smallest supported change and rerun affected cases plus the held-out set. Preserve prior result files when they are part of a release record.

Avoid viewers that interpolate untrusted output into executable HTML, scripts that kill unknown processes or ports, and orchestration that assumes Unix-only I/O behavior. Markdown and JSON reports are the portable default.

## Evaluation depth

Choose the lightest level that can support the decision:

| Level | Use when | Minimum retained evidence |
|---|---|---|
| Structural | A tiny low-risk edit cannot alter routing or workflow behavior | Strict validation, affected deterministic tests, and the reason behavioral evaluation was unnecessary |
| Focused regression | A localized defect or augmentation has a known failure | Reproduction case, nearby boundary case, before/after result, and artifact evidence |
| Paired evaluation | A new skill or material routing/instruction change is being assessed | Independent baseline and assisted runs, positive and near-miss cases, assertion grades, and a held-out case |
| Release benchmark | The skill is high-impact, broadly distributed, synthesized, optimized, or expensive | Repeated paired runs, blind review where useful, variance and regression analysis, thresholds, and a reproducible release record |

Static validation supports structural claims. Model runs support behavioral claims. Neither substitutes for the other.

## Role separation

Use these contracts even when one person fills several roles:

| Role | Owns | Must not do |
|---|---|---|
| [Evaluation Designer](../agents/eval-designer.md) | Scenarios, assertions, fixtures, and data splits | Tune assertions after seeing candidate identity |
| [Executor](../agents/executor.md) | One isolated treatment and its factual run record | Grade or repair its output |
| [Grader](../agents/grader.md) | Assertion decisions from actual evidence | Rerun, edit, or favor a treatment |
| [Comparator](../agents/comparator.md) | Blind A/B preference under a fixed rubric | Infer identities or force a winner |
| [Benchmark Analyst](../agents/benchmark-analyst.md) | Aggregate metrics, confounders, and decision support | Hide denominators or overstate causality |
| [Optimizer](../agents/optimizer.md) | Controlled candidate iteration | Repeatedly consume held-out cases |

Independent grading is preferred for consequential release decisions.

## Run plan

Predeclare the following before execution:

- exact skill path and candidate revision or digest;
- model, reasoning effort, available tools, and environment;
- scenarios and train, validation, holdout, or regression split;
- variants, runs per variant, and pairing rule;
- timeout, retry, failure, and invalid-treatment policy;
- primary metrics, mandatory safety gates, and release thresholds;
- budget and stopping conditions;
- executor, grader, comparator, and reviewer identities or methods.

Timestamp later amendments and distinguish them from the original plan. Do not add runs only because an early result is unfavorable.

## Data splits

- `train` cases may be inspected while diagnosing and drafting.
- `validation` cases select among candidates.
- `holdout` cases are inspected only for the final decision.
- `regression` cases permanently retain confirmed failures after repair.

The compact schema uses the Boolean `holdout` field; record richer split labels in `tags` when optimization is expected. Never rewrite a holdout case after observing a candidate. Version it and create a new holdout instead.

## Workspace initialization

Create an isolated execution plan without invoking a model:

```powershell
python scripts/init_eval_workspace.py <skill-directory> --output <workspace> --iteration 1 --runs 2
```

The initializer validates `evals/evals.json`, writes `run-plan.json`, and creates empty per-run output directories. A stable identity is:

```text
<scenario_id>/<variant>/<run_id>
```

Fixtures should be read-only where practical. Baseline and assisted runs must not share conversational state or generated artifacts.

## Evidence grading

The grader should inspect actual artifacts instead of relying on final-response claims. For every assertion:

1. locate the claimed evidence;
2. inspect the artifact, field, command result, or transcript event directly;
3. decide pass or fail under the same standard for every treatment;
4. cite concise evidence and its location;
5. report missing or ambiguous evidence.

A partial-quality score cannot override a mandatory failed safety assertion. Evaluation-design defects belong in a separate field; they are not proof that the task succeeded.

## Blind comparison

Use blind comparison only for dimensions that deterministic assertions capture poorly, such as clarity, usefulness, visual quality, or maintainability:

- hide treatment names and candidate metadata;
- randomize or record A/B order;
- fix the rubric and weights before inspection;
- allow ties;
- require evidence for material preferences;
- reveal identities only after judgment.

Presentation polish must not outweigh correctness or safety.

## Local review viewer

Launch the localized reviewer against canonical results:

```powershell
python eval-viewer/generate_review.py <workspace>/iteration-1 --evals <skill-directory>/evals/evals.json --results <workspace>/iteration-1/results.jsonl --benchmark <workspace>/iteration-1/benchmark.json
```

Create a static, read-only report when a server is undesirable:

```powershell
python eval-viewer/generate_review.py <workspace>/iteration-1 --evals <skill-directory>/evals/evals.json --results <workspace>/iteration-1/results.jsonl --static <workspace>/review.html
```

The local server binds only to loopback. If the requested port is occupied, it selects an available loopback port and never terminates another process. Feedback writes are atomic and confined to the review workspace. Treat displayed output and saved feedback as untrusted text.

## Decision hierarchy

Apply predeclared thresholds in this order:

1. mandatory safety and authorization assertions;
2. core task correctness;
3. routing precision and recall;
4. regression tolerance;
5. efficiency and maintenance cost;
6. subjective preference.

Valid outcomes include ship, revise, collect more evidence, narrow scope, or stop. Preserve the eval definitions, original plan and amendments, raw runs, grades, benchmark, comparison, candidate identity, human feedback, limitations, and final rationale. Do not package the evaluation workspace inside the skill.

## Bias and integrity controls

| Risk | Control |
|---|---|
| Treatment leakage | Independent contexts and anonymized comparisons |
| Confirmation bias | Predeclared assertions and evidence-only grading |
| Overfitting | Train, validation, and holdout separation |
| Selective reruns | Fixed retry policy and retained failed attempts |
| Weak evidence | Direct artifact inspection and evidence paths |
| Model or environment drift | Recorded settings, versions, and deviations |
| Safety averaged away | Mandatory fail-closed safety gates |
| Viewer-induced execution | Render untrusted content inertly; never execute it |
| Hidden platform bias | Use platform-neutral assertions unless platform behavior is itself the contract |

The objective is not to prove that a skill is good. It is to make the decision reproducible and open to disconfirmation.
