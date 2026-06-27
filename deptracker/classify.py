"""Heuristic classification for parsed dependency changes."""

from __future__ import annotations

import re
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import PurePosixPath
from collections.abc import Iterator
from typing import Any

from packaging.version import InvalidVersion, Version

from deptracker.adapters.base import adapter_for_file
from deptracker.diffutils import normalise_version
from deptracker.store import Store
from deptracker.supersession import detect_supersession_from_comments

CLASSIFIER_VERSION = 1
DIMENSIONS = ("source", "semver_tier", "security", "direct_transitive", "outcome")
CURATED_BOT_LOGINS = frozenset(
    {
        "pyup-bot",
        "pre-commit-ci",
        "whitesource-bolt-for-github",
        "snyk-bot",
        "mend-for-github-com",
        "restyled-io",
        "github-actions",
        "imgbot",
        "allcontributors",
    }
)

_DEPENDABOT_RE = re.compile(r"^dependabot(\[bot\])?$", re.IGNORECASE)
_RENOVATE_RE = re.compile(r"^(renovate|renovate-bot)(\[bot\])?$", re.IGNORECASE)
_BOT_SUFFIX_RE = re.compile(r"\[bot\]$", re.IGNORECASE)
_SECURITY_RE = re.compile(
    r"(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}|CVE-\d{4}-\d{4,})",
    re.IGNORECASE,
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_CALVER_RE = re.compile(r"^\d{4}\.\d{1,2}(?:\.\d{1,2})?$")
_PRERELEASE_RE = re.compile(r"-[0-9A-Za-z]")
_SUPERSEDED_RE = re.compile(r"[Ss]uperseded by #\d+")


def classify_source(
    actor_login: str | None,
    author_login: str | None,
    author_type: str | None = None,
) -> str:
    """Classify the PR source, preferring PR author data over event actor data."""
    if author_login:
        return _classify_login(author_login, author_type=author_type)
    if actor_login:
        return _classify_login(actor_login)
    return "human"


def _classify_login(login: str, *, author_type: str | None = None) -> str:
    """Classify one GitHub login using explicit bot patterns and author type."""
    if _DEPENDABOT_RE.fullmatch(login):
        return "dependabot"
    if _RENOVATE_RE.fullmatch(login):
        return "renovate"
    if (author_type or "").lower() == "bot":
        return "other-bot"
    if _BOT_SUFFIX_RE.search(login) or _is_curated_bot(login):
        return "other-bot"
    return "human"


def classify_semver_tier(
    from_version: str | None,
    to_version: str,
    ecosystem: str,  # noqa: ARG001 - reserved for ecosystem-specific refinements.
) -> str:
    """Classify the semver tier change between two versions."""
    if from_version is None:
        return "unknown"

    from_clean = normalise_version(from_version)
    to_clean = normalise_version(to_version)

    if _SHA_RE.fullmatch(from_clean) or _SHA_RE.fullmatch(to_clean):
        return "sha-pin"
    if _CALVER_RE.fullmatch(from_clean) or _CALVER_RE.fullmatch(to_clean):
        return "calver"
    if _PRERELEASE_RE.search(from_clean) or _PRERELEASE_RE.search(to_clean):
        return "prerelease"

    from_parts = _version_parts(from_clean)
    to_parts = _version_parts(to_clean)
    if from_parts is None or to_parts is None:
        return "unknown"

    if from_parts[0] != to_parts[0]:
        return "major"
    if from_parts[1] != to_parts[1]:
        return "minor"
    if from_parts[2] != to_parts[2]:
        return "patch"
    return "unknown"


def classify_security(
    title: str | None,
    body: str | None,
    commit_messages_concat: str | None = None,
) -> str:
    """Classify whether the PR looks security-related."""
    text = "\n".join(part for part in (title, body, commit_messages_concat) if part)
    return "security" if _SECURITY_RE.search(text) else "non-security"


def classify_direct_transitive(
    is_lockfile: int | bool,
    manifest_path: str,  # noqa: ARG001 - path retained for stable public helper signature.
    has_direct_manifest_change_for_package: int | bool = False,
    package: str | None = None,
    title: str | None = None,
    body: str | None = None,
    files_json: str | None = None,
    ecosystem: str | None = None,
) -> str:
    """Classify directness from the PR-level package evidence.

    Direct manifest rows are always direct. Lockfile rows are transitive only
    when the same package is not changed in a non-lockfile manifest in the same
    PR. If parser coverage missed a nested manifest section, a lockfile row can
    still be direct when the PR changed a same-directory direct manifest and
    Dependabot/Renovate-style PR text explicitly names the package.
    """
    if not bool(is_lockfile):
        return "direct"
    if bool(has_direct_manifest_change_for_package):
        return "direct"
    if _metadata_indicates_direct_lockfile_target(
        manifest_path=manifest_path,
        package=package,
        title=title,
        body=body,
        files_json=files_json,
        ecosystem=ecosystem,
    ):
        return "direct"
    return "transitive"


def classify_outcome(
    state: str | None,
    merged: int | bool | None,
    closed_at: str | None,  # noqa: ARG001 - retained for the stable classifier signature.
    merged_at: str | None,  # noqa: ARG001 - retained for the stable classifier signature.
    title: str | None,
    body: str | None,
    closing_comments_json: str | None = None,
) -> str:
    """Classify the PR outcome from its state and text hints."""
    outcome, _reason = classify_outcome_with_reason(
        state,
        merged,
        closed_at,
        merged_at,
        title,
        body,
        closing_comments_json,
    )
    return outcome


def classify_outcome_with_reason(
    state: str | None,
    merged: int | bool | None,
    closed_at: str | None,  # noqa: ARG001 - retained for the stable classifier signature.
    merged_at: str | None,  # noqa: ARG001 - retained for the stable classifier signature.
    title: str | None,
    body: str | None,
    closing_comments_json: str | None = None,
) -> tuple[str, str | None]:
    """Classify PR outcome and return any superseded reason."""
    # TODO: superseded-by-newer-PR within seven days requires cross-PR lookup.
    if bool(merged):
        return "merged", None
    if state and state.lower() == "open":
        return "open", None
    if state and state.lower() == "closed":
        text = "\n".join(part for part in (title, body) if part)
        if _SUPERSEDED_RE.search(text):
            return "superseded", "title_body"
        comment_match = detect_supersession_from_comments(closing_comments_json)
        if comment_match:
            return "superseded", comment_match.reason
        return "closed-unmerged", None
    return "unknown", None


def classify(
    store: Store,
    classifier_version: int = CLASSIFIER_VERSION,
    force_dimensions: list[str] | None = None,
) -> dict[str, Any]:
    """Classify all pending changes in the store for the selected dimensions."""
    dimensions = _selected_dimensions(force_dimensions)
    old_labels = (
        _existing_labels(store, classifier_version, dimensions) if force_dimensions else {}
    )
    deleted = store.delete_classifications_by_dimensions(dimensions, classifier_version) if force_dimensions else 0
    classified_at = datetime.now(UTC).isoformat()
    distributions: dict[str, Counter[str]] = defaultdict(Counter)
    flips: dict[str, Counter[str]] = {dimension: Counter() for dimension in dimensions}
    changes_classified = 0
    rows_inserted = 0
    pr_reason_updates: dict[int, str | None] = {}

    for rows in _iter_rows_needing_classification(store, classifier_version, dimensions):
        changes_classified += len(rows)
        classification_rows: list[dict[str, Any]] = []
        for row in rows:
            labels, metadata = labels_and_metadata_for_row(row)
            if "outcome" in dimensions:
                pr_reason_updates[int(row["pr_id"])] = metadata["superseded_reason"]
            for dimension in dimensions:
                label = labels[dimension]
                classification_rows.append(
                    {
                        "change_id": row["change_id"],
                        "dimension": dimension,
                        "label": label,
                        "classifier_version": classifier_version,
                        "classified_at": classified_at,
                    }
                )
                distributions[dimension][label] += 1
                if old_label := old_labels.get(row["change_id"], {}).get(dimension):
                    if old_label != label:
                        flips[dimension][f"{old_label}->{label}"] += 1
        rows_inserted += store.insert_classifications(classification_rows)
        if "outcome" in dimensions:
            store.update_pr_superseded_reasons(pr_reason_updates)
            pr_reason_updates.clear()

    return {
        "classifier_version": classifier_version,
        "forced_dimensions": dimensions if force_dimensions else [],
        "classification_rows_deleted": deleted,
        "changes_classified": changes_classified,
        "classification_rows_inserted": rows_inserted,
        "label_distribution": {
            dimension: dict(sorted(distributions[dimension].items())) for dimension in dimensions
        },
        "forced_dimension_flips": {
            dimension: dict(sorted(flips[dimension].items()))
            for dimension in dimensions
            if flips[dimension]
        },
    }


def labels_for_row(row: Any) -> dict[str, str]:
    """Build all classifier labels for a single change row."""
    labels, _metadata = labels_and_metadata_for_row(row)
    return labels


def labels_and_metadata_for_row(row: Any) -> tuple[dict[str, str], dict[str, str | None]]:
    """Build classifier labels plus PR-level classifier side effects."""
    outcome, superseded_reason = classify_outcome_with_reason(
        row["state"],
        row["merged"],
        row["closed_at"],
        row["merged_at"],
        row["title"],
        row["body"],
        row["closing_comments_json"],
    )
    return {
        "source": classify_source(
            row["actor_login"],
            row["author_login"],
            row["author_type"],
        ),
        "semver_tier": classify_semver_tier(
            row["from_version"],
            row["to_version"],
            row["ecosystem"],
        ),
        "security": classify_security(
            row["title"],
            row["body"],
            row["commit_messages_concat"],
        ),
        "direct_transitive": classify_direct_transitive(
            row["is_lockfile"],
            row["manifest_path"],
            row["has_direct_manifest_change_for_package"],
            row["package"],
            row["title"],
            row["body"],
            row["files_json"],
            row["ecosystem"],
        ),
        "outcome": outcome,
    }, {"superseded_reason": superseded_reason}


def _iter_rows_needing_classification(
    store: Store,
    classifier_version: int,
    dimensions: tuple[str, ...],
) -> Iterator[list[Any]]:
    """Yield changes missing at least one requested classification dimension in batches."""
    missing_conditions = " OR ".join(
        """
        NOT EXISTS (
          SELECT 1
          FROM classification
          WHERE classification.change_id = change.id
            AND classification.classifier_version = ?
            AND classification.dimension = ?
        )
        """
        for _ in dimensions
    )
    dimension_params: list[Any] = []
    for dimension in dimensions:
        dimension_params.extend([classifier_version, dimension])

    last_change_id = 0
    batch_size = 5_000
    while True:
        params: list[Any] = [last_change_id, *dimension_params, batch_size]
        with store._connect() as conn:
            rows = conn.execute(
            f"""
            SELECT
              change.id AS change_id,
              pr.id AS pr_id,
              change.ecosystem,
              change.package,
              change.from_version,
              change.to_version,
              change.manifest_path,
              change.is_lockfile,
              EXISTS (
                SELECT 1
                FROM change AS manifest_change
                WHERE manifest_change.pr_id = change.pr_id
                  AND manifest_change.ecosystem = change.ecosystem
                  AND manifest_change.package = change.package
                  AND manifest_change.is_lockfile = 0
              ) AS has_direct_manifest_change_for_package,
              pr.actor_login,
              pr.author_login,
              pr.author_type,
              pr.title,
              pr.body,
              pr.files_json,
              pr.commit_messages_concat,
              pr.state,
              pr.merged,
              pr.closed_at,
              pr.merged_at,
              (
                SELECT COALESCE(
                  json_group_array(
                    json_object(
                      'author_login', c.author_login,
                      'author_type', c.author_type,
                      'body', c.body,
                      'created_at', c.created_at
                    )
                  ),
                  '[]'
                )
                FROM (
                  SELECT author_login, author_type, body, created_at
                  FROM pr_closing_comment
                  WHERE pr_id = pr.id
                  ORDER BY created_at ASC, id ASC
                ) AS c
              ) AS closing_comments_json
            FROM change
            JOIN pr ON pr.id = change.pr_id
            WHERE change.id > ?
              AND ({missing_conditions})
            ORDER BY change.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        if not rows:
            break
        yield rows
        last_change_id = int(rows[-1]["change_id"])


def _existing_labels(
    store: Store,
    classifier_version: int,
    dimensions: tuple[str, ...],
) -> dict[int, dict[str, str]]:
    """Load existing labels for forced reclassification bookkeeping."""
    placeholders = ", ".join("?" for _ in dimensions)
    with store._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT change_id, dimension, label
            FROM classification
            WHERE classifier_version = ?
              AND dimension IN ({placeholders})
            """,
            (classifier_version, *dimensions),
        ).fetchall()
    labels: dict[int, dict[str, str]] = defaultdict(dict)
    for row in rows:
        labels[row["change_id"]][row["dimension"]] = row["label"]
    return labels


def _selected_dimensions(force_dimensions: list[str] | None) -> tuple[str, ...]:
    """Validate and normalize the requested classifier dimensions."""
    if not force_dimensions:
        return DIMENSIONS
    invalid = sorted(set(force_dimensions) - set(DIMENSIONS))
    if invalid:
        raise ValueError(f"unknown classification dimensions: {', '.join(invalid)}")
    return tuple(dict.fromkeys(force_dimensions))


def _is_curated_bot(login: str) -> bool:
    """Return True when the login is a known non-dependabot bot."""
    normalized = login.lower()
    if normalized.endswith("[bot]"):
        normalized = normalized[: -len("[bot]")]
    return normalized in CURATED_BOT_LOGINS


def _metadata_indicates_direct_lockfile_target(
    *,
    manifest_path: str,
    package: str | None,
    title: str | None,
    body: str | None,
    files_json: str | None,
    ecosystem: str | None,
) -> bool:
    """Use conservative metadata evidence for lockfile-only direct updates."""
    if not package or not ecosystem or not _package_mentioned(package, title, body):
        return False
    if not _same_directory_direct_manifest_changed(manifest_path, files_json, ecosystem):
        return False
    return True


def _same_directory_direct_manifest_changed(
    manifest_path: str,
    files_json: str | None,
    ecosystem: str,
) -> bool:
    """Return True when a direct manifest changed next to this lockfile."""
    try:
        files = json.loads(files_json or "[]")
    except json.JSONDecodeError:
        return False
    lock_parent = PurePosixPath(manifest_path).parent
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        path = file_info.get("path")
        if not isinstance(path, str):
            continue
        adapter = adapter_for_file(path)
        if not adapter or adapter.name != ecosystem:
            continue
        if adapter.is_lockfile_path(path):
            continue
        if PurePosixPath(path).parent == lock_parent:
            return True
    return False


def _package_mentioned(package: str, title: str | None, body: str | None) -> bool:
    """Conservatively check whether PR metadata names the package."""
    text = "\n".join(part for part in (title, body) if part)
    if not text:
        return False
    candidates = {package.lower()}
    if ":" in package:
        candidates.add(package.rsplit(":", 1)[-1].lower())
    candidates.add(package.replace("_", "-").lower())
    candidates.add(package.replace("-", "_").lower())
    return any(_token_mentioned(candidate, text) for candidate in candidates if candidate)


def _token_mentioned(candidate: str, text: str) -> bool:
    """Return True when candidate appears as a whole package-name token."""
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.\-/]){re.escape(candidate)}(?![A-Za-z0-9_.\-/])",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


def _version_parts(version: str) -> tuple[int, int, int] | None:
    """Extract the first three numeric release components from a version string."""
    try:
        release = Version(version).release
    except InvalidVersion:
        release = _fallback_release(version)
    if not release:
        return None
    padded = tuple(release[:3]) + (0,) * (3 - len(release[:3]))
    return padded[:3]


def _fallback_release(version: str) -> tuple[int, ...]:
    """Extract numeric release parts from an unparsable version string."""
    parts = [int(part) for part in re.findall(r"\d+", version)]
    return tuple(parts)
