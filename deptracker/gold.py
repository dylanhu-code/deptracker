"""Shared helpers for mixed-level gold-set JSONL rows."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

PR_DIMENSIONS = ("source", "outcome", "security")
CHANGE_DIMENSIONS = ("semver_tier", "direct_or_transitive")
DIMENSIONS = PR_DIMENSIONS + CHANGE_DIMENSIONS

ALLOWED_PR_LABELS = {
    "source": {"dependabot", "renovate", "other-bot", "human", "unknown"},
    "outcome": {"merged", "closed-unmerged", "open", "superseded", "unknown"},
    "security": {"security", "non-security", "unknown"},
}
ALLOWED_CHANGE_LABELS = {
    "semver_tier": {
        "major",
        "minor",
        "patch",
        "prerelease",
        "calver",
        "sha-pin",
        "unknown",
    },
    "direct_or_transitive": {"direct", "transitive", "unknown"},
}
ALLOWED_LABELS = ALLOWED_PR_LABELS | ALLOWED_CHANGE_LABELS
ALLOWED_LABEL_STATUS = {"human_verified", "skipped"}
ALLOWED_LABEL_SOURCE = {"manual"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_CHANGE_LABEL_SCOPE = {"all_changes", "sampled_changes", "none"}
REQUIRED_FIELDS = {
    "pr_id",
    "pr_url",
    "repo",
    "pr_number",
    "change_ids",
    "ecosystem_summary",
    "group_size",
    "pr_labels",
    "change_labels",
    "unlabelled_change_ids",
    "change_label_scope",
    "heuristic_pr_labels",
    "heuristic_change_labels",
    "label_status",
    "label_source",
    "reviewed_by_human",
    "confidence",
    "reasoning",
    "uncertainty_notes",
    "notes",
    "labelled_at",
}


class GoldValidationError(ValueError):
    """Raised when a gold-set row is malformed."""


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dictionaries."""
    file_path = Path(path)
    if not file_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldValidationError(f"{file_path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise GoldValidationError(f"{file_path}:{line_number}: row must be a JSON object")
        rows.append(data)
    return rows


def append_jsonl_row(path: str | Path, row: dict[str, Any]) -> None:
    """Append one validated row to a JSONL file."""
    validate_gold_row(row)
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        write_jsonl_row(handle, row)


def write_jsonl_row(handle: TextIO, row: dict[str, Any]) -> None:
    """Write one validated row to an open JSONL stream."""
    validate_gold_row(row)
    handle.write(json.dumps(row, sort_keys=True) + "\n")
    handle.flush()


def labelled_pr_ids(path: str | Path) -> set[int]:
    """Return the PR ids present in a gold-set file."""
    return {int(row["pr_id"]) for row in read_jsonl(path) if "pr_id" in row}


def human_verified_pr_ids(path: str | Path) -> set[int]:
    """Return PR ids whose gold-set status is human verified."""
    return {
        int(row["pr_id"])
        for row in read_jsonl(path)
        if row.get("label_status") == "human_verified"
    }


def validate_gold_row(row: dict[str, Any]) -> None:
    """Validate that a gold-set row has the required mixed-level shape."""
    missing = sorted(REQUIRED_FIELDS - set(row))
    if missing:
        raise GoldValidationError(f"missing required fields: {', '.join(missing)}")
    _validate_int(row["pr_id"], "pr_id")
    _validate_int(row["pr_number"], "pr_number")
    _validate_int(row["group_size"], "group_size")
    if not isinstance(row["pr_url"], str) or not row["pr_url"]:
        raise GoldValidationError("pr_url must be a non-empty string")
    if not isinstance(row["repo"], str) or "/" not in row["repo"]:
        raise GoldValidationError("repo must be an owner/repo string")
    if not isinstance(row["change_ids"], list) or not row["change_ids"]:
        raise GoldValidationError("change_ids must be a non-empty list")
    for change_id in row["change_ids"]:
        _validate_int(change_id, "change_ids")
    if len(set(row["change_ids"])) != len(row["change_ids"]):
        raise GoldValidationError("change_ids must not contain duplicates")
    if not isinstance(row["ecosystem_summary"], list) or not row["ecosystem_summary"]:
        raise GoldValidationError("ecosystem_summary must be a non-empty list")
    if any(not isinstance(ecosystem, str) or not ecosystem for ecosystem in row["ecosystem_summary"]):
        raise GoldValidationError("ecosystem_summary values must be non-empty strings")
    validate_pr_labels(row["pr_labels"], field="pr_labels")
    validate_pr_labels(row["heuristic_pr_labels"], field="heuristic_pr_labels")
    validate_change_label_partition(row)
    validate_change_labels(
        row["change_labels"],
        row["change_ids"],
        require_metadata=True,
        allow_subset=True,
    )
    validate_change_labels(
        row["heuristic_change_labels"],
        row["change_ids"],
        require_metadata=False,
        field="heuristic_change_labels",
    )
    if row["change_label_scope"] not in ALLOWED_CHANGE_LABEL_SCOPE:
        raise GoldValidationError(f"invalid change_label_scope: {row['change_label_scope']}")
    if row["label_status"] not in ALLOWED_LABEL_STATUS:
        raise GoldValidationError(f"invalid label_status: {row['label_status']}")
    if row["label_source"] not in ALLOWED_LABEL_SOURCE:
        raise GoldValidationError(f"invalid label_source: {row['label_source']}")
    if not isinstance(row["reviewed_by_human"], bool):
        raise GoldValidationError("reviewed_by_human must be a boolean")
    if row["confidence"] is not None and row["confidence"] not in ALLOWED_CONFIDENCE:
        raise GoldValidationError(f"invalid confidence: {row['confidence']}")
    for text_field in ("reasoning", "uncertainty_notes", "notes", "labelled_at"):
        if not isinstance(row[text_field], str):
            raise GoldValidationError(f"{text_field} must be a string")


def validate_pr_labels(labels: Any, field: str = "pr_labels") -> None:
    """Validate PR-level label objects."""
    if not isinstance(labels, dict):
        raise GoldValidationError(f"{field} must be an object")
    missing = sorted(set(PR_DIMENSIONS) - set(labels))
    if missing:
        raise GoldValidationError(f"{field} missing dimensions: {', '.join(missing)}")
    for dimension in PR_DIMENSIONS:
        if labels[dimension] not in ALLOWED_PR_LABELS[dimension]:
            raise GoldValidationError(f"invalid {field}.{dimension}: {labels[dimension]}")


def validate_change_label_values(labels: Any, field: str = "change_labels") -> None:
    """Validate change-level label objects without row metadata."""
    if not isinstance(labels, dict):
        raise GoldValidationError(f"{field} must be an object")
    missing = sorted(set(CHANGE_DIMENSIONS) - set(labels))
    if missing:
        raise GoldValidationError(f"{field} missing dimensions: {', '.join(missing)}")
    for dimension in CHANGE_DIMENSIONS:
        if labels[dimension] not in ALLOWED_CHANGE_LABELS[dimension]:
            raise GoldValidationError(f"invalid {field}.{dimension}: {labels[dimension]}")


def validate_change_labels(
    rows: Any,
    expected_change_ids: list[int],
    *,
    require_metadata: bool,
    allow_subset: bool = False,
    field: str = "change_labels",
) -> None:
    """Validate a list of change labels against the expected change ids."""
    if not isinstance(rows, list):
        raise GoldValidationError(f"{field} must be a list")
    seen: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            raise GoldValidationError(f"{field} entries must be objects")
        _validate_int(row.get("change_id"), f"{field}.change_id")
        seen.append(row["change_id"])
        validate_change_label_values(row, field=field)
        if require_metadata:
            for text_field in ("package", "to_version", "manifest_path"):
                if not isinstance(row.get(text_field), str):
                    raise GoldValidationError(f"{field}.{text_field} must be a string")
            if row.get("from_version") is not None and not isinstance(row.get("from_version"), str):
                raise GoldValidationError(f"{field}.from_version must be null or a string")
    expected = sorted(expected_change_ids)
    actual = sorted(seen)
    if len(actual) != len(set(actual)):
        raise GoldValidationError(f"{field} must not contain duplicate change_ids")
    if allow_subset:
        extra = sorted(set(actual) - set(expected))
        if extra:
            raise GoldValidationError(f"{field} contains change_ids not in packet: {extra}")
        return
    if actual != expected:
        raise GoldValidationError(
            f"{field} change_ids must match packet change_ids exactly; expected {expected}, got {actual}"
        )


def validate_change_label_partition(row: dict[str, Any]) -> None:
    """Validate that labelled and unlabelled change ids partition the packet."""
    expected = set(row["change_ids"])
    labelled = {change_row["change_id"] for change_row in row["change_labels"]}
    unlabelled = row["unlabelled_change_ids"]
    if not isinstance(unlabelled, list):
        raise GoldValidationError("unlabelled_change_ids must be a list")
    for change_id in unlabelled:
        _validate_int(change_id, "unlabelled_change_ids")
    unlabelled_set = set(unlabelled)
    if len(unlabelled) != len(unlabelled_set):
        raise GoldValidationError("unlabelled_change_ids must not contain duplicates")
    if labelled - expected:
        raise GoldValidationError("change_labels contains ids outside change_ids")
    if unlabelled_set - expected:
        raise GoldValidationError("unlabelled_change_ids contains ids outside change_ids")
    if labelled & unlabelled_set:
        raise GoldValidationError("change_ids cannot be both labelled and unlabelled")
    if labelled | unlabelled_set != expected:
        raise GoldValidationError("labelled and unlabelled change IDs must partition change_ids")
    scope = row["change_label_scope"]
    if scope == "all_changes" and unlabelled_set:
        raise GoldValidationError("all_changes scope requires no unlabelled_change_ids")
    if scope == "none" and labelled:
        raise GoldValidationError("none scope requires empty change_labels")


def make_gold_row(
    *,
    pr_id: int,
    pr_url: str,
    repo: str,
    pr_number: int,
    change_ids: list[int],
    ecosystem_summary: list[str],
    group_size: int,
    pr_labels: dict[str, str],
    change_labels: list[dict[str, Any]],
    heuristic_pr_labels: dict[str, str],
    heuristic_change_labels: list[dict[str, Any]],
    label_status: str,
    label_source: str,
    reviewed_by_human: bool,
    confidence: str | None,
    unlabelled_change_ids: list[int] | None = None,
    change_label_scope: str = "all_changes",
    reasoning: str = "",
    uncertainty_notes: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Construct and validate one gold-set row."""
    row = {
        "pr_id": pr_id,
        "pr_url": pr_url,
        "repo": repo,
        "pr_number": pr_number,
        "change_ids": change_ids,
        "ecosystem_summary": ecosystem_summary,
        "group_size": group_size,
        "pr_labels": pr_labels,
        "change_labels": change_labels,
        "unlabelled_change_ids": unlabelled_change_ids
        if unlabelled_change_ids is not None
        else sorted(set(change_ids) - {row["change_id"] for row in change_labels}),
        "change_label_scope": change_label_scope,
        "heuristic_pr_labels": heuristic_pr_labels,
        "heuristic_change_labels": heuristic_change_labels,
        "label_status": label_status,
        "label_source": label_source,
        "reviewed_by_human": reviewed_by_human,
        "confidence": confidence,
        "reasoning": reasoning,
        "uncertainty_notes": uncertainty_notes,
        "notes": notes,
        "labelled_at": datetime.now(UTC).isoformat(),
    }
    validate_gold_row(row)
    return row




def _validate_int(value: Any, field: str) -> None:
    """Reject non-integer values, including booleans."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise GoldValidationError(f"{field} must be an integer")


def summarize_gold_set(gold_file: str | Path = "data/gold_set.jsonl") -> dict[str, Any]:
    """Summarize the contents of a gold set file."""
    rows = read_jsonl(gold_file)
    status_counts = Counter(row.get("label_status", "unknown") for row in rows)
    source_counts = Counter(row.get("label_source", "unknown") for row in rows)

    return {
        "total_rows": len(rows),
        "status_counts": dict(status_counts),
        "source_counts": dict(source_counts),
    }
