"""Stratified SQ2 alignment summaries."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from deptracker.store import Store

MIN_CI_N = 30


def analyze_sq2_strata(
    store: Store,
    output_path: str | Path = "data/sq2_strata.json",
    *,
    min_k_projects_decided: int = 5,
    bootstrap_resamples: int = 10_000,
    seed: int = 42,
) -> dict[str, Any]:
    """Summarize alignment triples at or above a decided-project threshold."""
    rows = _fetch_rows(store, min_k_projects_decided=min_k_projects_decided)
    rng = np.random.default_rng(seed)
    payload = {
        "min_k_projects_decided": min_k_projects_decided,
        "bootstrap_resamples": bootstrap_resamples,
        "seed": seed,
        "min_ci_n": MIN_CI_N,
        "overall": summarize_values(
            [row["alignment"] for row in rows],
            bootstrap_resamples=bootstrap_resamples,
            rng=rng,
            suppress_small_ci=False,
        ),
        "single_strata": {
            field: summarize_strata(rows, [field], bootstrap_resamples=bootstrap_resamples, rng=rng)
            for field in ("ecosystem", "semver_tier", "is_security_any", "source_mix")
        },
        "two_way_strata": {
            "ecosystem_x_semver_tier": summarize_strata(
                rows,
                ["ecosystem", "semver_tier"],
                bootstrap_resamples=bootstrap_resamples,
                rng=rng,
            ),
            "ecosystem_x_source_mix": summarize_strata(
                rows,
                ["ecosystem", "source_mix"],
                bootstrap_resamples=bootstrap_resamples,
                rng=rng,
            ),
            "semver_tier_x_is_security_any": summarize_strata(
                rows,
                ["semver_tier", "is_security_any"],
                bootstrap_resamples=bootstrap_resamples,
                rng=rng,
            ),
            "semver_tier_x_source_mix": summarize_strata(
                rows,
                ["semver_tier", "source_mix"],
                bootstrap_resamples=bootstrap_resamples,
                rng=rng,
            ),
        },
        "group_size_correlations": correlation_summary(
            rows,
            group_field=None,
            bootstrap_resamples=bootstrap_resamples,
            rng=rng,
        ),
        "group_size_correlations_by_ecosystem": correlation_summary(
            rows,
            group_field="ecosystem",
            bootstrap_resamples=bootstrap_resamples,
            rng=rng,
        ),
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def summarize_strata(
    rows: list[dict[str, Any]],
    fields: list[str],
    *,
    bootstrap_resamples: int,
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    """Summarize alignment values grouped by one or more fields."""
    grouped: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row[field] for field in fields)].append(float(row["alignment"]))
    result = []
    for key, values in sorted(grouped.items(), key=lambda item: tuple(str(part) for part in item[0])):
        summary = summarize_values(
            values,
            bootstrap_resamples=bootstrap_resamples,
            rng=rng,
            suppress_small_ci=True,
        )
        result.append(
            {
                "values": {field: value for field, value in zip(fields, key, strict=True)},
                **summary,
            }
        )
    return result


def summarize_values(
    values: list[float],
    *,
    bootstrap_resamples: int,
    rng: np.random.Generator,
    suppress_small_ci: bool,
) -> dict[str, Any]:
    """Return n, mean, median, and bootstrap CIs for a vector."""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "ci_mean_lo": None,
            "ci_mean_hi": None,
            "ci_median_lo": None,
            "ci_median_hi": None,
            "reportable": False,
            "ci_note": "no data",
        }
    reportable = array.size >= MIN_CI_N
    summary = {
        "n": int(array.size),
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "ci_mean_lo": None,
        "ci_mean_hi": None,
        "ci_median_lo": None,
        "ci_median_hi": None,
        "reportable": bool(reportable),
        "ci_note": None,
    }
    if suppress_small_ci and not reportable:
        summary["ci_note"] = "n too small"
        return summary
    sample_index = rng.integers(0, array.size, size=(bootstrap_resamples, array.size))
    samples = array[sample_index]
    means = np.mean(samples, axis=1)
    medians = np.median(samples, axis=1)
    summary.update(
        {
            "ci_mean_lo": float(np.percentile(means, 2.5)),
            "ci_mean_hi": float(np.percentile(means, 97.5)),
            "ci_median_lo": float(np.percentile(medians, 2.5)),
            "ci_median_hi": float(np.percentile(medians, 97.5)),
        }
    )
    return summary


def correlation_summary(
    rows: list[dict[str, Any]],
    *,
    group_field: str | None,
    bootstrap_resamples: int,
    rng: np.random.Generator,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Compute Pearson and Spearman correlations overall or by group."""
    if group_field is None:
        return _correlation_for_rows(rows, bootstrap_resamples=bootstrap_resamples, rng=rng)
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[group_field]].append(row)
    result = []
    for group, group_rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        result.append(
            {
                group_field: group,
                **_correlation_for_rows(group_rows, bootstrap_resamples=bootstrap_resamples, rng=rng),
            }
        )
    return result


def _correlation_for_rows(
    rows: list[dict[str, Any]],
    *,
    bootstrap_resamples: int,
    rng: np.random.Generator,
) -> dict[str, Any]:
    """Compute correlation estimates and bootstrap intervals for one row set."""
    x = np.asarray([row["mean_group_size"] for row in rows], dtype=float)
    y = np.asarray([row["alignment"] for row in rows], dtype=float)
    if x.size < 3 or np.std(x) == 0 or np.std(y) == 0:
        return {
            "n": int(x.size),
            "pearson": None,
            "pearson_ci_lo": None,
            "pearson_ci_hi": None,
            "spearman": None,
            "spearman_ci_lo": None,
            "spearman_ci_hi": None,
            "ci_note": "n too small or no variance",
        }
    pearson = _pearson(x, y)
    spearman = _pearson(_rankdata(x), _rankdata(y))
    pearson_samples = []
    spearman_samples = []
    for _ in range(bootstrap_resamples):
        idx = rng.integers(0, x.size, size=x.size)
        bx = x[idx]
        by = y[idx]
        if np.std(bx) == 0 or np.std(by) == 0:
            continue
        pearson_samples.append(_pearson(bx, by))
        spearman_samples.append(_pearson(_rankdata(bx), _rankdata(by)))
    return {
        "n": int(x.size),
        "pearson": pearson,
        "pearson_ci_lo": _percentile_or_none(pearson_samples, 2.5),
        "pearson_ci_hi": _percentile_or_none(pearson_samples, 97.5),
        "spearman": spearman,
        "spearman_ci_lo": _percentile_or_none(spearman_samples, 2.5),
        "spearman_ci_hi": _percentile_or_none(spearman_samples, 97.5),
        "ci_note": None if len(pearson_samples) == bootstrap_resamples else "some resamples skipped",
    }


def _fetch_rows(store: Store, *, min_k_projects_decided: int) -> list[dict[str, Any]]:
    """Load alignment rows that meet a decided-project threshold."""
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT
              ecosystem,
              semver_tier,
              is_security_any,
              source_mix,
              alignment,
              mean_group_size
            FROM triple_alignment
            WHERE k_projects_decided >= ?
            """,
            (min_k_projects_decided,),
        ).fetchall()
    return [dict(row) for row in rows]


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    """Return the Pearson correlation coefficient for two vectors."""
    return float(np.corrcoef(x, y)[0, 1])


def _rankdata(values: np.ndarray) -> np.ndarray:
    """Average-rank transform for Spearman correlation."""
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    sorted_values = values[order]
    start = 0
    while start < values.size:
        end = start + 1
        while end < values.size and sorted_values[end] == sorted_values[start]:
            end += 1
        rank = (start + end - 1) / 2 + 1
        ranks[order[start:end]] = rank
        start = end
    return ranks


def _percentile_or_none(values: list[float], percentile: float) -> float | None:
    """Return a percentile when bootstrap samples exist."""
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), percentile))


def format_sq2_summary(payload: dict[str, Any]) -> str:
    """Format a compact Markdown summary for CLI output."""
    overall = payload["overall"]
    lines = [
        "| scope | n | mean alignment | median alignment |",
        "|---|---:|---:|---:|",
        (
            f"| overall | {overall['n']} | "
            f"{overall['mean']:.3f} ({overall['ci_mean_lo']:.3f}, {overall['ci_mean_hi']:.3f}) | "
            f"{overall['median']:.3f} ({overall['ci_median_lo']:.3f}, {overall['ci_median_hi']:.3f}) |"
        ),
    ]
    for field, rows in payload["single_strata"].items():
        reportable_rows = [row for row in rows if row["reportable"]]
        for row in sorted(reportable_rows, key=lambda item: item["mean"]):
            label = row["values"][field]
            mean = f"{row['mean']:.3f}" if row["mean"] is not None else "NA"
            median = f"{row['median']:.3f}" if row["median"] is not None else "NA"
            lines.append(f"| {field}={label} | {row['n']} | {mean} | {median} |")
    return "\n".join(lines)
