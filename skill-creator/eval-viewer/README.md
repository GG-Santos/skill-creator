# Local Evaluation Viewer

This viewer turns canonical skill-evaluation records into an offline review workspace. It supports run-by-run artifact inspection, formal assertion grades, benchmark summaries, reviewer status and severity, file-level notes, feedback export, and responsive desktop/mobile layouts.

## Inputs

| Input | Required | Contract |
|---|:---:|---|
| Iteration workspace | Yes | Root used to resolve every output and transcript path |
| `results.jsonl` | For canonical mode | One record per `scenario_id`, `variant`, and `run_id` |
| `evals.json` or `evals.snapshot.json` | Recommended | Supplies prompts and assertion text |
| `benchmark.json` | Optional | Canonical benchmark from `benchmark_evals.py` |
| Previous workspace | Optional | Legacy prior outputs and feedback for comparison |

If no non-empty canonical results file is present, the generator retains read-only discovery of legacy directories containing an `outputs/` child.

## Run

```powershell
python generate_review.py <iteration-workspace> --results <results.jsonl> --evals <evals.json> --benchmark <benchmark.json>
```

The server binds to `127.0.0.1`. The preferred port is 3117. When that port is already occupied, the viewer chooses a free loopback port without stopping or changing the existing process.

Use `--no-open` to leave browser navigation to the operator. Use `--port 0` to request any available loopback port.

## Static mode

```powershell
python generate_review.py <iteration-workspace> --results <results.jsonl> --evals <evals.json> --static <output-directory>/review.html
```

Static mode copies `viewer.css`, `viewer.js`, and the local skill-creator icons beside the page. It refuses to replace an existing page or neighboring asset unless `--force` is supplied. It has no external font, script, or stylesheet dependency. It is read-only and cannot persist feedback to the source workspace; reviewers can export feedback from the page.

## Canonical mapping

Viewer run IDs are collision-resistant composites:

```text
<scenario_id>--<variant>--<source_run_id>
```

Feedback preserves all three original fields. Assertions from the run record are joined with descriptions and kinds from the eval definition. Output and transcript paths must be relative to and remain within the workspace.

## Safety and resource limits

| Control | Default |
|---|---:|
| Maximum canonical results size | 20 MiB |
| Maximum runs | 2,000 |
| Maximum embedded output files | 500 |
| Maximum single embedded file | 8 MiB |
| Maximum embedded file bytes | 64 MiB |
| Maximum feedback request | 2 MiB |

The generator:

- skips symlinked output files and directories;
- confines resolved output and transcript paths to the workspace;
- escapes less-than, greater-than, ampersand, and JavaScript line separators before embedding JSON;
- renders output and feedback as untrusted content;
- applies restrictive loopback response headers;
- requires the `feedback-1.1` schema for server saves;
- atomically replaces `feedback.json` only after parsing and validation;
- never executes checks or commands named inside eval data.

The viewer is an inspection aid. It does not convert reviewer approval into authorization for deployment, deletion, messaging, purchasing, or other external effects.

## Provenance

The evaluation viewer was authored by the same project owner as this skill and localized for the skill-creator workflow. The owner has licensed it under Apache License 2.0 together with the rest of this skill; see the root `license.txt`.
