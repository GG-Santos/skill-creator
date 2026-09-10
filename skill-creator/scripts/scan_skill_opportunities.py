#!/usr/bin/env python3
"""Find recurring skill opportunities in explicitly supplied local files."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import re
import sys
from typing import Any

import catalog_skills


MAX_INPUT_BYTES = 20 * 1024 * 1024
TEXT_KEYS = (
    "correction", "request", "prompt", "query", "task", "text", "message",
    "content", "error", "summary",
)
CONTAINER_KEYS = ("events", "items", "requests", "messages", "turns", "records")
TOKEN = re.compile(r"[a-z0-9]+")
CORRECTION = re.compile(
    r"\b(actually|instead|wrong|incorrect|redo|fix|forgot|missed|should have|do not|don't)\b",
    re.IGNORECASE,
)
SEQUENCE = re.compile(
    r"\b(then|after|before|handoff|pipeline|combine|across|followed by|next step)\b",
    re.IGNORECASE,
)
STOP_WORDS = catalog_skills.STOP_WORDS | {
    "again", "can", "could", "do", "have", "help", "make", "my", "need",
    "please", "should", "want", "would",
}
REDACTIONS = (
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE), "<email>"),
    (re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{16,}\b"), "<secret>"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*\b", re.IGNORECASE), "Bearer <secret>"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "<long-token>"),
    (re.compile(r"(?i)\bC:\\Users\\[^\\\s]+"), "C:/Users/<user>"),
    (re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), "<ip-address>"),
)


class OpportunityError(Exception):
    pass


def redact(text: str) -> str:
    value = text
    for pattern, replacement in REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def _extract_text(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if not isinstance(item, dict):
        return None
    for key in TEXT_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _json_entries(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in CONTAINER_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    return []


def read_signals(path: Path, source_label: str | None = None) -> list[dict[str, Any]]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise OpportunityError(f"Input file not found: {path}") from exc
    if not resolved.is_file():
        raise OpportunityError(f"Input must be a file: {resolved}")
    if resolved.stat().st_size > MAX_INPUT_BYTES:
        raise OpportunityError(f"Input exceeds {MAX_INPUT_BYTES} bytes: {resolved}")
    raw = resolved.read_text(encoding="utf-8-sig")
    entries: list[Any]
    if resolved.suffix.lower() == ".json":
        try:
            entries = _json_entries(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise OpportunityError(f"Invalid JSON in {resolved}: {exc}") from exc
    elif resolved.suffix.lower() == ".jsonl":
        entries = []
        for line_number, line in enumerate(raw.split("\n"), start=1):
            line = line.removesuffix("\r")
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise OpportunityError(
                    f"Invalid JSONL in {resolved} line {line_number}: {exc.msg}"
                ) from exc
    else:
        paragraphs = re.split(r"(?m)(?:\n\s*\n|^\s*[-*]\s+)", raw)
        entries = [part.strip() for part in paragraphs if part.strip()]

    signals: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        text = _extract_text(entry)
        if not text:
            continue
        safe_text = redact(text)
        tokens = {
            word for word in TOKEN.findall(safe_text.lower())
            if word not in STOP_WORDS and len(word) > 1
        }
        if len(tokens) < 2:
            continue
        signals.append(
            {
                "source": source_label or "source-1",
                "entry": index,
                "text": safe_text,
                "tokens": tokens,
                "correction": bool(CORRECTION.search(safe_text)),
                "sequence": bool(SEQUENCE.search(safe_text)),
                "fingerprint": hashlib.sha256(safe_text.encode("utf-8")).hexdigest()[:12],
            }
        )
    return signals


def _similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _cluster(signals: list[dict[str, Any]], threshold: float) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    token_unions: list[set[str]] = []
    for signal in signals:
        best_index = None
        best_score = 0.0
        for index, tokens in enumerate(token_unions):
            score = _similarity(signal["tokens"], tokens)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is None or best_score < threshold:
            clusters.append([signal])
            token_unions.append(set(signal["tokens"]))
        else:
            clusters[best_index].append(signal)
            token_unions[best_index].update(signal["tokens"])
    return clusters


def _candidate_title(cluster: list[dict[str, Any]]) -> str:
    counts = Counter(word for item in cluster for word in item["tokens"])
    words = [word for word, _ in counts.most_common(5)]
    return " ".join(words).title() or "Recurring Workflow"


def _catalog_match(
    cluster: list[dict[str, Any]], catalog: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, float]:
    if not catalog:
        return None, 0.0
    cluster_tokens = set().union(*(item["tokens"] for item in cluster))
    best = None
    best_score = 0.0
    for skill in catalog["skills"]:
        skill_tokens = catalog_skills._tokens(skill["name"] + " " + skill["description"])
        score = _similarity(cluster_tokens, skill_tokens)
        if score > best_score:
            best = skill
            best_score = score
    return best, best_score


def find_opportunities(
    inputs: list[Path],
    skill_roots: list[Path] | None = None,
    min_recurrence: int = 2,
    similarity: float = 0.34,
    include_snippets: bool = False,
    include_source_paths: bool = False,
) -> dict[str, Any]:
    if min_recurrence < 2:
        raise OpportunityError("Minimum recurrence must be at least 2.")
    if not 0 <= similarity <= 1:
        raise OpportunityError("Similarity must be between 0 and 1.")
    resolved_inputs = [source.resolve(strict=True) for source in inputs]
    source_labels = [
        str(source) if include_source_paths else f"source-{index}"
        for index, source in enumerate(resolved_inputs, start=1)
    ]
    signals = [
        signal
        for source, source_label in zip(resolved_inputs, source_labels)
        for signal in read_signals(source, source_label)
    ]
    catalog = catalog_skills.build_catalog(skill_roots) if skill_roots else None
    candidates = []
    for cluster in _cluster(signals, similarity):
        if len(cluster) < min_recurrence:
            continue
        match, match_score = _catalog_match(cluster, catalog)
        sequence_count = sum(1 for item in cluster if item["sequence"])
        correction_count = sum(1 for item in cluster if item["correction"])
        if match is not None and match_score >= 0.28:
            action = "augment"
            target = {"name": match["name"]}
            if include_source_paths:
                target["path"] = match["path"]
        elif sequence_count >= max(2, len(cluster) // 2):
            action = "connect-or-create-orchestrator"
            target = None
        else:
            action = "create"
            target = None
        evidence = [
            {
                "source": item["source"],
                "entry": item["entry"],
                "fingerprint": item["fingerprint"],
                **({"snippet": item["text"][:240]} if include_snippets else {}),
            }
            for item in cluster
        ]
        confidence = (
            "high" if len(cluster) >= 5 and correction_count >= 2
            else "medium" if len(cluster) >= 3
            else "low"
        )
        candidates.append(
            {
                "id": "opportunity-" + hashlib.sha256(
                    "|".join(sorted(item["fingerprint"] for item in cluster)).encode("utf-8")
                ).hexdigest()[:10],
                "title": _candidate_title(cluster),
                "recommended_action": action,
                "target_skill": target,
                "do_nothing_alternative": "Keep handling these requests ad hoc and monitor recurrence.",
                "recurrence": len(cluster),
                "correction_signals": correction_count,
                "sequence_signals": sequence_count,
                "catalog_match_score": round(match_score, 4),
                "confidence": confidence,
                "evidence": evidence,
                "next_step": "Review source evidence and define trigger boundaries before implementation.",
            }
        )
    candidates.sort(
        key=lambda item: (
            -item["correction_signals"],
            -item["recurrence"],
            item["title"],
        )
    )
    return {
        "schema_version": 1,
        "sources": source_labels,
        "source_count": len(inputs),
        "signal_count": len(signals),
        "candidate_count": len(candidates),
        "redaction": "content-always-on; local-paths-opt-in",
        "source_paths_included": include_source_paths,
        "local_paths_included": include_source_paths,
        "snippets_included": include_snippets,
        "candidates": candidates,
        "limitations": [
            "Clustering is lexical and may split equivalent requests or group unrelated requests.",
            "Recommendations are review leads and do not authorize creating or modifying a skill.",
            "Only explicitly supplied files and skill roots were read.",
        ],
    }


def _cell(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("|", r"\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Skill Opportunity Report",
        "",
        f"- Sources: {report['source_count']}",
        f"- Signals: {report['signal_count']}",
        f"- Candidates: {report['candidate_count']}",
        f"- Redaction: {report['redaction']}",
        "",
        "| Candidate | Action | Recurrence | Corrections | Confidence |",
        "|---|---|---:|---:|---|",
    ]
    for item in report["candidates"]:
        lines.append(
            f"| {_cell(item['title'])} | {_cell(item['recommended_action'])} | "
            f"{item['recurrence']} | {item['correction_signals']} | {_cell(item['confidence'])} |"
        )
    if not report["candidates"]:
        lines.append("| None | Do nothing | 0 | 0 | n/a |")
    for item in report["candidates"]:
        lines.extend(
            [
                "",
                f"## {_cell(item['title'])}",
                "",
                f"- ID: {_cell(item['id'])}",
                f"- Recommended action: {_cell(item['recommended_action'])}",
                f"- Do-nothing alternative: {_cell(item['do_nothing_alternative'])}",
                f"- Next step: {_cell(item['next_step'])}",
            ]
        )
        if item["target_skill"]:
            target_path = item["target_skill"].get("path")
            suffix = f" ({_cell(target_path)})" if target_path else ""
            lines.append(
                f"- Candidate target: {_cell(item['target_skill']['name'])}{suffix}"
            )
        lines.append("- Evidence: " + ", ".join(
            f"{_cell(Path(entry['source']).name)}#{entry['entry']}:{entry['fingerprint']}"
            for entry in item["evidence"]
        ))
        if report["snippets_included"]:
            for entry in item["evidence"]:
                lines.append(f"  - {_cell(entry.get('snippet', ''))}")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {_cell(item)}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan explicitly supplied local files for recurring skill opportunities."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="Text, Markdown, JSON, or JSONL files")
    parser.add_argument(
        "--skill-root",
        action="append",
        type=Path,
        default=[],
        help="Explicit skill root used to distinguish creation from augmentation",
    )
    parser.add_argument("--min-recurrence", type=int, default=2)
    parser.add_argument("--similarity", type=float, default=0.34)
    parser.add_argument(
        "--include-snippets",
        action="store_true",
        help="Include redacted excerpts; omitted by default",
    )
    parser.add_argument(
        "--include-source-paths",
        "--include-local-paths",
        dest="include_source_paths",
        action="store_true",
        help="Include absolute input and catalog paths; opaque labels are used by default",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = find_opportunities(
            args.inputs,
            args.skill_root,
            args.min_recurrence,
            args.similarity,
            args.include_snippets,
            args.include_source_paths,
        )
        output = (
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
            if args.format == "json"
            else render_markdown(report)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    except (OpportunityError, catalog_skills.CatalogError, OSError, UnicodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
