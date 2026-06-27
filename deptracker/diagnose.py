"""Offline diagnostics derived from the persisted SQLite database."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from deptracker.adapters.base import ADAPTERS
from deptracker.classify import CLASSIFIER_VERSION
from deptracker.store import Store

FILTER_REASONS = (
    "no_manifest_files",
    "mixed_manifest_and_non_manifest_files",
    "too_many_files",
    "too_many_files_cap",
    "graphql_not_found",
    "graphql_error",
    "other/unknown",
)

FILE_BINS = ("1", "2", "3", "4", "5", "6-10", "11-25", "26-50", "51-100", ">100")
GROUP_SIZE_BINS = ("1", "2", "3", "4", "5", "6-10", "11-25", "26-50", ">50")
PR_LEVEL_DIMENSIONS = ("source", "outcome", "security")
GRADLE_FILENAMES = {"build.gradle", "build.gradle.kts"}


def diagnose(store: Store, output_path: str | Path = "data/diagnose.json") -> dict[str, Any]:
    """Generate the full offline diagnostics JSON report."""
    store.init_schema()
    with store._connect() as conn:
        pr_rows = conn.execute("SELECT * FROM pr WHERE enriched_at IS NOT NULL").fetchall()
        file_rows = conn.execute("SELECT * FROM pr WHERE files_json IS NOT NULL").fetchall()
        change_rows = conn.execute(
            """
            SELECT change.pr_id, change.ecosystem
            FROM change
            GROUP BY change.pr_id, change.ecosystem
            """
        ).fetchall()
        error_rows = conn.execute(
            """
            SELECT COALESCE(error_type, 'unknown') AS error_type, COUNT(*) AS count
            FROM pipeline_error
            WHERE stage = 'enrich'
            GROUP BY COALESCE(error_type, 'unknown')
            ORDER BY error_type
            """
        ).fetchall()

    changes_by_pr: dict[int, set[str]] = defaultdict(set)
    parse_success = Counter()
    for row in change_rows:
        changes_by_pr[row["pr_id"]].add(row["ecosystem"])
        parse_success[row["ecosystem"]] += 1

    candidate_counts = Counter()
    filter_pass = Counter()
    filter_fail: dict[str, Counter] = defaultdict(Counter)
    zero_changes = Counter()
    zero_change_cases: list[dict[str, Any]] = []
    file_bins = Counter({name: 0 for name in FILE_BINS})
    zero_file_prs = 0
    pr_actions = Counter()

    for row in pr_rows:
        action = row["pr_action"] or "unknown"
        pr_actions[action] += 1

    for row in file_rows:
        files = _load_files(row["files_json"])
        ecosystems = _ecosystems_for_files(files)
        for ecosystem in ecosystems:
            candidate_counts[ecosystem] += 1

        file_bin = _file_bin(row["file_count"], len(files), row["files_has_next_page"])
        if file_bin is None:
            zero_file_prs += 1
        else:
            file_bins[file_bin] += 1

        if row["passes_manifest_only_filter"] == 1:
            for ecosystem in ecosystems:
                filter_pass[ecosystem] += 1
            if row["id"] not in changes_by_pr:
                manifest_paths = [file_info["path"] for file_info in files if _ecosystem_for_path(file_info["path"])]
                case_ecosystems = ecosystems or {"unmatched"}
                for ecosystem in case_ecosystems:
                    zero_changes[ecosystem] += 1
                zero_change_cases.append(
                    {
                        "pr_id": row["id"],
                        "ecosystems": sorted(case_ecosystems),
                        "manifest_paths": manifest_paths,
                        "all_paths_known_stubs": _all_known_stubs(manifest_paths),
                    }
                )
        elif row["passes_manifest_only_filter"] == 0:
            reason = _normalise_reason(row["manifest_filter_reason"])
            for ecosystem in ecosystems:
                filter_fail[ecosystem][reason] += 1

    for row in pr_rows:
        if row["files_json"] is None and row["passes_manifest_only_filter"] == 0:
            reason = _normalise_reason(row["manifest_filter_reason"])
            filter_fail["unmatched"][reason] += 1

    has_next_denominator = sum(1 for row in pr_rows if row["files_has_next_page"] is not None)
    has_next_count = sum(1 for row in pr_rows if row["files_has_next_page"] == 1)

    output = {
        "candidate_count_per_ecosystem": dict(sorted(candidate_counts.items())),
        "filter_pass_count_per_ecosystem": dict(sorted(filter_pass.items())),
        "filter_fail_count_per_ecosystem": {
            ecosystem: {reason: counts.get(reason, 0) for reason in FILTER_REASONS}
            for ecosystem, counts in sorted(filter_fail.items())
        },
        "parse_success_count_per_ecosystem": dict(sorted(parse_success.items())),
        "pass_filter_but_zero_changes_count_per_ecosystem": dict(sorted(zero_changes.items())),
        "pass_filter_but_zero_changes_cases": zero_change_cases,
        "files_per_pr_distribution": {name: file_bins[name] for name in FILE_BINS},
        "zero_file_prs": zero_file_prs,
        "too_many_files_rejections": {
            "count": has_next_count,
            "denominator": has_next_denominator,
            "fraction": has_next_count / has_next_denominator if has_next_denominator else 0,
        },
        "graphql_error_summary": {row["error_type"]: row["count"] for row in error_rows},
        "pr_action_distribution": dict(sorted(pr_actions.items())),
        "synchronize_duplicate_fraction": {
            "recoverable": False,
            "reason": (
                "Discovery stores one deduplicated pr row via INSERT OR IGNORE; duplicate "
                "events are not retained."
            ),
            "needed_for_future": "A pr_event table storing every discovered event before deduplication.",
        },
    }
    output.update(_classified_diagnostics(store))

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    return output


def _classified_diagnostics(store: Store) -> dict[str, Any]:
    """Compute diagnostics over classified change rows."""
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT
              pr.id AS pr_id,
              change.id AS change_id,
              change.ecosystem AS ecosystem,
              change.group_size AS group_size,
              classification.dimension AS dimension,
              classification.label AS label
            FROM change
            JOIN pr ON pr.id = change.pr_id
            JOIN classification ON classification.change_id = change.id
            WHERE classification.classifier_version = ?
            """,
            (CLASSIFIER_VERSION,),
        ).fetchall()
        triple_counts = _triple_counts(conn)

    pr_changes: dict[int, set[int]] = defaultdict(set)
    pr_ecosystems: dict[int, set[str]] = defaultdict(set)
    change_labels: dict[int, dict[str, str]] = defaultdict(dict)
    change_ecosystem: dict[int, str] = {}
    change_group_size: dict[int, int | None] = {}
    for row in rows:
        pr_id = row["pr_id"]
        change_id = row["change_id"]
        pr_changes[pr_id].add(change_id)
        pr_ecosystems[pr_id].add(row["ecosystem"])
        change_ecosystem[change_id] = row["ecosystem"]
        change_group_size[change_id] = row["group_size"]
        change_labels[change_id][row["dimension"]] = row["label"]

    pr_weighted = {dimension: Counter() for dimension in PR_LEVEL_DIMENSIONS}
    pr_weighted_by_ecosystem = {
        dimension: defaultdict(Counter) for dimension in PR_LEVEL_DIMENSIONS
    }
    change_weighted = {dimension: Counter() for dimension in PR_LEVEL_DIMENSIONS}
    consistency_violations = Counter()

    for change_id, labels in change_labels.items():
        for dimension in PR_LEVEL_DIMENSIONS:
            if dimension in labels:
                change_weighted[dimension][labels[dimension]] += 1

    for pr_id, change_ids in pr_changes.items():
        for dimension in PR_LEVEL_DIMENSIONS:
            labels = [
                change_labels[change_id][dimension]
                for change_id in change_ids
                if dimension in change_labels[change_id]
            ]
            if not labels:
                continue
            if len(set(labels)) > 1:
                consistency_violations[dimension] += 1
            label = _most_common_label(Counter(labels))
            pr_weighted[dimension][label] += 1
            for ecosystem in pr_ecosystems[pr_id]:
                pr_weighted_by_ecosystem[dimension][ecosystem][label] += 1

    group_sizes = Counter({name: 0 for name in GROUP_SIZE_BINS})
    group_sizes["unknown"] = 0
    for change_id in change_labels:
        group_sizes[_group_size_bin(change_group_size.get(change_id))] += 1
    if group_sizes["unknown"] == 0:
        del group_sizes["unknown"]

    return {
        "pr_level_distributions": {
            dimension: {
                "overall": _distribution(pr_weighted[dimension]),
                "per_ecosystem": {
                    ecosystem: _distribution(counts)
                    for ecosystem, counts in sorted(pr_weighted_by_ecosystem[dimension].items())
                },
            }
            for dimension in PR_LEVEL_DIMENSIONS
        },
        "pr_level_consistency_violations": dict(sorted(consistency_violations.items())),
        "cross_ecosystem_classified_pr_count": sum(
            1 for ecosystems in pr_ecosystems.values() if len(ecosystems) > 1
        ),
        "classified_change_group_size_distribution": dict(group_sizes),
        "pr_vs_change_weighted_pr_level_dimensions": {
            dimension: _comparison(pr_weighted[dimension], change_weighted[dimension])
            for dimension in PR_LEVEL_DIMENSIONS
        },
        "sq2_repeated_triple_counts": triple_counts,
    }


def _triple_counts(conn: Any) -> dict[str, Any]:
    """Count repeated version triples across projects and ecosystems."""
    overall = conn.execute(
        """
        WITH triple_projects AS (
          SELECT
            change.ecosystem,
            change.package,
            change.from_version,
            change.to_version,
            COUNT(DISTINCT pr.repo_owner || '/' || pr.repo_name) AS project_count,
            SUM(CASE WHEN COALESCE(change.group_size, 0) = 1 THEN 0 ELSE 1 END)
              AS non_singleton_rows
          FROM change
          JOIN pr ON pr.id = change.pr_id
          GROUP BY change.ecosystem, change.package, change.from_version, change.to_version
        )
        SELECT
          COUNT(*) AS repeated_triples,
          COALESCE(
            SUM(CASE WHEN non_singleton_rows = 0 THEN 1 ELSE 0 END),
            0
          ) AS singleton_restricted_repeated_triples
        FROM triple_projects
        WHERE project_count >= 5
        """
    ).fetchone()
    per_ecosystem_rows = conn.execute(
        """
        WITH triple_projects AS (
          SELECT
            change.ecosystem,
            change.package,
            change.from_version,
            change.to_version,
            COUNT(DISTINCT pr.repo_owner || '/' || pr.repo_name) AS project_count,
            SUM(CASE WHEN COALESCE(change.group_size, 0) = 1 THEN 0 ELSE 1 END)
              AS non_singleton_rows
          FROM change
          JOIN pr ON pr.id = change.pr_id
          GROUP BY change.ecosystem, change.package, change.from_version, change.to_version
        )
        SELECT
          ecosystem,
          COUNT(*) AS repeated_triples,
          COALESCE(
            SUM(CASE WHEN non_singleton_rows = 0 THEN 1 ELSE 0 END),
            0
          ) AS singleton_restricted_repeated_triples
        FROM triple_projects
        WHERE project_count >= 5
        GROUP BY ecosystem
        ORDER BY ecosystem
        """
    ).fetchall()
    return {
        "overall": {
            "triples_in_at_least_5_projects": overall["repeated_triples"],
            "triples_in_at_least_5_projects_all_group_size_1": overall[
                "singleton_restricted_repeated_triples"
            ],
        },
        "per_ecosystem": {
            row["ecosystem"]: {
                "triples_in_at_least_5_projects": row["repeated_triples"],
                "triples_in_at_least_5_projects_all_group_size_1": row[
                    "singleton_restricted_repeated_triples"
                ],
            }
            for row in per_ecosystem_rows
        },
    }


def _distribution(counts: Counter[str]) -> dict[str, dict[str, float | int]]:
    """Format a label counter as counts and percentages."""
    total = sum(counts.values())
    return {
        label: {
            "count": count,
            "percentage": round((count / total) * 100, 1) if total else 0.0,
        }
        for label, count in sorted(counts.items())
    }


def _comparison(
    pr_weighted: Counter[str],
    change_weighted: Counter[str],
) -> dict[str, dict[str, float | int]]:
    """Compare PR-weighted and change-weighted label distributions."""
    labels = sorted(set(pr_weighted) | set(change_weighted))
    pr_total = sum(pr_weighted.values())
    change_total = sum(change_weighted.values())
    return {
        label: {
            "pr_weighted_count": pr_weighted[label],
            "pr_weighted_percentage": round((pr_weighted[label] / pr_total) * 100, 1)
            if pr_total
            else 0.0,
            "change_weighted_count": change_weighted[label],
            "change_weighted_percentage": round(
                (change_weighted[label] / change_total) * 100, 1
            )
            if change_total
            else 0.0,
        }
        for label in labels
    }


def _group_size_bin(group_size: int | None) -> str:
    """Bucket a numeric group size into a display bin."""
    if group_size is None or group_size < 1:
        return "unknown"
    if group_size <= 5:
        return str(group_size)
    if group_size <= 10:
        return "6-10"
    if group_size <= 25:
        return "11-25"
    if group_size <= 50:
        return "26-50"
    return ">50"


def _most_common_label(counts: Counter[str]) -> str:
    """Return the modal label, breaking ties deterministically."""
    if not counts:
        return "unknown"
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _load_files(files_json: str | None) -> list[dict[str, Any]]:
    """Deserialize a files JSON payload into a list of objects."""
    if not files_json:
        return []
    data = json.loads(files_json)
    return data if isinstance(data, list) else []


def _ecosystems_for_files(files: list[dict[str, Any]]) -> set[str]:
    """Infer ecosystems represented by a set of file paths."""
    ecosystems = {
        ecosystem
        for file_info in files
        if (ecosystem := _ecosystem_for_path(file_info.get("path", "")))
    }
    return ecosystems or {"unmatched"}


def _ecosystem_for_path(path: str) -> str | None:
    """Resolve the adapter ecosystem for a manifest path."""
    for adapter_cls in ADAPTERS.values():
        adapter = adapter_cls()
        if adapter.is_manifest_path(path):
            return adapter.name
    return None


def _all_known_stubs(paths: list[str]) -> bool:
    """Return True when every path looks like a known stub or lockfile."""
    if not paths:
        return False
    return all(_is_known_stub(path) for path in paths)


def _is_known_stub(path: str) -> bool:
    """Detect lockfiles and other known stub-like dependency files."""
    name = Path(path).name
    if name in GRADLE_FILENAMES:
        return True
    for adapter_cls in ADAPTERS.values():
        if adapter_cls().is_lockfile_path(path):
            return True
    return False


def _normalise_reason(reason: str | None) -> str:
    """Normalize filter failure reasons into the known summary buckets."""
    if reason in FILTER_REASONS:
        return reason
    if reason == "mixed_manifest_and_non_manifest_files":
        return reason
    if reason == "too_many_files":
        return reason
    if reason == "too_many_files_cap":
        return reason
    if reason == "no_manifest_files":
        return reason
    if reason == "graphql_not_found":
        return reason
    if reason == "graphql_error":
        return reason
    return "other/unknown"


def _file_bin(file_count: int | None, fallback_count: int, has_next_page: int | None) -> str | None:
    """Bucket file counts for diagnostics output."""
    if has_next_page == 1:
        return ">100"
    count = file_count if file_count is not None else fallback_count
    if count < 1:
        return None
    if count <= 5:
        return str(count)
    if count <= 10:
        return "6-10"
    if count <= 25:
        return "11-25"
    if count <= 50:
        return "26-50"
    return "51-100"
