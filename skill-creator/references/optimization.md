# Skill Optimization

Optimize only after identifying which layer is failing. A description-routing problem, an instruction-following problem, a missing deterministic helper, and an inefficient workflow need different changes.

## Optimization targets

| Layer | Evidence of failure | Candidate changes | Primary metrics |
|---|---|---|---|
| Description | Missed intended activations or plausible false activations | Scope wording, trigger contexts, near-miss boundary | Recall, precision, false-positive/negative cases |
| Instructions | Skill loads but behavior is wrong, inconsistent, or over-constrained | Order, defaults, conditions, rationale, gotchas | Assertion pass rate, human preference, variance |
| References | Agent lacks conditional facts or loads too much irrelevant content | Split, route, refresh, or remove references | Correctness, context use, stale-fact findings |
| Scripts | Repeated code, fragile mechanics, or nondeterministic results | Add, simplify, validate, or replace a helper | Deterministic checks, error rate, runtime |
| Assets | Output repeatedly recreates or drifts from an expected form | Add or repair templates and examples | Render/format checks, human review |
| Orchestration | Roles duplicate work, leak labels, or block unnecessarily | Change role boundaries, parallelism, or handoffs | Wall time, token use, independence, completion |

Change one target layer per experiment whenever practical.

## Dataset design

For description optimization, use realistic queries with a balanced mix of:

- explicit and implicit positive cases;
- short and context-rich requests;
- casual language, typos, paths, and project details;
- hard near-misses sharing domain vocabulary;
- conflicts where another skill should win;
- requests where no skill should load.

Use three partitions:

1. **Training**: inspect failures and use them to propose revisions.
2. **Validation**: compare candidates; do not expose individual failures while drafting.
3. **Fresh holdout**: run once after selecting a candidate to estimate generalization.

Keep partitions fixed across candidate comparisons. Record the random seed or explicit IDs. Never move a difficult case after seeing the result.

For behavior optimization, use paired old/new or baseline/candidate runs with equivalent prompts, artifacts, tools, model configuration, and permissions. Randomize labels for subjective comparison.

## Candidate loop

1. Record the original text and baseline metrics.
2. Classify each failure by root cause.
3. Propose a small candidate change and state the expected mechanism.
4. Run training cases and reject obvious regressions.
5. Run validation cases without using their details to revise the same candidate.
6. Compare quality, safety, time, and token cost.
7. Keep the best validated candidate, not automatically the latest one.
8. Run the fresh holdout once.
9. Apply the candidate only if it meets the predeclared acceptance rule.
10. Preserve the candidate history and rejected alternatives.

If several changes are necessary, sequence them and retain an ablation trail so the useful change remains identifiable.

## Routing metrics

Let:

- true positive: should trigger and did trigger;
- false negative: should trigger and did not;
- false positive: should not trigger but did;
- true negative: should not trigger and did not.

Report recall and precision with raw counts. Accuracy alone can hide a description that never triggers. Repeated runs estimate activation rate for nondeterministic routing, but the run count should reflect decision importance and cost.

Treat the skill as triggered only when evidence shows the intended `SKILL.md` was loaded. Naming the skill in a prompt is not proof.

## Behavior and efficiency metrics

Report:

- assertion pass counts and evidence;
- human or blind preference for subjective qualities;
- sample size and incomplete pairs;
- mean and sample standard deviation when repeated runs exist;
- duration and token deltas;
- safety and scope regressions separately from average quality;
- per-scenario patterns hidden by an aggregate.

Never trade a failed safety boundary for a higher average score. Avoid combining unrelated dimensions into one opaque score unless the weighting was defined before results were seen.

## Guarding against overfitting

- Generalize from the failure category, not a phrase copied from one prompt.
- Keep the holdout invisible to the proposal step.
- Reject candidate rules that only mention test fixtures or exact evaluator wording.
- Add a new scenario when a real failure reveals a new class.
- Remove assertions that pass every treatment and do not discriminate.
- Investigate assertions that fail every treatment before changing the skill.
- Recheck near-misses after broadening a description.
- Prefer removing ineffective instructions over accumulating exceptions.

## Stop conditions

Stop when any predeclared condition occurs:

- acceptance thresholds are met;
- two consecutive candidates fail to improve validation results materially;
- the remaining failures are mislabeled, unverifiable, or outside scope;
- cost or run-count budget is reached;
- a safety or compatibility blocker requires user input;
- added complexity costs more than the measured gain.

Report why optimization stopped and whether the final claim is measured, human-judged, or inferred.
