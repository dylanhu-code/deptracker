"""SQ1 effect-size analysis using Cramer's V."""

from __future__ import annotations

import itertools
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from deptracker.classify import CLASSIFIER_VERSION
from deptracker.store import Store

PR_DIMENSIONS = ("source", "outcome", "security")
CHANGE_DIMENSIONS = ("semver_tier", "direct_transitive")
DIMENSION_LEVELS = {
    "source": ("dependabot", "renovate", "human", "other-bot"),
    "outcome": ("merged", "closed-unmerged", "open", "superseded"),
    "security": ("security", "non-security"),
    "semver_tier": ("major", "minor", "patch", "prerelease", "calver", "sha-pin", "unknown"),
    "direct_transitive": ("direct", "transitive"),
}
STRATIFIER_LEVELS = {
    "ecosystem": ("maven", "npm", "cargo", "pip", "go"),
    "source": ("dependabot", "renovate", "human", "other-bot"),
}


def analyze_sq1_effects(
    store: Store,
    output_path: str | Path = "data/sq1_effect_sizes.json",
    *,
    bootstrap_resamples: int = 10_000,
    seed: int = 42,
) -> dict[str, Any]:
    """Compute SQ1 Cramer's V effect sizes and persist them as JSON."""
    pr_records, cross_ecosystem_prs = _pr_level_records(store)
    change_records = _change_level_records(store)
    results = []
    for dimension in (*PR_DIMENSIONS, *CHANGE_DIMENSIONS):
        unit = "pr" if dimension in PR_DIMENSIONS else "change"
        records = pr_records if unit == "pr" else change_records
        for stratifier in ("ecosystem", "source"):
            results.append(
                effect_size_result(
                    records,
                    dimension=dimension,
                    stratifier=stratifier,
                    unit=unit,
                    bootstrap_resamples=bootstrap_resamples,
                    seed=seed,
                )
            )
    payload = {
        "bootstrap_resamples": bootstrap_resamples,
        "seed": seed,
        "pr_level_ecosystem_assignment": "first ecosystem alphabetically for cross-ecosystem PRs",
        "cross_ecosystem_prs": cross_ecosystem_prs,
        "results": results,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def effect_size_result(
    records: list[dict[str, str]],
    *,
    dimension: str,
    stratifier: str,
    unit: str,
    bootstrap_resamples: int,
    seed: int,
) -> dict[str, Any]:
    """Compute the full-table and pairwise Cramer's V for one dimension/stratifier."""
    filtered = [
        record
        for record in records
        if record.get(dimension) in DIMENSION_LEVELS[dimension]
        and record.get(stratifier) in STRATIFIER_LEVELS[stratifier]
    ]
    strat_counts = Counter(record[stratifier] for record in filtered)
    strat_levels = [
        level for level in STRATIFIER_LEVELS[stratifier] if strat_counts.get(level, 0) > 0
    ]
    dimension_levels = [
        level
        for level in DIMENSION_LEVELS[dimension]
        if any(record[dimension] == level for record in filtered)
    ]
    table = contingency_table(filtered, stratifier, dimension, strat_levels, dimension_levels)
    rng = np.random.default_rng(seed)
    overall_v = cramers_v(table)
    ci_lo, ci_hi = bootstrap_cramers_v(table, bootstrap_resamples, rng)
    pairwise = []
    excluded_levels = [
        {"level": level, "n": strat_counts[level]}
        for level in strat_levels
        if strat_counts[level] < 30
    ]
    for a, b in itertools.combinations(strat_levels, 2):
        if strat_counts[a] < 30 or strat_counts[b] < 30:
            continue
        pair_records = [record for record in filtered if record[stratifier] in {a, b}]
        pair_table = contingency_table(pair_records, stratifier, dimension, [a, b], dimension_levels)
        v = cramers_v(pair_table)
        pair_ci_lo, pair_ci_hi = bootstrap_cramers_v(pair_table, bootstrap_resamples, rng)
        pairwise.append(
            {
                "a": a,
                "b": b,
                "n_a": strat_counts[a],
                "n_b": strat_counts[b],
                "v": v,
                "ci_lo": pair_ci_lo,
                "ci_hi": pair_ci_hi,
                "substantive": v >= 0.10,
            }
        )
    pairwise.sort(key=lambda row: (-row["v"], row["a"], row["b"]))
    return {
        "dimension": dimension,
        "stratifier": stratifier,
        "unit": unit,
        "n": len(filtered),
        "overall_cramers_v": overall_v,
        "overall_ci_lo": ci_lo,
        "overall_ci_hi": ci_hi,
        "pairwise": pairwise,
        "excluded_pairwise_levels": excluded_levels,
    }


def contingency_table(
    records: list[dict[str, str]],
    row_key: str,
    column_key: str,
    row_levels: list[str] | tuple[str, ...],
    column_levels: list[str] | tuple[str, ...],
) -> list[list[int]]:
    """Build a contingency table from categorical records."""
    row_index = {level: index for index, level in enumerate(row_levels)}
    column_index = {level: index for index, level in enumerate(column_levels)}
    table = [[0 for _ in column_levels] for _ in row_levels]
    for record in records:
        if record[row_key] not in row_index or record[column_key] not in column_index:
            continue
        table[row_index[record[row_key]]][column_index[record[column_key]]] += 1
    return table


def cramers_v(table: list[list[int]] | np.ndarray) -> float:
    """Compute Cramer's V from a contingency table."""
    observed = np.asarray(table, dtype=float)
    if observed.size == 0:
        return 0.0
    observed = observed[observed.sum(axis=1) > 0]
    if observed.size == 0:
        return 0.0
    observed = observed[:, observed.sum(axis=0) > 0]
    if observed.size == 0:
        return 0.0
    n = observed.sum()
    rows, columns = observed.shape
    if n == 0 or rows < 2 or columns < 2:
        return 0.0
    expected = np.outer(observed.sum(axis=1), observed.sum(axis=0)) / n
    mask = expected > 0
    chi2 = np.sum(((observed - expected) ** 2)[mask] / expected[mask])
    denominator = n * min(rows - 1, columns - 1)
    if denominator <= 0:
        return 0.0
    value = math.sqrt(float(chi2 / denominator))
    return min(1.0, value)


def bootstrap_cramers_v(
    table: list[list[int]] | np.ndarray,
    resamples: int,
    rng: np.random.Generator,
) -> tuple[float | None, float | None]:
    """Bootstrap Cramer's V by resampling categorical cell counts."""
    observed = np.asarray(table, dtype=int)
    n = int(observed.sum())
    if n == 0 or resamples <= 0:
        return (None, None)
    flat = observed.ravel()
    probabilities = flat / flat.sum()
    values = np.empty(resamples, dtype=float)
    shape = observed.shape
    for index in range(resamples):
        sample = rng.multinomial(n, probabilities).reshape(shape)
        values[index] = cramers_v(sample)
    return (float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5)))


def _pr_level_records(store: Store) -> tuple[list[dict[str, str]], int]:
    """Return one PR-level analysis record per parsed PR."""
    grouped: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "ecosystems": set(),
            "source": Counter(),
            "outcome": Counter(),
            "security": Counter(),
        }
    )
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT
              change.pr_id,
              change.ecosystem,
              source.label AS source,
              outcome.label AS outcome,
              security.label AS security
            FROM change
            JOIN classification AS source ON source.change_id = change.id
              AND source.dimension = 'source'
              AND source.classifier_version = ?
            JOIN classification AS outcome ON outcome.change_id = change.id
              AND outcome.dimension = 'outcome'
              AND outcome.classifier_version = ?
            JOIN classification AS security ON security.change_id = change.id
              AND security.dimension = 'security'
              AND security.classifier_version = ?
            """,
            (CLASSIFIER_VERSION, CLASSIFIER_VERSION, CLASSIFIER_VERSION),
        ).fetchall()
    for row in rows:
        group = grouped[row["pr_id"]]
        group["ecosystems"].add(row["ecosystem"])
        group["source"][row["source"]] += 1
        group["outcome"][row["outcome"]] += 1
        group["security"][row["security"]] += 1
    records = []
    cross_ecosystem_prs = 0
    for group in grouped.values():
        ecosystems = sorted(group["ecosystems"])
        if len(ecosystems) > 1:
            cross_ecosystem_prs += 1
        records.append(
            {
                "ecosystem": ecosystems[0],
                "source": _modal(group["source"]),
                "outcome": _modal(group["outcome"]),
                "security": _modal(group["security"]),
            }
        )
    return records, cross_ecosystem_prs


def _change_level_records(store: Store) -> list[dict[str, str]]:
    """Return one change-level analysis record per classified change row."""
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT
              change.ecosystem,
              source.label AS source,
              semver.label AS semver_tier,
              direct.label AS direct_transitive
            FROM change
            JOIN classification AS source ON source.change_id = change.id
              AND source.dimension = 'source'
              AND source.classifier_version = ?
            JOIN classification AS semver ON semver.change_id = change.id
              AND semver.dimension = 'semver_tier'
              AND semver.classifier_version = ?
            JOIN classification AS direct ON direct.change_id = change.id
              AND direct.dimension = 'direct_transitive'
              AND direct.classifier_version = ?
            """,
            (CLASSIFIER_VERSION, CLASSIFIER_VERSION, CLASSIFIER_VERSION),
        ).fetchall()
    return [
        {
            "ecosystem": row["ecosystem"],
            "source": row["source"],
            "semver_tier": row["semver_tier"],
            "direct_transitive": row["direct_transitive"],
        }
        for row in rows
    ]


def _modal(counter: Counter[str]) -> str:
    """Return the deterministic modal label from a counter."""
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[0][0]


def format_sq1_summary(payload: dict[str, Any]) -> str:
    """Format a compact Markdown table for CLI output."""
    lines = [
        "| dimension | stratifier | unit | n | overall V (95% CI) | pairwise V>=0.10 |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in payload["results"]:
        ci = f"{row['overall_cramers_v']:.3f} ({row['overall_ci_lo']:.3f}, {row['overall_ci_hi']:.3f})"
        substantive = sum(1 for pair in row["pairwise"] if pair["substantive"])
        lines.append(
            f"| {row['dimension']} | {row['stratifier']} | {row['unit']} | "
            f"{row['n']} | {ci} | {substantive} |"
        )
    return "\n".join(lines)
