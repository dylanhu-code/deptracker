"""Compute shared-version alignment for SQ2.

The alignment dataset has one row per shared upstream version triple:
``(ecosystem, package, from_version, to_version)``. Project-level decisions
come from the PR-level outcome classifier. Open PRs are treated as non-decisions
and excluded from the alignment score and SQ2 thresholds. Superseded PRs are
also treated as non-decisions because they represent replacement rather than an
accept/reject decision on the version pair. If a single project produces
multiple PRs for the same upstream version pair, we report the latest
non-excluded outcome, where the primary decision categories are exactly
``merged`` and ``closed-unmerged``.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from statistics import fmean, median
from typing import Any

from deptracker.classify import CLASSIFIER_VERSION
from deptracker.store import Store

OUTCOME_LABELS = ("merged", "closed-unmerged", "open", "superseded")
DECISION_LABELS = ("merged", "closed-unmerged")
EXCLUDED_LABELS = ("open", "superseded")
SOURCE_LABELS = ("dependabot", "renovate", "human", "other-bot")
LOGGER = logging.getLogger(__name__)

TripleKey = tuple[str, str, str | None, str]


def compute_alignment(store: Store) -> dict:
    """Compute SQ2 alignment metrics from classified change rows."""
    started_at = time.perf_counter()
    fetched_rows = _fetch_contributing_rows(store)
    rows = [
        row
        for row in fetched_rows
        if not bool(row["is_fork"]) and not bool(row["is_archived"])
    ]
    excluded_repository_rows = [
        row
        for row in fetched_rows
        if bool(row["is_fork"]) or bool(row["is_archived"])
    ]
    excluded_by_ecosystem = Counter(row["ecosystem"] for row in excluded_repository_rows)
    grouped = _group_by_triple(rows)
    computed_at = datetime.now(UTC).isoformat()

    alignment_rows = []
    triples_with_open_projects = 0
    triples_with_non_decision_projects = 0
    triples_dropped_zero_decided = 0
    triples_dropped_only_superseded = 0
    triples_below_k2_after_non_decision_exclusion = 0
    excluded_change_rows = 0
    superseded_change_rows_excluded = 0
    for (ecosystem, package, from_version, to_version), triple_rows in grouped.items():
        decided_rows = [row for row in triple_rows if row["outcome"] in DECISION_LABELS]
        excluded_rows = [row for row in triple_rows if row["outcome"] in EXCLUDED_LABELS]
        excluded_change_rows += len(excluded_rows)
        superseded_change_rows_excluded += sum(
            1 for row in excluded_rows if row["outcome"] == "superseded"
        )
        if len(decided_rows) < len(triple_rows):
            triples_with_non_decision_projects += 1
            if any(row["outcome"] == "open" for row in excluded_rows):
                triples_with_open_projects += 1
        if not decided_rows:
            triples_dropped_zero_decided += 1
            outcomes = {row["outcome"] for row in triple_rows}
            if outcomes == {"superseded"}:
                triples_dropped_only_superseded += 1
            continue

        project_decisions = _latest_decision_by_project(decided_rows)
        decision_counts = Counter(project_decisions.values())
        unknown_outcomes = set(decision_counts) - set(DECISION_LABELS)
        if unknown_outcomes:
            unknown = ", ".join(sorted(unknown_outcomes))
            raise ValueError(f"Unsupported outcome label(s) for alignment: {unknown}")

        all_projects = {
            (row["repo_owner"], row["repo_name"])
            for row in triple_rows
        }
        decided_projects = set(project_decisions)
        k_projects_all = len(all_projects)
        k_projects_decided = len(decided_projects)
        if k_projects_decided < 2:
            triples_below_k2_after_non_decision_exclusion += 1
        excluded_decisions = _latest_excluded_by_project(triple_rows, decided_projects)
        excluded_counts = Counter(excluded_decisions.values())
        group_sizes = [int(row["group_size"] or 1) for row in decided_rows]
        semver_tier = _semver_tier_for_triple(
            decided_rows,
            (ecosystem, package, from_version, to_version),
        )
        source_counts = Counter(row["source"] for row in decided_rows)
        security_labels = [row["security"] for row in decided_rows]
        alignment_rows.append(
            {
                "ecosystem": ecosystem,
                "package": package,
                "from_version": from_version,
                "to_version": to_version,
                "semver_tier": semver_tier,
                "k_projects_all": k_projects_all,
                "k_projects_decided": k_projects_decided,
                "k_changes": len(decided_rows),
                "k_changes_total": len(triple_rows),
                "n_merged": decision_counts["merged"],
                "n_closed_unmerged": decision_counts["closed-unmerged"],
                "n_open": excluded_counts["open"],
                "n_superseded": excluded_counts["superseded"],
                "is_security_any": int(any(label == "security" for label in security_labels)),
                "is_security_all": int(
                    bool(security_labels) and all(label == "security" for label in security_labels)
                ),
                "n_dependabot": source_counts["dependabot"],
                "n_renovate": source_counts["renovate"],
                "n_human": source_counts["human"],
                "n_other_bot": source_counts["other-bot"],
                "source_mix": _source_mix(source_counts),
                "alignment": _alignment_score(
                    [
                        decision_counts["merged"],
                        decision_counts["closed-unmerged"],
                    ],
                    k_projects_decided,
                ),
                "mean_group_size": fmean(group_sizes),
                "median_group_size": float(median(group_sizes)),
                "max_group_size": max(group_sizes),
                "all_singleton": int(all(size == 1 for size in group_sizes)),
                "computed_at": computed_at,
            }
        )

    persisted_rows = [row for row in alignment_rows if row["k_projects_decided"] >= 2]
    store.replace_triple_alignment(persisted_rows)

    triples_k_ge_5_per_ecosystem = {
        ecosystem: sum(
            1
            for row in alignment_rows
            if row["ecosystem"] == ecosystem and row["k_projects_decided"] >= 5
        )
        for ecosystem in ("maven", "npm", "cargo", "pip", "go")
    }

    return {
        "triples_total": len(alignment_rows),
        "triples_k_ge_2": sum(1 for row in alignment_rows if row["k_projects_decided"] >= 2),
        "triples_k_ge_3": sum(1 for row in alignment_rows if row["k_projects_decided"] >= 3),
        "triples_k_ge_5": sum(1 for row in alignment_rows if row["k_projects_decided"] >= 5),
        "triples_k_ge_5_per_ecosystem": triples_k_ge_5_per_ecosystem,
        "triples_k_ge_5_all_singleton": sum(
            1
            for row in alignment_rows
            if row["k_projects_decided"] >= 5 and row["all_singleton"] == 1
        ),
        "triples_with_open_projects": triples_with_open_projects,
        "triples_with_non_decision_projects": triples_with_non_decision_projects,
        "triples_dropped_zero_decided": triples_dropped_zero_decided,
        "triples_dropped_only_superseded": triples_dropped_only_superseded,
        "triples_below_k2_after_non_decision_exclusion": (
            triples_below_k2_after_non_decision_exclusion + triples_dropped_zero_decided
        ),
        "open_change_rows_excluded": excluded_change_rows - superseded_change_rows_excluded,
        "superseded_change_rows_excluded": superseded_change_rows_excluded,
        "non_decision_change_rows_excluded": excluded_change_rows,
        "fork_or_archived_change_rows_excluded": sum(excluded_by_ecosystem.values()),
        "fork_or_archived_change_rows_excluded_per_ecosystem": {
            ecosystem: excluded_by_ecosystem.get(ecosystem, 0)
            for ecosystem in ("maven", "npm", "cargo", "pip", "go")
        },
        "elapsed_seconds": time.perf_counter() - started_at,
    }


def _fetch_contributing_rows(store: Store) -> list[dict[str, Any]]:
    """Fetch classified change rows that contribute to alignment metrics."""
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT
              change.id AS change_id,
              change.ecosystem,
              change.package,
              change.from_version,
              change.to_version,
              pr.repo_owner,
              pr.repo_name,
              pr.is_fork,
              pr.is_archived,
              change.group_size,
              pr.event_created_at,
              outcome.label AS outcome,
              semver.label AS semver_tier,
              security.label AS security,
              source.label AS source
            FROM change
            JOIN pr ON pr.id = change.pr_id
            JOIN classification AS outcome ON outcome.change_id = change.id
              AND outcome.dimension = 'outcome'
              AND outcome.classifier_version = ?
            JOIN classification AS semver ON semver.change_id = change.id
              AND semver.dimension = 'semver_tier'
              AND semver.classifier_version = ?
            JOIN classification AS security ON security.change_id = change.id
              AND security.dimension = 'security'
              AND security.classifier_version = ?
            JOIN classification AS source ON source.change_id = change.id
              AND source.dimension = 'source'
              AND source.classifier_version = ?
            """,
            (CLASSIFIER_VERSION, CLASSIFIER_VERSION, CLASSIFIER_VERSION, CLASSIFIER_VERSION),
        ).fetchall()
    return [dict(row) for row in rows]


def _group_by_triple(rows: list[dict[str, Any]]) -> dict[TripleKey, list[dict[str, Any]]]:
    """Group classified rows by ecosystem, package, and version triple."""
    grouped: dict[TripleKey, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row["ecosystem"],
            row["package"],
            row["from_version"],
            row["to_version"],
        )
        grouped[key].append(row)
    return grouped


def _latest_decision_by_project(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Keep the latest merged/closed decision for each project in a triple."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        project = (row["repo_owner"], row["repo_name"])
        current = latest.get(project)
        if current is None or _event_sort_key(row) > _event_sort_key(current):
            latest[project] = row
    return {project: row["outcome"] for project, row in latest.items()}


def _latest_excluded_by_project(
    rows: list[dict[str, Any]],
    decided_projects: set[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """Keep the latest excluded outcome for projects with no decided outcome."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        project = (row["repo_owner"], row["repo_name"])
        if project in decided_projects or row["outcome"] not in EXCLUDED_LABELS:
            continue
        current = latest.get(project)
        if current is None or _event_sort_key(row) > _event_sort_key(current):
            latest[project] = row
    return {project: row["outcome"] for project, row in latest.items()}


def _event_sort_key(row: dict[str, Any]) -> tuple[str, int]:
    """Sort PR events by creation time and change id."""
    return (row["event_created_at"] or "", int(row["change_id"]))


def _semver_tier_for_triple(rows: list[dict[str, Any]], key: TripleKey) -> str:
    """Choose the modal semver tier for a shared version triple."""
    counts = Counter(row["semver_tier"] for row in rows)
    if len(counts) > 1:
        LOGGER.warning(
            "mixed semver tiers for triple %s: %s; using modal value",
            key,
            dict(sorted(counts.items())),
        )
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _source_mix(source_counts: Counter[str]) -> str:
    """Summarize the source composition of a triple."""
    total = sum(source_counts[label] for label in SOURCE_LABELS)
    if total == 0:
        return "mixed_other"
    for label, mix in (
        ("dependabot", "all_dependabot"),
        ("renovate", "all_renovate"),
        ("human", "all_human"),
        ("other-bot", "all_other_bot"),
    ):
        if source_counts[label] == total:
            return mix
    bot_total = source_counts["dependabot"] + source_counts["renovate"] + source_counts["other-bot"]
    if source_counts["human"] == 0 and bot_total > 0:
        return "mixed_bots"
    if source_counts["human"] > 0 and bot_total > 0:
        return "mixed_bot_human"
    return "mixed_other"


def _alignment_score(counts: list[int], k_projects: int) -> float:
    """Compute the normalized alignment score from decision counts."""
    if k_projects <= 1 or max(counts, default=0) == k_projects:
        return 1.0

    entropy = 0.0
    for count in counts:
        if count == 0:
            continue
        probability = count / k_projects
        entropy -= probability * math.log(probability)
    return 1.0 - (entropy / math.log(2))
