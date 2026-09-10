#!/usr/bin/env python3
"""Generate a local or static Codex skill-evaluation review.

The viewer accepts this skill creator's canonical eval definitions and JSONL
run records. It also retains directory discovery for older evaluation
workspaces. The local server binds to loopback, never terminates an existing
process to claim a port, and atomically persists bounded feedback.
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import webbrowser
from functools import partial
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Files to exclude from output listings
METADATA_FILES = {"transcript.md", "user_notes.md", "metrics.json"}
MAX_EMBED_FILE_BYTES = 8 * 1024 * 1024
MAX_EMBED_TOTAL_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_FILES = 500
MAX_FEEDBACK_BYTES = 2 * 1024 * 1024
MAX_RESULTS_BYTES = 20 * 1024 * 1024
MAX_RUNS = 2000

# Extensions we render as inline text
TEXT_EXTENSIONS = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".yaml", ".yml", ".xml", ".html", ".css", ".sh", ".rb", ".go", ".rs",
    ".java", ".c", ".cpp", ".h", ".hpp", ".sql", ".r", ".toml",
}

# Extensions we render as inline images
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# MIME type overrides for common types
MIME_OVERRIDES = {
    ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# Reuse the skill's maintained visual identity.
_ASSET_DIR = Path(__file__).resolve().parents[1] / "assets"


def get_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_OVERRIDES:
        return MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def find_runs(workspace: Path) -> list[dict]:
    """Recursively discover legacy run directories within the workspace."""
    root = workspace.resolve()
    runs: list[dict] = []
    budget = {"files": 0, "bytes": 0}
    _find_runs_recursive(root, root, runs, budget)
    runs.sort(key=lambda run: (str(run.get("eval_id", "")), run["id"]))
    return runs


def _find_runs_recursive(
    root: Path, current: Path, runs: list[dict], budget: dict[str, int]
) -> None:
    if not current.is_dir() or current.is_symlink():
        return
    outputs_dir = current / "outputs"
    if outputs_dir.is_dir() and not outputs_dir.is_symlink():
        run = build_run(root, current, budget)
        if run:
            runs.append(run)
        return
    skip = {"node_modules", ".git", "__pycache__", "skill", "inputs"}
    for child in sorted(current.iterdir()):
        if child.is_dir() and not child.is_symlink() and child.name not in skip:
            _find_runs_recursive(root, child, runs, budget)


def build_run(
    root: Path, run_dir: Path, budget: dict[str, int] | None = None
) -> dict | None:
    """Build a legacy run record with bounded embedded output."""
    budget = budget or {"files": 0, "bytes": 0}
    prompt = ""
    eval_id = None
    for candidate in [run_dir / "eval_metadata.json", run_dir.parent / "eval_metadata.json"]:
        if candidate.exists():
            try:
                metadata = json.loads(candidate.read_text(encoding="utf-8"))
                prompt = metadata.get("prompt", "")
                eval_id = metadata.get("eval_id")
            except (json.JSONDecodeError, OSError):
                pass
            if prompt:
                break
    if not prompt:
        for candidate in [run_dir / "transcript.md", run_dir / "outputs" / "transcript.md"]:
            if candidate.exists():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    match = re.search(r"## Eval Prompt\n\n([\s\S]*?)(?=\n##|$)", text)
                    if match:
                        prompt = match.group(1).strip()
                except OSError:
                    pass
                if prompt:
                    break
    run_id = str(run_dir.relative_to(root)).replace("/", "-").replace("\\", "-")
    grading = None
    for candidate in [run_dir / "grading.json", run_dir.parent / "grading.json"]:
        if candidate.exists():
            try:
                grading = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
            if grading:
                break
    return {
        "id": run_id,
        "prompt": prompt or "(No prompt found)",
        "eval_id": eval_id,
        "outputs": collect_outputs(root, run_dir / "outputs", budget),
        "grading": grading,
    }


def collect_outputs(
    workspace: Path, outputs_dir: Path, budget: dict[str, int]
) -> list[dict]:
    root = workspace.resolve()
    try:
        output_root = outputs_dir.resolve(strict=True)
    except OSError:
        return []
    if not output_root.is_dir() or not _within(output_root, root):
        return [{"name": "output-error.txt", "type": "error", "content": "(Unsafe output directory omitted)"}]
    output_files: list[dict] = []
    for current, dirs, files in os.walk(output_root, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            directory for directory in dirs
            if not (current_path / directory).is_symlink()
        )
        for filename in sorted(files):
            path = current_path / filename
            if path.name in METADATA_FILES or path.is_symlink():
                continue
            if budget["files"] >= MAX_OUTPUT_FILES:
                output_files.append(
                    {
                        "name": "output-limit.txt",
                        "type": "error",
                        "content": f"(Additional files omitted after {MAX_OUTPUT_FILES} embedded files)",
                    }
                )
                return output_files
            resolved = path.resolve()
            if not _within(resolved, output_root) or not _within(resolved, root):
                continue
            display_name = resolved.relative_to(output_root).as_posix()
            output_files.append(embed_file(resolved, display_name, budget))
    return output_files


def embed_file(
    path: Path, display_name: str | None = None, budget: dict[str, int] | None = None
) -> dict:
    """Read one bounded file and return an inert embedded representation."""
    budget = budget or {"files": 0, "bytes": 0}
    name = display_name or path.name
    try:
        size = path.stat().st_size
    except OSError:
        return {"name": name, "type": "error", "content": "(Error reading file)"}
    budget["files"] += 1
    if size > MAX_EMBED_FILE_BYTES:
        return {
            "name": name,
            "type": "error",
            "content": f"(File omitted: {size} bytes exceeds the per-file limit)",
        }
    if budget["bytes"] + size > MAX_EMBED_TOTAL_BYTES:
        return {
            "name": name,
            "type": "error",
            "content": "(File omitted: review embedding budget exhausted)",
        }
    budget["bytes"] += size
    ext = path.suffix.lower()
    mime = get_mime_type(path)
    try:
        if ext in TEXT_EXTENSIONS:
            return {
                "name": name,
                "type": "text",
                "content": path.read_text(encoding="utf-8", errors="replace"),
            }
        raw = path.read_bytes()
    except OSError:
        return {"name": name, "type": "error", "content": "(Error reading file)"}
    encoded = base64.b64encode(raw).decode("ascii")
    if ext in IMAGE_EXTENSIONS:
        return {"name": name, "type": "image", "mime": mime, "data_uri": f"data:{mime};base64,{encoded}"}
    if ext == ".pdf":
        return {"name": name, "type": "pdf", "data_uri": f"data:{mime};base64,{encoded}"}
    if ext == ".xlsx":
        return {"name": name, "type": "xlsx", "data_b64": encoded}
    return {
        "name": name,
        "type": "binary",
        "mime": mime,
        "data_uri": f"data:{mime};base64,{encoded}",
    }


class ReviewError(Exception):
    pass


def _load_json_object(path: Path, label: str) -> dict:
    if not path.is_file():
        raise ReviewError(f"{label} not found: {path}")
    if path.stat().st_size > MAX_RESULTS_BYTES:
        raise ReviewError(f"{label} exceeds {MAX_RESULTS_BYTES} bytes: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ReviewError(f"Invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewError(f"{label} must contain a JSON object.")
    return data


def load_eval_definitions(path: Path | None) -> tuple[str | None, dict[str, dict]]:
    if path is None:
        return None, {}
    data = _load_json_object(path, "eval definitions")
    skill_name = data.get("skill")
    if skill_name is not None and not isinstance(skill_name, str):
        raise ReviewError("Eval skill name must be a string.")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list):
        raise ReviewError("Eval definitions must contain a scenarios list.")
    result: dict[str, dict] = {}
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ReviewError(f"Eval scenario {index} must be an object.")
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            raise ReviewError(f"Eval scenario {index} has no valid id.")
        if scenario_id in result:
            raise ReviewError(f"Duplicate eval scenario id: {scenario_id}")
        result[scenario_id] = scenario
    return skill_name, result


def _resolve_workspace_file(workspace: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReviewError(f"{label} must be a safe workspace-relative path: {value}")
    resolved = (workspace / relative).resolve()
    if not _within(resolved, workspace):
        raise ReviewError(f"{label} leaves the workspace: {value}")
    return resolved


def _grading_for_record(record: dict, scenario: dict | None) -> dict | None:
    raw_assertions = record.get("assertions")
    if raw_assertions is None:
        return None
    if not isinstance(raw_assertions, list) or not raw_assertions:
        raise ReviewError("Run assertions must be a non-empty list when present.")
    definitions = {
        item.get("id"): item
        for item in (scenario or {}).get("assertions", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    assertions = []
    seen: set[str] = set()
    for raw in raw_assertions:
        if not isinstance(raw, dict):
            raise ReviewError("Every run assertion must be an object.")
        assertion_id = raw.get("id")
        if not isinstance(assertion_id, str) or not assertion_id.strip():
            raise ReviewError("Every run assertion requires a non-empty id.")
        if assertion_id in seen:
            raise ReviewError(f"Run duplicates assertion id: {assertion_id}")
        seen.add(assertion_id)
        if not isinstance(raw.get("passed"), bool):
            raise ReviewError(f"Assertion {assertion_id} requires a Boolean passed value.")
        evidence = raw.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ReviewError(f"Assertion {assertion_id} requires non-empty evidence.")
        definition = definitions.get(assertion_id, {})
        assertions.append(
            {
                "id": assertion_id,
                "text": definition.get("description", assertion_id),
                "kind": definition.get("kind", "outcome"),
                "passed": raw["passed"],
                "evidence": evidence,
                "evidence_paths": raw.get("evidence_paths", []),
            }
        )
    if definitions and seen != set(definitions):
        raise ReviewError(
            f"Run assertion ids do not match eval definitions; expected {sorted(definitions)}, got {sorted(seen)}."
        )
    passed_count = sum(1 for item in assertions if item["passed"])
    return {
        "assertions": assertions,
        "summary": {
            "passed": passed_count,
            "failed": len(assertions) - passed_count,
            "total": len(assertions),
            "pass_rate": passed_count / len(assertions),
        },
    }


def load_canonical_runs(
    workspace: Path, results_path: Path, evals_path: Path | None
) -> tuple[list[dict], str | None]:
    workspace = workspace.resolve()
    if not results_path.is_file():
        raise ReviewError(f"Results file not found: {results_path}")
    if results_path.stat().st_size > MAX_RESULTS_BYTES:
        raise ReviewError(f"Results file exceeds {MAX_RESULTS_BYTES} bytes.")
    skill_name, scenarios = load_eval_definitions(evals_path)
    budget = {"files": 0, "bytes": 0}
    runs: list[dict] = []
    identities: set[tuple[str, str, str]] = set()
    for line_number, raw_line in enumerate(
        results_path.read_text(encoding="utf-8-sig").split("\n"), start=1
    ):
        raw_line = raw_line.removesuffix("\r")
        if not raw_line.strip():
            continue
        if len(runs) >= MAX_RUNS:
            raise ReviewError(f"Results exceed the {MAX_RUNS}-run limit.")
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ReviewError(f"Invalid results JSONL line {line_number}: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise ReviewError(f"Results line {line_number} must be an object.")
        scenario_id = record.get("scenario_id")
        variant = record.get("variant")
        source_run_id = record.get("run_id")
        if not all(isinstance(value, str) and value.strip() for value in (scenario_id, variant, source_run_id)):
            raise ReviewError(
                f"Results line {line_number} requires scenario_id, variant, and run_id strings."
            )
        identity = (scenario_id, variant, source_run_id)
        if identity in identities:
            raise ReviewError(f"Duplicate run identity on line {line_number}: {identity}")
        identities.add(identity)
        if scenarios and scenario_id not in scenarios:
            raise ReviewError(f"Results line {line_number} references unknown scenario: {scenario_id}")
        passed = record.get("passed")
        if not isinstance(passed, bool):
            raise ReviewError(f"Results line {line_number} requires Boolean passed.")
        scenario = scenarios.get(scenario_id)
        grading = _grading_for_record(record, scenario)
        if grading is not None and passed != all(
            item["passed"] for item in grading["assertions"]
        ):
            raise ReviewError(
                f"Results line {line_number} passed value conflicts with assertion results."
            )
        outputs_ref = record.get(
            "outputs_dir", f"{scenario_id}/{variant}/{source_run_id}/outputs"
        )
        if not isinstance(outputs_ref, str):
            raise ReviewError(f"Results line {line_number} outputs_dir must be a string.")
        outputs_dir = _resolve_workspace_file(
            workspace, outputs_ref, f"results line {line_number} outputs_dir"
        )
        outputs = collect_outputs(workspace, outputs_dir, budget)
        transcript_ref = record.get("transcript_path")
        if transcript_ref is not None:
            if not isinstance(transcript_ref, str):
                raise ReviewError(f"Results line {line_number} transcript_path must be a string.")
            transcript = _resolve_workspace_file(
                workspace, transcript_ref, f"results line {line_number} transcript_path"
            )
            if transcript.is_file():
                outputs.append(embed_file(transcript, "transcript.md", budget))
        final_output = record.get("final_output")
        if isinstance(final_output, str) and final_output:
            outputs.append(
                {"name": "final-response.md", "type": "text", "content": final_output}
            )
        outputs.append(
            {
                "name": "run-record.json",
                "type": "text",
                "content": json.dumps(record, indent=2, ensure_ascii=False),
            }
        )
        viewer_id = f"{scenario_id}--{variant}--{source_run_id}"
        runs.append(
            {
                "id": viewer_id,
                "scenario_id": scenario_id,
                "variant": variant,
                "source_run_id": source_run_id,
                "configuration": variant.replace("_", " "),
                "prompt": (scenario or {}).get("prompt", record.get("prompt", "(No prompt found)")),
                "eval_id": scenario_id,
                "outputs": outputs,
                "grading": grading,
                "benchmark": {
                    "passed": passed,
                    "score": record.get("score"),
                    "duration_seconds": record.get("duration_seconds"),
                    "tokens": record.get("tokens"),
                },
            }
        )
    if not runs:
        raise ReviewError("Results file contains no run records.")
    runs.sort(key=lambda run: (run["scenario_id"], run["variant"], run["source_run_id"]))
    return runs, skill_name


def load_previous_iteration(workspace: Path) -> dict[str, dict]:
    """Load previous iteration's feedback and outputs."""
    result: dict[str, dict] = {}

    feedback_map: dict[str, str] = {}
    feedback_path = workspace / "feedback.json"
    if feedback_path.exists():
        try:
            data = json.loads(feedback_path.read_text())
            feedback_map = {
                r["run_id"]: r["feedback"]
                for r in data.get("reviews", [])
                if r.get("feedback", "").strip()
            }
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    prev_runs = find_runs(workspace)
    for run in prev_runs:
        result[run["id"]] = {
            "feedback": feedback_map.get(run["id"], ""),
            "outputs": run.get("outputs", []),
        }

    for run_id, fb in feedback_map.items():
        if run_id not in result:
            result[run_id] = {"feedback": fb, "outputs": []}

    return result


def load_feedback(path: Path) -> dict:
    """Load current feedback.json, returning an empty object when absent/invalid."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def script_json(data: object) -> str:
    """Serialize JSON for inert embedding inside a script element."""
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def generate_html(
    runs: list[dict],
    skill_name: str,
    previous: dict[str, dict] | None = None,
    benchmark: dict | None = None,
    feedback: dict | None = None,
    asset_prefix: str = "/",
    iteration: int | None = None,
) -> str:
    """Generate the complete standalone HTML page with embedded data."""
    template_path = Path(__file__).parent / "viewer.html"
    template = template_path.read_text(encoding="utf-8")
    template = template.replace("__ASSET_PREFIX__", asset_prefix)

    previous_feedback: dict[str, str] = {}
    previous_outputs: dict[str, list[dict]] = {}
    if previous:
        for run_id, data in previous.items():
            if data.get("feedback"):
                previous_feedback[run_id] = data["feedback"]
            if data.get("outputs"):
                previous_outputs[run_id] = data["outputs"]

    embedded = {
        "skill_name": skill_name,
        "iteration": iteration,
        "runs": runs,
        "previous_feedback": previous_feedback,
        "previous_outputs": previous_outputs,
    }
    if benchmark:
        embedded["benchmark"] = benchmark

    data_json = script_json(embedded)
    feedback_json = script_json(feedback or {})
    bootstrap = (
        f"window.EVAL_REVIEW_BOOTSTRAP = {data_json};\n"
        f"window.EVAL_REVIEW_FEEDBACK = {feedback_json};"
    )

    return template.replace("/*__BOOTSTRAP_DATA__*/", bootstrap)


class ReviewHandler(BaseHTTPRequestHandler):
    """Serves the review HTML and handles feedback saves.

    Regenerates the HTML on each page load so that refreshing the browser
    picks up new eval outputs without restarting the server.
    """

    def __init__(
        self,
        workspace: Path,
        skill_name: str,
        feedback_path: Path,
        previous: dict[str, dict],
        benchmark_path: Path | None,
        results_path: Path | None,
        evals_path: Path | None,
        iteration: int | None,
        *args,
        **kwargs,
    ):
        self.workspace = workspace
        self.skill_name = skill_name
        self.feedback_path = feedback_path
        self.previous = previous
        self.benchmark_path = benchmark_path
        self.results_path = results_path
        self.evals_path = evals_path
        self.iteration = iteration
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            try:
                if self.results_path:
                    runs, _ = load_canonical_runs(
                        self.workspace, self.results_path, self.evals_path
                    )
                else:
                    runs = find_runs(self.workspace)
            except (ReviewError, OSError, UnicodeError) as exc:
                self.send_error(500, f"Could not refresh review data: {exc}")
                return
            benchmark = None
            if self.benchmark_path and self.benchmark_path.exists():
                try:
                    benchmark = json.loads(self.benchmark_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
            feedback = load_feedback(self.feedback_path)
            html = generate_html(
                runs,
                self.skill_name,
                self.previous,
                benchmark,
                feedback,
                asset_prefix="/",
                iteration=self.iteration,
            )
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif path in {"/viewer.css", "/viewer.js", "/favicon.ico"} or path.startswith("/assets/"):
            # Assets live in _ASSET_DIR (same dir as this script / assets/)
            if path.startswith("/assets/"):
                asset_root = _ASSET_DIR
                asset_path = asset_root / path.removeprefix("/assets/")
                allowed_root = asset_root.resolve()
            elif path == "/favicon.ico":
                asset_root = _ASSET_DIR
                asset_path = asset_root / "skill-creator-small.svg"
                allowed_root = asset_root.resolve()
            else:
                asset_path = Path(__file__).parent / path.lstrip("/")
                allowed_root = Path(__file__).parent.resolve()
            try:
                asset_path.resolve().relative_to(allowed_root)
            except ValueError:
                self.send_error(404)
                return
            try:
                data = asset_path.read_bytes()
            except OSError:
                self.send_error(404)
                return
            content_type = get_mime_type(asset_path)
            if path.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            elif path.endswith(".js"):
                content_type = "application/javascript; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/feedback":
            data = b"{}"
            if self.feedback_path.exists():
                data = self.feedback_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] == "/api/feedback":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_FEEDBACK_BYTES:
                self.send_error(413, "Feedback payload is too large")
                return
            if "application/json" not in self.headers.get("Content-Type", ""):
                self.send_error(415, "Feedback must use application/json")
                return
            body = self.rfile.read(length)
            temporary_path: Path | None = None
            try:
                data = json.loads(body)
                if not isinstance(data, dict) or not isinstance(data.get("reviews"), list):
                    raise ValueError("Expected JSON object with a reviews array")
                if data.get("schemaVersion") != "feedback-1.1":
                    raise ValueError("Expected schemaVersion feedback-1.1")
                if len(data["reviews"]) > MAX_RUNS:
                    raise ValueError("Feedback contains too many reviews")
                run_ids = [review.get("run_id") for review in data["reviews"] if isinstance(review, dict)]
                if len(run_ids) != len(data["reviews"]) or any(
                    not isinstance(run_id, str) or not run_id for run_id in run_ids
                ):
                    raise ValueError("Every review requires a non-empty run_id")
                if len(set(run_ids)) != len(run_ids):
                    raise ValueError("Feedback contains duplicate run_id values")
                self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".feedback-", suffix=".tmp", dir=self.feedback_path.parent
                )
                temporary_path = Path(temporary_name)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(data, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.feedback_path)
                temporary_path = None
                resp = b'{"ok":true}'
                self.send_response(200)
            except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as e:
                resp = json.dumps({"error": str(e)}).encode()
                self.send_response(400)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress request logging to keep terminal clean
        pass


def _read_run_plan(workspace: Path) -> dict:
    path = workspace / "run-plan.json"
    return _load_json_object(path, "run plan") if path.is_file() else {}


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_static_review(static_path: Path, html_page: str, *, force: bool = False) -> None:
    static_path = static_path.resolve()
    viewer_dir = Path(__file__).resolve().parent
    copies = [
        (viewer_dir / "viewer.css", static_path.parent / "viewer.css"),
        (viewer_dir / "viewer.js", static_path.parent / "viewer.js"),
    ]
    copies.extend(
        (source, static_path.parent / "assets" / source.name)
        for source in (
            _ASSET_DIR / "skill-creator-small.svg",
            _ASSET_DIR / "skill-creator.png",
        )
        if source.is_file()
    )
    destinations = [static_path]
    destinations.extend(
        destination
        for source, destination in copies
        if source.resolve() != destination.resolve()
    )
    collisions = [path for path in destinations if os.path.lexists(path)]
    if collisions and not force:
        rendered = ", ".join(str(path) for path in collisions)
        raise ReviewError(
            f"Static output would replace existing files: {rendered}. Use --force to replace them."
        )
    assets_dir = static_path.parent / "assets"
    if os.path.lexists(assets_dir) and not assets_dir.is_dir():
        raise ReviewError(f"Static asset destination is not a directory: {assets_dir}")

    _atomic_write(static_path, html_page)
    for source, destination in copies:
        if source.resolve() != destination.resolve():
            _atomic_copy(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and serve a Codex skill evaluation review")
    parser.add_argument("workspace", type=Path, help="Iteration workspace directory")
    parser.add_argument("--port", "-p", type=int, default=3117, help="Preferred loopback port")
    parser.add_argument("--skill-name", "-n", type=str, help="Skill name for the header")
    parser.add_argument("--results", type=Path, help="Canonical results.jsonl")
    parser.add_argument("--evals", type=Path, help="Canonical evals.json or eval snapshot")
    parser.add_argument(
        "--previous-workspace",
        type=Path,
        help="Previous workspace whose outputs and feedback provide review context",
    )
    parser.add_argument("--benchmark", type=Path, help="benchmark.json to show")
    parser.add_argument(
        "--static", "-s", type=Path,
        help="Write a static review page instead of starting a server",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow static mode to replace its existing page and asset files",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    args = parser.parse_args()

    try:
        workspace = args.workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise ReviewError(f"Workspace is not a directory: {workspace}")
        if not 0 <= args.port <= 65535:
            raise ReviewError("Port must be between 0 and 65535.")

        auto_results = workspace / "results.jsonl"
        results_path = (
            args.results.resolve()
            if args.results
            else auto_results if auto_results.is_file() and auto_results.stat().st_size else None
        )
        auto_evals = workspace / "evals.snapshot.json"
        evals_path = (
            args.evals.resolve()
            if args.evals
            else auto_evals if auto_evals.is_file() else None
        )
        plan = _read_run_plan(workspace)
        if results_path:
            runs, eval_skill_name = load_canonical_runs(
                workspace, results_path, evals_path
            )
        else:
            runs = find_runs(workspace)
            eval_skill_name = None
        if not runs:
            raise ReviewError(f"No reviewable runs found in {workspace}")

        iteration_value = plan.get("iteration")
        iteration = iteration_value if isinstance(iteration_value, int) else None
        if iteration is None:
            match = re.search(r"iteration-(\d+)$", workspace.name)
            iteration = int(match.group(1)) if match else None
        skill_name = (
            args.skill_name
            or eval_skill_name
            or (plan.get("skill") if isinstance(plan.get("skill"), str) else None)
            or workspace.name.replace("-workspace", "")
        )
        feedback_path = workspace / "feedback.json"
        previous: dict[str, dict] = {}
        if args.previous_workspace:
            previous_workspace = args.previous_workspace.resolve(strict=True)
            if not previous_workspace.is_dir():
                raise ReviewError(f"Previous workspace is not a directory: {previous_workspace}")
            previous = load_previous_iteration(previous_workspace)

        auto_benchmark = workspace / "benchmark.json"
        benchmark_path = (
            args.benchmark.resolve()
            if args.benchmark
            else auto_benchmark if auto_benchmark.is_file() else None
        )
        benchmark = (
            _load_json_object(benchmark_path, "benchmark")
            if benchmark_path
            else None
        )

        if args.static:
            static_path = args.static.resolve()
            html_page = generate_html(
                runs,
                skill_name,
                previous,
                benchmark,
                load_feedback(feedback_path),
                asset_prefix="",
                iteration=iteration,
            )
            write_static_review(static_path, html_page, force=args.force)
            print(f"[OK] Static review written to: {static_path}")
            return 0

        handler = partial(
            ReviewHandler,
            workspace,
            skill_name,
            feedback_path,
            previous,
            benchmark_path,
            results_path,
            evals_path,
            iteration,
        )
        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
        except OSError:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            print(
                f"[INFO] Port {args.port} was unavailable; selected a free loopback port."
            )
        server.daemon_threads = True
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}"
        print("Codex Skill Evaluation")
        print(f"URL: {url}")
        print(f"Workspace: {workspace}")
        print(f"Feedback: {feedback_path}")
        if results_path:
            print(f"Results: {results_path}")
        if benchmark_path:
            print(f"Benchmark: {benchmark_path}")
        print("Press Ctrl+C to stop.")
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            server.server_close()
        return 0
    except (ReviewError, OSError, UnicodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
