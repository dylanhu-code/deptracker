from deptracker.classify import (
    classify,
    classify_direct_transitive,
    classify_outcome,
    classify_security,
    classify_semver_tier,
    classify_source,
)
from deptracker.store import Store


def test_classify_source_branches() -> None:
    """Exercise human, known-bot, and typed-bot source branches."""
    assert classify_source("dependabot[bot]", None) == "dependabot"
    assert classify_source(None, "renovate-bot") == "renovate"
    assert classify_source("helper[bot]", None) == "other-bot"
    assert classify_source("pre-commit-ci", None) == "other-bot"
    assert classify_source("maintainer", "author") == "human"
    assert classify_source("dependabot[bot]", "maintainer", "User") == "human"
    assert classify_source(None, "automation-service", "Bot") == "other-bot"
    assert classify_source("maintainer", "renovate[bot]", "Bot") == "renovate"


def test_classify_semver_tier_branches() -> None:
    """Exercise each supported semver-tier label."""
    assert classify_semver_tier("1.2.3", "1.2.4", "npm") == "patch"
    assert classify_semver_tier("1.2.3", "1.3.0", "npm") == "minor"
    assert classify_semver_tier("1.2.3", "2.0.0", "npm") == "major"
    assert classify_semver_tier("1.2.3", "1.2.4-rc1", "npm") == "prerelease"
    assert classify_semver_tier("2025.12", "2026.1", "pip") == "calver"
    assert (
        classify_semver_tier(
            "a" * 40,
            "b" * 40,
            "go",
        )
        == "sha-pin"
    )
    assert classify_semver_tier(None, "1.0.0", "cargo") == "unknown"


def test_classify_security() -> None:
    """Detect security identifiers across PR metadata fields."""
    assert classify_security("Fix GHSA-abcd-1234-efgh", None) == "security"
    assert classify_security(None, "Addresses CVE-2026-12345") == "security"
    assert classify_security("Update dependency", "", "Fixes CVE-2026-12345") == "security"
    assert classify_security("Update dependency", "Routine version bump") == "non-security"


def test_classify_direct_transitive() -> None:
    """Exercise directness rules for manifest and lockfile changes."""
    assert classify_direct_transitive(False, "package.json") == "direct"
    assert classify_direct_transitive(1, "package-lock.json") == "transitive"
    assert classify_direct_transitive(1, "package-lock.json", True) == "direct"
    assert (
        classify_direct_transitive(
            1,
            "frontend/package-lock.json",
            False,
            "react",
            "Bump react from 18.2.0 to 18.3.0",
            "",
            '[{"path": "frontend/package.json"}]',
            "npm",
        )
        == "direct"
    )
    assert (
        classify_direct_transitive(
            1,
            "backend/package-lock.json",
            False,
            "react",
            "Bump react from 18.2.0 to 18.3.0",
            "",
            '[{"path": "frontend/package.json"}]',
            "npm",
        )
        == "transitive"
    )
    assert (
        classify_direct_transitive(
            1,
            "frontend/package-lock.json",
            False,
            "lodash.merge",
            "Bump lodash from 4.17.20 to 4.17.21",
            "",
            '[{"path": "frontend/package.json"}]',
            "npm",
        )
        == "transitive"
    )
    assert (
        classify_direct_transitive(
            1,
            "frontend/package-lock.json",
            False,
            "lodash",
            "Bump lodash from 4.17.20 to 4.17.21",
            "",
            '[{"path": "frontend/package.json"}]',
            "npm",
        )
        == "direct"
    )
    assert (
        classify_direct_transitive(
            1,
            "frontend/package-lock.json",
            False,
            "react",
            "Bump unrelated from 1.0.0 to 1.1.0",
            "",
            '[{"path": "frontend/package.json"}]',
            "npm",
        )
        == "transitive"
    )


def test_classify_direct_transitive_uses_pr_level_manifest_package_evidence(tmp_path) -> None:
    """Use same-PR manifest evidence to classify matching lockfile rows."""
    store = Store(tmp_path / "direct.sqlite")
    store.init_schema()
    pr_id = _insert_pr(store)
    store.insert_changes(
        pr_id,
        [
            {
                "ecosystem": "npm",
                "package": "react",
                "from_version": "18.2.0",
                "to_version": "18.3.0",
                "manifest_path": "package.json",
                "is_lockfile": False,
            },
            {
                "ecosystem": "npm",
                "package": "react",
                "from_version": "18.2.0",
                "to_version": "18.3.0",
                "manifest_path": "package-lock.json",
                "is_lockfile": True,
            },
            {
                "ecosystem": "npm",
                "package": "transitive-only",
                "from_version": "1.0.0",
                "to_version": "1.0.1",
                "manifest_path": "package-lock.json",
                "is_lockfile": True,
            },
        ],
    )

    classify(store, force_dimensions=["direct_transitive"])

    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT change.package, change.is_lockfile, classification.label
            FROM change
            JOIN classification ON classification.change_id = change.id
            WHERE classification.dimension = 'direct_transitive'
            ORDER BY change.id
            """
        ).fetchall()

    assert [row["label"] for row in rows] == ["direct", "direct", "transitive"]


def test_classify_outcome_branches() -> None:
    """Exercise merged, open, closed, and superseded outcome labels."""
    assert classify_outcome("CLOSED", 1, "2026-05-01", "2026-05-01", "", "") == "merged"
    assert classify_outcome("OPEN", 0, None, None, "", "") == "open"
    assert classify_outcome("CLOSED", 0, "2026-05-01", None, "No longer needed", "") == (
        "closed-unmerged"
    )
    assert classify_outcome(
        "CLOSED",
        0,
        "2026-05-01",
        None,
        "Superseded by #123",
        "",
    ) == "superseded"


def _insert_pr(store: Store) -> int:
    """Insert one minimal PR and return its generated identifier."""
    store.insert_pr_batch(
        [
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 1,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "dependabot[bot]",
                "pr_action": "opened",
            }
        ]
    )
    with store._connect() as conn:
        row = conn.execute("SELECT id FROM pr").fetchone()
    return row["id"]
