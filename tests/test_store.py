from deptracker.store import Store


OLD_PR_SCHEMA = """
CREATE TABLE pr (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_owner TEXT NOT NULL,
  repo_name TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  event_created_at TEXT NOT NULL,
  actor_login TEXT,
  pr_action TEXT,
  pr_created_at TEXT,
  title TEXT,
  body TEXT,
  author_login TEXT,
  state TEXT,
  merged INTEGER,
  merged_at TEXT,
  closed_at TEXT,
  additions INTEGER,
  deletions INTEGER,
  base_sha TEXT,
  head_sha TEXT,
  is_cross_repository INTEGER,
  is_fork INTEGER,
  is_archived INTEGER,
  files_json TEXT,
  passes_manifest_only_filter INTEGER,
  enriched_at TEXT,
  UNIQUE(repo_owner, repo_name, pr_number)
);
"""


def test_init_schema_migrates_existing_database_idempotently(tmp_path) -> None:
    """Migrate an old database schema safely across repeated initialization."""
    db_path = tmp_path / "old.sqlite"
    store = Store(db_path)
    with store._connect() as conn:
        conn.executescript(
            OLD_PR_SCHEMA
            + """
            CREATE TABLE change (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pr_id INTEGER NOT NULL REFERENCES pr(id),
              ecosystem TEXT NOT NULL,
              package TEXT NOT NULL,
              from_version TEXT,
              to_version TEXT NOT NULL,
              manifest_path TEXT NOT NULL,
              is_lockfile INTEGER NOT NULL
            );
            """
        )

    store.init_schema()
    store.init_schema()

    with store._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(pr)").fetchall()}
        change_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(change)").fetchall()
        }
        error_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'pipeline_error'"
        ).fetchone()
        classification_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'classification'"
        ).fetchone()
        closing_comment_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'pr_closing_comment'"
        ).fetchone()

    assert {
        "files_has_next_page",
        "file_count",
        "manifest_file_count",
        "non_manifest_file_count",
        "manifest_filter_reason",
        "parse_priority",
        "parse_attempted_at",
        "root_commit_sha",
        "commit_messages_concat",
        "commits_truncated",
        "outcome_titlebody",
        "superseded_reason",
        "closing_comments_fetched_at",
    }.issubset(columns)
    assert "group_size" in change_columns
    assert error_table is not None
    assert classification_table is not None
    assert closing_comment_table is not None


def test_pipeline_error_counts_by_stage(tmp_path) -> None:
    """Aggregate persisted pipeline errors by stage and type."""
    store = Store(tmp_path / "errors.sqlite")
    store.init_schema()

    store.insert_pipeline_error(stage="enrich", error_type="NOT_FOUND", message="missing")
    store.insert_pipeline_error(stage="enrich", error_type="NOT_FOUND", message="missing")
    store.insert_pipeline_error(stage="parse", error_type="JSONDecodeError", message="bad")

    assert store.error_counts_by_stage() == {
        "enrich": {"NOT_FOUND": 2},
        "parse": {"JSONDecodeError": 1},
    }


def test_pr_counts_separate_successful_enrichment_from_errors(tmp_path) -> None:
    """Separate successful enrichment rows from error attempts in stats."""
    store = Store(tmp_path / "counts.sqlite")
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
            },
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 2,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "opened",
            },
            {
                "repo_owner": "owner",
                "repo_name": "repo",
                "pr_number": 3,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "opened",
            },
        ]
    )
    store.update_pr_enrichment(
        1,
        {
            "title": "pass",
            "passes_manifest_only_filter": 1,
            "manifest_filter_reason": "passed",
            "enriched_at": "2026-05-01T00:00:00Z",
        },
    )
    store.update_pr_enrichment(
        2,
        {
            "title": "fail",
            "passes_manifest_only_filter": 0,
            "manifest_filter_reason": "no_manifest_files",
            "enriched_at": "2026-05-01T00:00:00Z",
        },
    )
    store.update_pr_enrichment(
        3,
        {
            "passes_manifest_only_filter": 0,
            "manifest_filter_reason": "graphql_not_found",
            "enriched_at": "2026-05-01T00:00:00Z",
        },
    )

    counts = store.pr_counts_by_stage()

    assert counts["enriched_prs"] == 2
    assert counts["enrichment_attempted_prs"] == 3
    assert counts["passed_manifest_only_filter"] == 1
    assert counts["failed_manifest_only_filter"] == 1
    assert counts["enrichment_error_prs"] == 1


def test_insert_classifications_is_idempotent(tmp_path) -> None:
    """Ignore duplicate classification rows for the same version."""
    store = Store(tmp_path / "classification.sqlite")
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
    store.insert_changes(
        1,
        [
            {
                "ecosystem": "npm",
                "package": "react",
                "from_version": "18.2.0",
                "to_version": "18.3.0",
                "manifest_path": "package.json",
                "is_lockfile": False,
            }
        ],
    )

    rows = [
        {
            "change_id": 1,
            "dimension": "source",
            "label": "human",
            "classifier_version": 1,
            "classified_at": "2026-05-01T00:00:00Z",
        }
    ]

    assert store.insert_classifications(rows) == 1
    assert store.insert_classifications(rows) == 0


def test_insert_changes_sets_group_size_for_grouped_pr(tmp_path) -> None:
    """Store the sibling count for grouped dependency updates."""
    store = Store(tmp_path / "grouped.sqlite")
    store.init_schema()
    _insert_minimal_pr(store, pr_number=1)

    store.insert_changes(
        1,
        [
            _change("react", "18.2.0", "18.3.0"),
            _change("vite", "5.0.0", "5.1.0"),
            _change("eslint", "8.1.0", "8.2.0"),
        ],
    )

    with store._connect() as conn:
        values = [
            row["group_size"]
            for row in conn.execute("SELECT group_size FROM change ORDER BY id").fetchall()
        ]

    assert values == [3, 3, 3]


def test_insert_changes_sets_group_size_for_singleton_pr(tmp_path) -> None:
    """Store a group size of one for singleton dependency updates."""
    store = Store(tmp_path / "singleton.sqlite")
    store.init_schema()
    _insert_minimal_pr(store, pr_number=1)

    store.insert_changes(1, [_change("react", "18.2.0", "18.3.0")])

    with store._connect() as conn:
        value = conn.execute("SELECT group_size FROM change").fetchone()["group_size"]

    assert value == 1


def _insert_minimal_pr(store: Store, pr_number: int) -> None:
    """Insert one minimal discovered PR."""
    store.insert_pr_batch(
        [
            {
                "repo_owner": "owner",
                "repo_name": f"repo-{pr_number}",
                "pr_number": pr_number,
                "event_created_at": "2026-05-01T00:00:00Z",
                "actor_login": "actor",
                "pr_action": "opened",
            }
        ]
    )


def _change(package: str, from_version: str, to_version: str) -> dict:
    """Build one minimal npm change row."""
    return {
        "ecosystem": "npm",
        "package": package,
        "from_version": from_version,
        "to_version": to_version,
        "manifest_path": "package.json",
        "is_lockfile": False,
    }
