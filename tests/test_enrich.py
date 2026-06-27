import json

import deptracker.enrich as enrich_module
from deptracker.enrich import _enrichment_fields, _parse_priority_for_files
from deptracker.enrich import enrich
from deptracker.store import Store


def test_enrichment_fields_include_manifest_filter_reason() -> None:
    """Record why a PR with mixed manifest and source files is rejected."""
    fields = _enrichment_fields(
        {
            "files": {
                "nodes": [
                    {"path": "package.json", "additions": 1, "deletions": 1},
                    {"path": "src/index.js", "additions": 1, "deletions": 1},
                ],
                "pageInfo": {"hasNextPage": False},
            },
            "repository": {},
            "author": {},
        }
    )

    assert fields["files_has_next_page"] == 0
    assert fields["file_count"] == 2
    assert fields["manifest_file_count"] == 1
    assert fields["non_manifest_file_count"] == 1
    assert fields["manifest_filter_reason"] == "mixed_manifest_and_non_manifest_files"
    assert fields["passes_manifest_only_filter"] == 0


def test_enrichment_fields_mark_file_cap() -> None:
    """Reject file lists that exceed the configured pagination cap."""
    fields = _enrichment_fields(
        {
            "files": {
                "nodes": [{"path": "package.json", "additions": 1, "deletions": 1}],
                "pageInfo": {"hasNextPage": True, "capExceeded": True},
            },
            "repository": {},
            "author": {},
        }
    )

    assert fields["files_has_next_page"] == 1
    assert fields["manifest_filter_reason"] == "too_many_files_cap"
    assert fields["passes_manifest_only_filter"] == 0


def test_enrichment_fields_capture_commit_messages() -> None:
    """Persist commit-message text and its truncation flag."""
    fields = _enrichment_fields(
        {
            "files": {
                "nodes": [{"path": "package.json", "additions": 1, "deletions": 1}],
                "pageInfo": {"hasNextPage": False},
            },
            "repository": {"object": {"oid": "root"}},
            "author": {},
            "commits": {
                "nodes": [
                    {"commit": {"message": "first"}},
                    {"commit": {"message": "second"}},
                ],
                "pageInfo": {"hasNextPage": True},
            },
        }
    )

    assert fields["root_commit_sha"] == "root"
    assert fields["commit_messages_concat"] == "first\n---\nsecond"
    assert fields["commits_truncated"] == 1


def test_enrichment_fields_capture_author_type() -> None:
    """Persist GraphQL author type for source classification."""
    fields = _enrichment_fields(
        {
            "author": {"login": "automation-service", "__typename": "Bot"},
            "files": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            "repository": {},
            "commits": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        }
    )

    assert fields["author_login"] == "automation-service"
    assert fields["author_type"] == "Bot"


def test_priority_ecosystem_parse_priority_is_persisted(tmp_path) -> None:
    """Assign parse priorities while preserving valid deferred PRs."""
    store = Store(tmp_path / "priority.sqlite")
    store.init_schema()
    rows = [
        ("owner", "maven-repo", 1, [{"path": "pom.xml"}], 1),
        ("owner", "pip-repo", 2, [{"path": "services/api/requirements-dev.txt"}], 1),
        ("owner", "npm-repo", 3, [{"path": "frontend/package.json"}], 1),
        ("owner", "mixed-repo", 4, [{"path": "package.json"}, {"path": "src/app.js"}], 0),
    ]
    store.insert_pr_batch(
        [
            {
                "repo_owner": owner,
                "repo_name": repo,
                "pr_number": number,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "opened",
            }
            for owner, repo, number, _files, _passes_filter in rows
        ]
    )

    priority_ecosystems = {"maven", "pip"}
    with store._connect() as conn:
        pr_ids = {
            row["pr_number"]: row["id"]
            for row in conn.execute("SELECT id, pr_number FROM pr").fetchall()
        }
    for _owner, _repo, number, files, passes_filter in rows:
        store.update_pr_enrichment(
            pr_ids[number],
            {
                "title": "Update dependency",
                "files_json": files,
                "passes_manifest_only_filter": passes_filter,
                "parse_priority": _parse_priority_for_files(
                    files,
                    passes_filter=bool(passes_filter),
                    priority_ecosystems=priority_ecosystems,
                ),
                "enriched_at": "2026-05-01T00:00:00Z",
            },
        )

    with store._connect() as conn:
        priorities = {
            row["pr_number"]: row["parse_priority"]
            for row in conn.execute(
                "SELECT pr_number, parse_priority FROM pr ORDER BY pr_number"
            ).fetchall()
        }
    default_parse_numbers = [
        row["pr_number"] for row in store.iter_prs_needing_parsing()
    ]
    include_deferred_numbers = [
        row["pr_number"] for row in store.iter_prs_needing_parsing(include_deferred=True)
    ]

    assert priorities == {
        1: "priority",
        2: "priority",
        3: "deferred_non_priority",
        4: None,
    }
    assert default_parse_numbers == [1, 2]
    assert include_deferred_numbers == [1, 2, 3]


def test_enrich_accepts_stop_remaining(monkeypatch, tmp_path) -> None:
    """Stop enrichment cleanly below the configured API budget."""
    store = Store(tmp_path / "enrich.sqlite")
    store.init_schema()
    store.insert_pr_batch(
        [
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 1,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "opened",
            }
        ]
    )

    class FakeResponse:
        headers = {"x-ratelimit-remaining": "150"}

        def raise_for_status(self) -> None:
            """Simulate a successful GraphQL HTTP response."""
            return None

        def json(self) -> dict:
            """Return one synthetic GraphQL PR payload."""
            return {
                "data": {
                    "pr_1_0": {
                        "pullRequest": {
                            "createdAt": "2026-05-01T00:00:00Z",
                            "title": "Update dependency",
                            "body": "",
                            "author": {"login": "actor"},
                            "merged": False,
                            "state": "OPEN",
                            "closedAt": None,
                            "mergedAt": None,
                            "additions": 1,
                            "deletions": 1,
                            "baseRefOid": "base",
                            "headRefOid": "head",
                            "isCrossRepository": False,
                            "repository": {"isFork": False, "isArchived": False},
                            "files": {
                                "nodes": [
                                    {"path": "package.json", "additions": 1, "deletions": 1}
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            },
                        }
                    }
                }
            }

    class FakeClient:
        def __init__(self, timeout: int) -> None:
            """Record the requested HTTP timeout."""
            self.timeout = timeout

        def __enter__(self):
            """Return the fake client as a context manager."""
            return self

        def __exit__(self, *args) -> None:
            """Exit the fake context manager without suppression."""
            return None

        def post(self, *args, **kwargs) -> FakeResponse:
            """Return a synthetic enrichment response."""
            return FakeResponse()

    monkeypatch.setattr(enrich_module.httpx, "Client", FakeClient)

    result = enrich(store=store, token="token", stop_remaining=200)

    assert result["enriched"] == 1
    assert result["passes_filter"] == 1
    assert result["stopped_for_rate_limit"] is True
    assert result["rate_limit_remaining"] == 150


def test_enrich_paginates_changed_files(monkeypatch, tmp_path) -> None:
    """Fetch subsequent GraphQL file pages before filtering a PR."""
    store = Store(tmp_path / "paginate.sqlite")
    store.init_schema()
    store.insert_pr_batch(
        [
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 1,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "opened",
            }
        ]
    )

    class FakeResponse:
        headers = {"x-ratelimit-remaining": "5000"}

        def __init__(self, payload: dict) -> None:
            """Store a synthetic GraphQL payload."""
            self.payload = payload

        def raise_for_status(self) -> None:
            """Simulate a successful GraphQL HTTP response."""
            return None

        def json(self) -> dict:
            """Return the stored synthetic GraphQL payload."""
            return self.payload

    initial_payload = {
        "data": {
            "pr_1_0": {
                "pullRequest": {
                    "createdAt": "2026-05-01T00:00:00Z",
                    "title": "Update dependency",
                    "body": "",
                    "author": {"login": "actor"},
                    "merged": False,
                    "state": "OPEN",
                    "closedAt": None,
                    "mergedAt": None,
                    "additions": 2,
                    "deletions": 2,
                    "baseRefOid": "base",
                    "headRefOid": "head",
                    "isCrossRepository": False,
                    "repository": {"isFork": False, "isArchived": False},
                    "files": {
                        "nodes": [{"path": "package.json", "additions": 1, "deletions": 1}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                    },
                }
            }
        }
    }
    next_payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "files": {
                        "nodes": [
                            {"path": "frontend/package.json", "additions": 1, "deletions": 1}
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
    }

    class FakeClient:
        def __init__(self, timeout: int) -> None:
            """Queue initial and paginated GraphQL payloads."""
            self.responses = [FakeResponse(initial_payload), FakeResponse(next_payload)]

        def __enter__(self):
            """Return the fake client as a context manager."""
            return self

        def __exit__(self, *args) -> None:
            """Exit the fake context manager without suppression."""
            return None

        def post(self, *args, **kwargs) -> FakeResponse:
            """Return the next queued GraphQL response."""
            return self.responses.pop(0)

    monkeypatch.setattr(enrich_module.httpx, "Client", FakeClient)

    result = enrich(store=store, token="token")
    row = next(store.iter_prs_needing_parsing())
    files = json.loads(row["files_json"])

    assert result["enriched"] == 1
    assert result["passes_filter"] == 1
    assert row["file_count"] == 2
    assert row["files_has_next_page"] == 0
    assert [file["path"] for file in files] == ["package.json", "frontend/package.json"]
