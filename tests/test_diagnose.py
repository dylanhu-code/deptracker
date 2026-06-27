import json

from deptracker.diagnose import diagnose
from deptracker.store import Store


def test_diagnose_persists_offline_diagnostics(tmp_path) -> None:
    """Persist manifest-filter diagnostics without network access."""
    store = Store(tmp_path / "diagnose.sqlite")
    store.init_schema()
    store.insert_pr_batch(
        [
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 1,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "synchronize",
            }
        ]
    )
    store.update_pr_enrichment(
        1,
        {
            "title": "pass",
            "files_json": [{"path": "frontend/package.json", "additions": 1, "deletions": 1}],
            "passes_manifest_only_filter": 1,
            "files_has_next_page": 0,
            "file_count": 1,
            "manifest_file_count": 1,
            "non_manifest_file_count": 0,
            "manifest_filter_reason": "passed",
            "enriched_at": "2026-05-01T00:00:00Z",
        },
    )
    output_path = tmp_path / "diagnose.json"

    result = diagnose(store, output_path=output_path)

    assert result["candidate_count_per_ecosystem"] == {"npm": 1}
    assert result["filter_pass_count_per_ecosystem"] == {"npm": 1}
    assert result["pass_filter_but_zero_changes_count_per_ecosystem"] == {"npm": 1}
    assert result["files_per_pr_distribution"]["1"] == 1
    assert json.loads(output_path.read_text()) == result


def test_diagnose_reports_pr_weighted_and_group_size_distributions(tmp_path) -> None:
    """Report PR-weighted labels and classified group-size distributions."""
    store = Store(tmp_path / "classified.sqlite")
    store.init_schema()
    store.insert_pr_batch(
        [
            _pr_row(1),
            _pr_row(2),
        ]
    )
    store.insert_changes(
        1,
        [
            _change("npm", "react", "package.json"),
            _change("pip", "requests", "requirements.txt"),
        ],
    )
    store.insert_changes(2, [_change("npm", "vite", "package.json")])
    store.insert_classifications(
        [
            _classification(change_id, dimension, label)
            for change_id, labels in {
                1: _labels("dependabot", "merged", "non-security"),
                2: _labels("dependabot", "merged", "non-security"),
                3: _labels("human", "open", "security"),
            }.items()
            for dimension, label in labels.items()
        ]
    )

    result = diagnose(store, output_path=tmp_path / "diagnose.json")

    assert result["pr_level_distributions"]["source"]["overall"]["dependabot"]["count"] == 1
    assert result["pr_level_distributions"]["source"]["overall"]["human"]["count"] == 1
    assert (
        result["pr_level_distributions"]["source"]["per_ecosystem"]["pip"]["dependabot"][
            "count"
        ]
        == 1
    )
    assert result["cross_ecosystem_classified_pr_count"] == 1
    assert result["classified_change_group_size_distribution"]["1"] == 1
    assert result["classified_change_group_size_distribution"]["2"] == 2
    assert (
        result["pr_vs_change_weighted_pr_level_dimensions"]["source"]["dependabot"][
            "pr_weighted_count"
        ]
        == 1
    )
    assert (
        result["pr_vs_change_weighted_pr_level_dimensions"]["source"]["dependabot"][
            "change_weighted_count"
        ]
        == 2
    )


def _pr_row(number: int) -> dict:
    """Build a synthetic PR discovery row."""
    return {
        "repo_owner": "owner",
        "repo_name": f"repo-{number}",
        "pr_number": number,
        "event_created_at": "2026-05-01T00:00:00Z",
        "actor_login": "actor",
        "pr_action": "opened",
    }


def _change(ecosystem: str, package: str, manifest_path: str) -> dict:
    """Build a synthetic dependency change row."""
    return {
        "ecosystem": ecosystem,
        "package": package,
        "from_version": "1.2.3",
        "to_version": "1.2.4",
        "manifest_path": manifest_path,
        "is_lockfile": False,
    }


def _labels(source: str, outcome: str, security: str) -> dict[str, str]:
    """Build the five synthetic labels for one change."""
    return {
        "source": source,
        "semver_tier": "patch",
        "security": security,
        "direct_transitive": "direct",
        "outcome": outcome,
    }


def _classification(change_id: int, dimension: str, label: str) -> dict:
    """Build one synthetic classification row."""
    return {
        "change_id": change_id,
        "dimension": dimension,
        "label": label,
        "classifier_version": 1,
        "classified_at": "2026-05-01T00:00:00Z",
    }
