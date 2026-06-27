from pathlib import Path

import deptracker.parse as parse_module
from deptracker.parse import parse
from deptracker.store import Store


def test_parse_marks_attempted_and_inserts_changes(monkeypatch, tmp_path) -> None:
    """Mark parse attempts and persist extracted dependency changes."""
    store = Store(tmp_path / "parse.sqlite")
    store.init_schema()
    _insert_parse_ready_pr(store, pr_number=1)
    diff_text = Path("tests/adapters/fixtures/npm_simple_bump.diff").read_text()

    monkeypatch.setattr(parse_module.httpx, "Client", _fake_client_factory(diff_text))

    result = parse(store=store, token="")

    with store._connect() as conn:
        pr = conn.execute("SELECT parse_attempted_at FROM pr WHERE id = 1").fetchone()
        changes = conn.execute("SELECT COUNT(*) AS count FROM change").fetchone()

    assert result["parsed"] == 1
    assert result["changes_inserted"] == 1
    assert pr["parse_attempted_at"] is not None
    assert changes["count"] == 1


def test_parse_retry_errors_removes_error_on_success(monkeypatch, tmp_path) -> None:
    """Remove a persisted parse error after a successful retry."""
    store = Store(tmp_path / "retry.sqlite")
    store.init_schema()
    _insert_parse_ready_pr(store, pr_number=1)
    store.insert_pipeline_error(
        stage="parse",
        repo_owner="owner",
        repo_name="repo",
        pr_number=1,
        error_type="HTTPStatusError",
        message="rate limited",
    )
    diff_text = Path("tests/adapters/fixtures/npm_simple_bump.diff").read_text()

    monkeypatch.setattr(parse_module.httpx, "Client", _fake_client_factory(diff_text))

    result = parse(store=store, token="", retry_errors=True)

    with store._connect() as conn:
        error_count = conn.execute("SELECT COUNT(*) AS count FROM pipeline_error").fetchone()
        change_count = conn.execute("SELECT COUNT(*) AS count FROM change").fetchone()

    assert result["parsed"] == 1
    assert result["changes_inserted"] == 1
    assert result["parse_errors_removed"] == 1
    assert error_count["count"] == 0
    assert change_count["count"] == 1


def _insert_parse_ready_pr(store: Store, pr_number: int) -> None:
    """Insert one enriched PR ready for offline diff parsing."""
    store.insert_pr_batch(
        [
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": pr_number,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "opened",
            }
        ]
    )
    store.update_pr_enrichment(
        1,
        {
            "title": "Bump react",
            "files_json": [{"path": "package.json", "additions": 1, "deletions": 1}],
            "passes_manifest_only_filter": 1,
            "enriched_at": "2026-05-01T00:00:00Z",
        },
    )


def _fake_client_factory(diff_text: str):
    """Build a context-managed HTTP client that serves a fixture diff."""
    class FakeResponse:
        headers = {"x-ratelimit-remaining": "5000", "x-ratelimit-reset": "0"}
        text = diff_text
        status_code = 200

        def raise_for_status(self) -> None:
            """Simulate a successful HTTP response."""
            return None

    class FakeClient:
        def __init__(self, timeout: int, follow_redirects: bool) -> None:
            """Record client construction arguments for compatibility."""
            self.timeout = timeout
            self.follow_redirects = follow_redirects

        def __enter__(self):
            """Return the fake client as a context manager."""
            return self

        def __exit__(self, *args) -> None:
            """Exit the fake context manager without suppression."""
            return None

        def get(self, *args, **kwargs) -> FakeResponse:
            """Return the configured diff response."""
            return FakeResponse()

    return FakeClient
