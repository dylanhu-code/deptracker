import pytest

from deptracker.gold import (
    GoldValidationError,
    append_jsonl_row,
    human_verified_pr_ids,
    labelled_pr_ids,
    make_gold_row,
    read_jsonl,
)


def test_gold_jsonl_validation_and_round_trip(tmp_path) -> None:
    """Append and read a valid mixed-level gold-set row."""
    gold_path = tmp_path / "gold.jsonl"
    row = _gold_row(label_status="human_verified", label_source="manual", reviewed=True)

    append_jsonl_row(gold_path, row)

    assert read_jsonl(gold_path)[0]["pr_id"] == 1
    assert labelled_pr_ids(gold_path) == {1}
    assert human_verified_pr_ids(gold_path) == {1}


def test_gold_jsonl_rejects_invalid_mixed_level_label(tmp_path) -> None:
    """Reject unsupported labels in mixed-level gold rows."""
    row = _gold_row(label_status="human_verified", label_source="manual", reviewed=True)
    row["change_labels"][0]["semver_tier"] = "not-a-tier"

    with pytest.raises(GoldValidationError):
        append_jsonl_row(tmp_path / "gold.jsonl", row)


def test_gold_jsonl_accepts_sampled_change_label_subset(tmp_path) -> None:
    """Accept gold rows that intentionally label a subset of changes."""
    row = make_gold_row(
        pr_id=1,
        pr_url="https://github.com/owner/repo/pull/1",
        repo="owner/repo",
        pr_number=1,
        change_ids=[10, 11],
        ecosystem_summary=["npm"],
        group_size=2,
        pr_labels={
            "source": "dependabot",
            "outcome": "merged",
            "security": "non-security",
        },
        change_labels=[
            {
                "change_id": 10,
                "package": "react",
                "from_version": "18.2.0",
                "to_version": "18.3.0",
                "manifest_path": "package.json",
                "semver_tier": "minor",
                "direct_or_transitive": "direct",
            }
        ],
        unlabelled_change_ids=[11],
        change_label_scope="sampled_changes",
        heuristic_pr_labels={
            "source": "dependabot",
            "outcome": "merged",
            "security": "non-security",
        },
        heuristic_change_labels=[
            {
                "change_id": 10,
                "semver_tier": "minor",
                "direct_or_transitive": "direct",
            },
            {
                "change_id": 11,
                "semver_tier": "patch",
                "direct_or_transitive": "transitive",
            },
        ],
        label_status="human_verified",
        label_source="manual",
        reviewed_by_human=True,
        confidence=None,
    )

    append_jsonl_row(tmp_path / "gold.jsonl", row)

    assert read_jsonl(tmp_path / "gold.jsonl")[0]["change_label_scope"] == "sampled_changes"


def test_gold_jsonl_rejects_incorrect_unlabelled_partition() -> None:
    """Reject sampled gold rows whose labelled partition is incomplete."""
    row = _gold_row(label_status="human_verified", label_source="manual", reviewed=True)
    row["change_ids"] = [10, 11]
    row["unlabelled_change_ids"] = []
    row["change_label_scope"] = "sampled_changes"

    with pytest.raises(GoldValidationError, match="partition"):
        append_jsonl_row("ignored.jsonl", row)


def _gold_row(label_status: str, label_source: str, reviewed: bool) -> dict:
    """Build one valid mixed-level gold-set row."""
    return make_gold_row(
        pr_id=1,
        pr_url="https://github.com/owner/repo/pull/1",
        repo="owner/repo",
        pr_number=1,
        change_ids=[10],
        ecosystem_summary=["npm"],
        group_size=1,
        pr_labels={
            "source": "dependabot",
            "outcome": "merged",
            "security": "non-security",
        },
        change_labels=[
            {
                "change_id": 10,
                "package": "react",
                "from_version": "18.2.0",
                "to_version": "18.3.0",
                "manifest_path": "package.json",
                "semver_tier": "minor",
                "direct_or_transitive": "direct",
            }
        ],
        heuristic_pr_labels={
            "source": "dependabot",
            "outcome": "merged",
            "security": "non-security",
        },
        heuristic_change_labels=[
            {
                "change_id": 10,
                "semver_tier": "minor",
                "direct_or_transitive": "direct",
            }
        ],
        label_status=label_status,
        label_source=label_source,
        reviewed_by_human=reviewed,
        confidence=None,
    )
