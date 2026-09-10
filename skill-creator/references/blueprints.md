# Skill Blueprints

Blueprints provide a reusable starting shape for recurring skill types. They are intentionally incomplete: the author must replace placeholders, remove unused sections, and validate the resulting skill.

## Selection matrix

| Blueprint | Use when | Default resources | Main risk to check |
|---|---|---|---|
| `instruction-only` | The model needs concise procedural or domain guidance and no deterministic helper | None beyond `agents/openai.yaml` | Generic advice that adds no behavioral value |
| `reference-guided` | The workflow is stable but domain facts, schemas, policies, or variants are loaded conditionally | `references/` | References that are copied manuals, stale, or never routed |
| `script-backed` | A repeated transformation, validation, or fragile operation benefits from deterministic code | `scripts/` and `references/` | Untested scripts, hidden dependencies, or unsafe side effects |
| `artifact-producing` | The skill generates documents, sites, data files, or other reusable outputs from templates | `assets/`, often `scripts/` | Template drift, inaccessible output, or missing render/verification |
| `orchestrator` | One coherent job has multiple substantial modes or specialist roles | `references/` and optionally `scripts/` | Broad triggering, conflicting modes, mandatory ceremony |
| `evaluation-ready` | The skill is high impact, distributed, optimized, synthesized, or expected to evolve from retained evidence | `evals/` and `references/` | Tests that mirror wording, leak answers, or lack a baseline |

Use `instruction-only` by default. Select a larger blueprint only when a concrete workflow requires its resources.

## Initialize

```powershell
python scripts/init_skill.py my-skill --path <output-directory> --blueprint instruction-only
python scripts/init_skill.py my-skill --path <output-directory> --blueprint reference-guided
python scripts/init_skill.py my-skill --path <output-directory> --blueprint script-backed
python scripts/init_skill.py my-skill --path <output-directory> --blueprint artifact-producing
python scripts/init_skill.py my-skill --path <output-directory> --blueprint orchestrator
python scripts/init_skill.py my-skill --path <output-directory> --blueprint evaluation-ready
```

Explicit `--resources` values are merged with the blueprint defaults. `--examples` adds unfinished sample resources and evals; validation must fail until they are replaced or deleted.

The source templates live under `assets/blueprints/`:

- `instruction-only.md.tmpl`
- `reference-guided.md.tmpl`
- `script-backed.md.tmpl`
- `artifact-producing.md.tmpl`
- `orchestrator.md.tmpl`
- `evaluation-ready.md.tmpl`

## Customize in this order

1. Replace the frontmatter description with the capability, realistic trigger contexts, and any necessary near-miss boundary.
2. Define the expected input and output contract.
3. Replace the workflow placeholders with task-specific decisions and defaults.
4. Add non-obvious failure modes and condition-based recovery.
5. Implement and test each retained script.
6. Replace or remove example references and assets.
7. Replace generated eval examples with realistic scenarios and observable assertions.
8. Remove every unused section and directory.
9. Generate or narrowly update `agents/openai.yaml`.
10. Run quick and deep validation.

## Blueprint quality checks

| Check | Pass condition |
|---|---|
| Scope | One coherent job; nearby requests have an explicit routing boundary |
| Novelty | Instructions change behavior beyond what the model reliably does unaided |
| Inputs/outputs | Fragile operations name required inputs, produced artifacts, and failure output |
| Resources | Every script, reference, and asset is used by a routed workflow |
| Portability | Platform-specific assumptions are declared in `compatibility` or isolated behind an adapter |
| Context cost | Core instructions stay in `SKILL.md`; conditional detail is directly discoverable |
| Verification | Deterministic outputs have deterministic checks; subjective outputs have human-review criteria |
| Evolution | Volatile facts have a verification source or an obsolescence trigger |

Do not preserve blueprint headings merely for symmetry. The finished skill should look purpose-built.
