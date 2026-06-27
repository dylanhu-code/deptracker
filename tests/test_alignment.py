import logging
import math

from deptracker.alignment import compute_alignment
from deptracker.store import Store


def test_alignment_is_one_when_all_projects_make_same_decision(tmp_path) -> None:
    """Assign perfect alignment when all projects merge the same triple."""
    store = Store(tmp_path / "same.sqlite")
    store.init_schema()
    for index in range(3):
        _insert_change_with_outcome(store, repo=f"repo-{index}", outcome="merged")

    summary = compute_alignment(store)

    row = _single_alignment_row(store)
    assert summary["triples_k_ge_3"] == 1
    assert row["k_projects_all"] == 3
    assert row["k_projects_decided"] == 3
    assert row["n_merged"] == 3
    assert row["alignment"] == 1.0


def test_alignment_matches_entropy_for_mixed_project_decisions(tmp_path) -> None:
    """Compute binary entropy after excluding superseded decisions."""
    store = Store(tmp_path / "mixed.sqlite")
    store.init_schema()
    for index, outcome in enumerate(["merged", "closed-unmerged", "superseded"]):
        _insert_change_with_outcome(store, repo=f"repo-{index}", outcome=outcome)

    compute_alignment(store)

    row = _single_alignment_row(store)
    expected = 0.0
    assert row["k_projects_all"] == 3
    assert row["k_projects_decided"] == 2
    assert row["n_merged"] == 1
    assert row["n_closed_unmerged"] == 1
    assert row["n_superseded"] == 1
    assert math.isclose(row["alignment"], expected)


def test_latest_pr_outcome_is_used_for_repeated_project_triple(tmp_path) -> None:
    """Use the latest decided PR when a project repeats a triple."""
    store = Store(tmp_path / "latest.sqlite")
    store.init_schema()
    _insert_change_with_outcome(
        store,
        repo="repeat-project",
        pr_number=1,
        event_created_at="2026-05-01T00:00:00Z",
        outcome="open",
    )
    _insert_change_with_outcome(
        store,
        repo="repeat-project",
        pr_number=2,
        event_created_at="2026-05-02T00:00:00Z",
        outcome="merged",
    )
    _insert_change_with_outcome(store, repo="other-project", pr_number=3, outcome="closed-unmerged")

    compute_alignment(store)

    row = _single_alignment_row(store)
    assert row["k_projects_decided"] == 2
    assert row["k_changes"] == 2
    assert row["k_changes_total"] == 3
    assert row["n_merged"] == 1
    assert row["n_closed_unmerged"] == 1
    assert row["n_open"] == 0


def test_latest_non_excluded_outcome_is_used_when_latest_pr_is_superseded(tmp_path) -> None:
    """Fall back to the latest decided outcome when a newer PR is superseded."""
    store = Store(tmp_path / "latest-superseded.sqlite")
    store.init_schema()
    _insert_change_with_outcome(
        store,
        repo="repeat-project",
        pr_number=1,
        event_created_at="2026-05-01T00:00:00Z",
        outcome="merged",
    )
    _insert_change_with_outcome(
        store,
        repo="repeat-project",
        pr_number=2,
        event_created_at="2026-05-02T00:00:00Z",
        outcome="superseded",
    )
    _insert_change_with_outcome(store, repo="other-project", pr_number=3, outcome="merged")

    compute_alignment(store)

    row = _single_alignment_row(store)
    assert row["k_projects_decided"] == 2
    assert row["k_changes"] == 2
    assert row["k_changes_total"] == 3
    assert row["n_merged"] == 2
    assert row["n_superseded"] == 0
    assert row["alignment"] == 1.0


def test_all_superseded_triple_is_not_persisted(tmp_path) -> None:
    """Drop triples that contain no decided project outcomes."""
    store = Store(tmp_path / "all-superseded.sqlite")
    store.init_schema()
    for index in range(3):
        _insert_change_with_outcome(store, repo=f"repo-{index}", outcome="superseded")

    summary = compute_alignment(store)

    assert summary["triples_dropped_zero_decided"] == 1
    assert _alignment_rows_for_comparison(store) == []


def test_group_size_statistics_are_aggregated_from_change_rows(tmp_path) -> None:
    """Aggregate group-size covariates from contributing changes."""
    store = Store(tmp_path / "groups.sqlite")
    store.init_schema()
    for index, group_size in enumerate([1, 5, 10]):
        _insert_change_with_outcome(
            store,
            repo=f"repo-{index}",
            group_size=group_size,
            outcome="merged",
        )

    compute_alignment(store)

    row = _single_alignment_row(store)
    assert row["mean_group_size"] == (1 + 5 + 10) / 3
    assert row["median_group_size"] == 5
    assert row["max_group_size"] == 10
    assert row["all_singleton"] == 0


def test_all_singleton_flag_is_set_when_every_change_is_singleton(tmp_path) -> None:
    """Flag triples whose contributing changes are all singleton updates."""
    store = Store(tmp_path / "singleton.sqlite")
    store.init_schema()
    for index in range(3):
        _insert_change_with_outcome(store, repo=f"repo-{index}", group_size=1, outcome="merged")

    compute_alignment(store)

    row = _single_alignment_row(store)
    assert row["all_singleton"] == 1


def test_compute_alignment_is_idempotent(tmp_path) -> None:
    """Replace alignment rows deterministically across repeated runs."""
    store = Store(tmp_path / "idempotent.sqlite")
    store.init_schema()
    for index, outcome in enumerate(["merged", "open", "closed-unmerged"]):
        _insert_change_with_outcome(store, repo=f"repo-{index}", outcome=outcome)

    compute_alignment(store)
    first_rows = _alignment_rows_for_comparison(store)
    compute_alignment(store)
    second_rows = _alignment_rows_for_comparison(store)

    assert second_rows == first_rows


def test_open_prs_are_excluded_from_alignment_decisions(tmp_path) -> None:
    """Exclude open PRs from the alignment decision distribution."""
    store = Store(tmp_path / "open.sqlite")
    store.init_schema()
    _insert_change_with_outcome(store, repo="repo-1", outcome="merged")
    _insert_change_with_outcome(store, repo="repo-2", outcome="closed-unmerged")
    _insert_change_with_outcome(store, repo="repo-3", outcome="open")

    summary = compute_alignment(store)

    row = _single_alignment_row(store)
    assert summary["open_change_rows_excluded"] == 1
    assert row["k_projects_all"] == 3
    assert row["k_projects_decided"] == 2
    assert row["k_changes"] == 2
    assert row["k_changes_total"] == 3
    assert row["n_open"] == 1
    expected = 1.0 - (-(0.5 * math.log(0.5) + 0.5 * math.log(0.5)) / math.log(2))
    assert math.isclose(row["alignment"], expected)


def test_fork_and_archived_rows_are_excluded_from_alignment(tmp_path) -> None:
    """Exclude forked and archived repositories from alignment."""
    store = Store(tmp_path / "forks.sqlite")
    store.init_schema()
    _insert_change_with_outcome(store, repo="repo-1", outcome="merged")
    _insert_change_with_outcome(store, repo="repo-2", outcome="merged")
    _insert_change_with_outcome(store, repo="forked", outcome="closed-unmerged", is_fork=1)
    _insert_change_with_outcome(store, repo="archived", outcome="closed-unmerged", is_archived=1)

    summary = compute_alignment(store)

    row = _single_alignment_row(store)
    assert summary["fork_or_archived_change_rows_excluded"] == 2
    assert summary["fork_or_archived_change_rows_excluded_per_ecosystem"]["npm"] == 2
    assert row["k_projects_all"] == 2
    assert row["k_projects_decided"] == 2
    assert row["n_merged"] == 2


def test_alignment_records_all_dependabot_source_mix(tmp_path) -> None:
    """Classify triples sourced exclusively from Dependabot."""
    store = Store(tmp_path / "dependabot.sqlite")
    store.init_schema()
    for index in range(3):
        _insert_change_with_outcome(
            store,
            repo=f"repo-{index}",
            outcome="merged",
            source="dependabot",
        )

    compute_alignment(store)

    row = _single_alignment_row(store)
    assert row["source_mix"] == "all_dependabot"
    assert row["n_dependabot"] == 3
    assert row["n_renovate"] == 0
    assert row["n_human"] == 0
    assert row["n_other_bot"] == 0


def test_alignment_records_mixed_bot_human_source_mix(tmp_path) -> None:
    """Classify triples with both bot and human sources."""
    store = Store(tmp_path / "mixed-source.sqlite")
    store.init_schema()
    _insert_change_with_outcome(store, repo="repo-1", outcome="merged", source="dependabot")
    _insert_change_with_outcome(store, repo="repo-2", outcome="merged", source="human")

    compute_alignment(store)

    row = _single_alignment_row(store)
    assert row["source_mix"] == "mixed_bot_human"
    assert row["n_dependabot"] == 1
    assert row["n_human"] == 1


def test_alignment_warns_and_uses_modal_semver_for_degenerate_mixed_tier(
    tmp_path,
    caplog,
) -> None:
    """Warn and use the modal semver tier for degenerate mixed triples."""
    store = Store(tmp_path / "mixed-semver.sqlite")
    store.init_schema()
    _insert_change_with_outcome(store, repo="repo-1", outcome="merged", semver_tier="major")
    _insert_change_with_outcome(store, repo="repo-2", outcome="merged", semver_tier="minor")
    _insert_change_with_outcome(store, repo="repo-3", outcome="merged", semver_tier="minor")

    with caplog.at_level(logging.WARNING, logger="deptracker.alignment"):
        compute_alignment(store)

    row = _single_alignment_row(store)
    assert row["semver_tier"] == "minor"
    assert "mixed semver tiers" in caplog.text


def _insert_change_with_outcome(
    store: Store,
    *,
    repo: str,
    outcome: str,
    pr_number: int = 1,
    event_created_at: str = "2026-05-01T00:00:00Z",
    group_size: int = 1,
    source: str = "dependabot",
    semver_tier: str = "minor",
    security: str = "non-security",
    is_fork: int = 0,
    is_archived: int = 0,
) -> None:
    """Insert one classified synthetic change for alignment tests."""
    store.insert_pr_batch(
        [
            {
                "repo_owner": "owner",
                "repo_name": repo,
                "pr_number": pr_number,
                "event_created_at": event_created_at,
                "actor_login": "actor",
                "pr_action": "opened",
            }
        ]
    )
    with store._connect() as conn:
        pr_id = conn.execute(
            """
            SELECT id FROM pr
            WHERE repo_owner = ? AND repo_name = ? AND pr_number = ?
            """,
            ("owner", repo, pr_number),
        ).fetchone()["id"]
        conn.execute(
            "UPDATE pr SET is_fork = ?, is_archived = ? WHERE id = ?",
            (is_fork, is_archived, pr_id),
        )

    store.insert_changes(
        pr_id,
        [
            {
                "ecosystem": "npm",
                "package": "left-pad",
                "from_version": "1.0.0",
                "to_version": "1.1.0",
                "manifest_path": "package.json",
                "is_lockfile": False,
            }
        ],
    )
    with store._connect() as conn:
        change_id = conn.execute("SELECT MAX(id) AS id FROM change").fetchone()["id"]
        conn.execute("UPDATE change SET group_size = ? WHERE id = ?", (group_size, change_id))

    store.insert_classifications(
        [
            _classification(change_id, "outcome", outcome),
            _classification(change_id, "source", source),
            _classification(change_id, "semver_tier", semver_tier),
            _classification(change_id, "security", security),
        ]
    )


def _classification(change_id: int, dimension: str, label: str) -> dict:
    """Build one synthetic classification row."""
    return {
        "change_id": change_id,
        "dimension": dimension,
        "label": label,
        "classifier_version": 1,
        "classified_at": "2026-05-01T00:00:00Z",
    }


def _single_alignment_row(store: Store) -> dict:
    """Return the sole persisted alignment row in a fixture database."""
    rows = _alignment_rows_for_comparison(store, include_computed_at=True)
    assert len(rows) == 1
    return rows[0]


def _alignment_rows_for_comparison(
    store: Store,
    *,
    include_computed_at: bool = False,
) -> list[dict]:
    """Return stable alignment rows for idempotency comparisons."""
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM triple_alignment
            ORDER BY ecosystem, package, from_version, to_version
            """
        ).fetchall()

    result = []
    for row in rows:
        row_dict = dict(row)
        row_dict.pop("id", None)
        if not include_computed_at:
            row_dict.pop("computed_at", None)
        result.append(row_dict)
    return result
