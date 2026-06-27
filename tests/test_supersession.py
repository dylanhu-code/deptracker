"""Tests for bot-comment supersession and workflow closure detection."""

from deptracker.classify import classify_outcome
from deptracker.supersession import COMMENT_PATTERNS
from deptracker.supersession import detect_supersession_from_comments


def test_each_supersession_comment_pattern_matches_positive_bot_comment() -> None:
    """Match every configured positive pattern on an appropriate bot comment."""
    examples = {
        "dependabot_superseded_by_pr": "Superseded by #123.",
        "dependabot_updatable_elsewhere": "Looks like these dependencies are updatable in another way.",
        "dependabot_no_longer_needed": "This is no longer needed.",
        "dependabot_up_to_date_now": "This dependency is up-to-date now.",
        "renovate_superseded_by_pr": "Superseded by #456.",
        "renovate_autoclosing": "Autoclosing this PR because it is obsolete.",
        "renovate_superseded_generic": "This update has been superseded.",
        "renovate_closure_generic": "Renovate will close this obsolete PR.",
        "generic_superseded_by_pr": "Superseded by #789.",
        "generic_grouped_or_obsolete": "These dependencies are updatable in another way.",
    }
    authors = {
        "dependabot": ("dependabot[bot]", "Bot"),
        "renovate": ("renovate[bot]", "Bot"),
        "other-bot": ("github-actions[bot]", "Bot"),
    }

    for pattern in COMMENT_PATTERNS:
        login, author_type = authors[pattern.bot_family]
        match = detect_supersession_from_comments(
            [
                {
                    "author_login": login,
                    "author_type": author_type,
                    "body": examples[pattern.name],
                    "created_at": "2026-05-01T00:00:00Z",
                }
            ]
        )

        assert match is not None, pattern.name
        assert match.reason == pattern.reason
        assert match.bot_family == pattern.bot_family
        assert match.pattern_name == pattern.name


def test_wont_notify_comment_is_not_supersession() -> None:
    """Keep Dependabot manual-dismissal comments as closed-unmerged."""
    match = detect_supersession_from_comments(
        [
            {
                "author_login": "dependabot[bot]",
                "author_type": "Bot",
                "body": "OK, I won't notify you again about this release.",
                "created_at": "2026-05-01T00:00:00Z",
            }
        ]
    )

    assert match is None


def test_human_superseded_comment_is_not_supersession() -> None:
    """Ignore supersession-looking text when authored by a human."""
    match = detect_supersession_from_comments(
        [
            {
                "author_login": "maintainer",
                "author_type": "User",
                "body": "Superseded by #123.",
                "created_at": "2026-05-01T00:00:00Z",
            }
        ]
    )

    assert match is None


def test_no_comment_is_not_supersession() -> None:
    """Keep PRs without cached comments as closed-unmerged."""
    assert detect_supersession_from_comments([]) is None
    assert classify_outcome("CLOSED", 0, "2026-05-01", None, "Update", "", "[]") == (
        "closed-unmerged"
    )


def test_classify_outcome_uses_bot_comment_supersession() -> None:
    """Upgrade closed-unmerged PRs to superseded on positive bot comments."""
    comments = (
        '[{"author_login":"dependabot[bot]","author_type":"Bot",'
        '"body":"Superseded by #123.","created_at":"2026-05-01T00:00:00Z"}]'
    )

    assert classify_outcome("CLOSED", 0, "2026-05-01", None, "Update", "", comments) == (
        "superseded"
    )
