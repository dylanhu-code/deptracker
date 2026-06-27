"""SQLite storage for discovered pull requests and parsed dependency changes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deptracker.adapters.base import Change


DISCOVERY_COLUMNS = (
    "repo_owner",
    "repo_name",
    "pr_number",
    "event_created_at",
    "actor_login",
    "pr_action",
)

ENRICHMENT_COLUMNS = {
    "pr_created_at",
    "title",
    "body",
    "author_login",
    "author_type",
    "state",
    "merged",
    "merged_at",
    "closed_at",
    "additions",
    "deletions",
    "base_sha",
    "head_sha",
    "is_cross_repository",
    "is_fork",
    "is_archived",
    "root_commit_sha",
    "commit_messages_concat",
    "commits_truncated",
    "files_json",
    "passes_manifest_only_filter",
    "files_has_next_page",
    "file_count",
    "manifest_file_count",
    "non_manifest_file_count",
    "manifest_filter_reason",
    "parse_priority",
    "enriched_at",
}

PR_DIAGNOSTIC_COLUMNS = {
    "files_has_next_page": "INTEGER",
    "file_count": "INTEGER",
    "manifest_file_count": "INTEGER",
    "non_manifest_file_count": "INTEGER",
    "manifest_filter_reason": "TEXT",
    "parse_priority": "TEXT",
    "parse_attempted_at": "TEXT",
    "root_commit_sha": "TEXT",
    "commit_messages_concat": "TEXT",
    "commits_truncated": "INTEGER NOT NULL DEFAULT 0",
    "author_type": "TEXT",
    "outcome_titlebody": "TEXT",
    "superseded_reason": "TEXT",
    "closing_comments_fetched_at": "TEXT",
}

CHANGE_DIAGNOSTIC_COLUMNS = {
    "group_size": "INTEGER",
}

TRIPLE_ALIGNMENT_COLUMNS = {
    "k_projects_decided": "INTEGER NOT NULL DEFAULT 0",
    "k_changes_total": "INTEGER NOT NULL DEFAULT 0",
    "semver_tier": "TEXT NOT NULL DEFAULT 'unknown'",
    "is_security_any": "INTEGER NOT NULL DEFAULT 0",
    "is_security_all": "INTEGER NOT NULL DEFAULT 0",
    "n_dependabot": "INTEGER NOT NULL DEFAULT 0",
    "n_renovate": "INTEGER NOT NULL DEFAULT 0",
    "n_human": "INTEGER NOT NULL DEFAULT 0",
    "n_other_bot": "INTEGER NOT NULL DEFAULT 0",
    "source_mix": "TEXT NOT NULL DEFAULT 'mixed_other'",
    "alignment_v1": "REAL",
    "k_projects_decided_v1": "INTEGER",
    "k_changes_v1": "INTEGER",
    "k_changes_total_v1": "INTEGER",
    "n_merged_v1": "INTEGER",
    "n_closed_unmerged_v1": "INTEGER",
    "n_open_v1": "INTEGER",
    "n_superseded_v1": "INTEGER",
    "alignment_pcb": "REAL",
    "k_projects_decided_pcb": "INTEGER",
    "k_changes_pcb": "INTEGER",
    "k_changes_total_pcb": "INTEGER",
    "n_merged_pcb": "INTEGER",
    "n_closed_unmerged_pcb": "INTEGER",
    "n_open_pcb": "INTEGER",
    "n_superseded_pcb": "INTEGER",
}

TRIPLE_ALIGNMENT_V1_COLUMNS = (
    "alignment_v1",
    "k_projects_decided_v1",
    "k_changes_v1",
    "k_changes_total_v1",
    "n_merged_v1",
    "n_closed_unmerged_v1",
    "n_open_v1",
    "n_superseded_v1",
)

TRIPLE_ALIGNMENT_PCB_COLUMNS = (
    "alignment_pcb",
    "k_projects_decided_pcb",
    "k_changes_pcb",
    "k_changes_total_pcb",
    "n_merged_pcb",
    "n_closed_unmerged_pcb",
    "n_open_pcb",
    "n_superseded_pcb",
)


class Store:
    """SQLite-backed persistence for discovered, enriched, and classified PR data."""

    def __init__(self, path: str | Path) -> None:
        """Create a store bound to a SQLite file path."""
        self.path = Path(path)

    def init_schema(self) -> None:
        """Create tables and apply any schema migrations."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pr (
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
                  author_type TEXT,
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
                  root_commit_sha TEXT,
                  commit_messages_concat TEXT,
                  commits_truncated INTEGER NOT NULL DEFAULT 0,
                  files_json TEXT,
                  passes_manifest_only_filter INTEGER,
                  files_has_next_page INTEGER,
                  file_count INTEGER,
                  manifest_file_count INTEGER,
                  non_manifest_file_count INTEGER,
                  manifest_filter_reason TEXT,
                  parse_priority TEXT,
                  parse_attempted_at TEXT,
                  outcome_titlebody TEXT,
                  superseded_reason TEXT,
                  closing_comments_fetched_at TEXT,
                  enriched_at TEXT,

                  UNIQUE(repo_owner, repo_name, pr_number)
                );

                CREATE TABLE IF NOT EXISTS change (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  pr_id INTEGER NOT NULL REFERENCES pr(id),
                  ecosystem TEXT NOT NULL,
                  package TEXT NOT NULL,
                  from_version TEXT,
                  to_version TEXT NOT NULL,
                  manifest_path TEXT NOT NULL,
                  is_lockfile INTEGER NOT NULL,
                  group_size INTEGER
                );

                CREATE TABLE IF NOT EXISTS pipeline_error (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  stage TEXT NOT NULL,
                  repo_owner TEXT,
                  repo_name TEXT,
                  pr_number INTEGER,
                  error_type TEXT,
                  message TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS classification (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  change_id INTEGER NOT NULL REFERENCES change(id),
                  dimension TEXT NOT NULL,
                  label TEXT NOT NULL,
                  classifier_version INTEGER NOT NULL,
                  classified_at TEXT NOT NULL,
                  UNIQUE(change_id, dimension, classifier_version)
                );

                CREATE TABLE IF NOT EXISTS triple_alignment (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ecosystem TEXT NOT NULL,
                  package TEXT NOT NULL,
                  from_version TEXT,
                  to_version TEXT NOT NULL,
                  semver_tier TEXT NOT NULL,
                  k_projects_all INTEGER NOT NULL,
                  k_projects_decided INTEGER NOT NULL,
                  k_changes INTEGER NOT NULL,
                  k_changes_total INTEGER NOT NULL,
                  n_merged INTEGER NOT NULL,
                  n_closed_unmerged INTEGER NOT NULL,
                  n_open INTEGER NOT NULL,
                  n_superseded INTEGER NOT NULL,
                  is_security_any INTEGER NOT NULL,
                  is_security_all INTEGER NOT NULL,
                  n_dependabot INTEGER NOT NULL,
                  n_renovate INTEGER NOT NULL,
                  n_human INTEGER NOT NULL,
                  n_other_bot INTEGER NOT NULL,
                  source_mix TEXT NOT NULL,
                  alignment REAL NOT NULL,
                  mean_group_size REAL NOT NULL,
                  median_group_size REAL NOT NULL,
                  max_group_size INTEGER NOT NULL,
                  all_singleton INTEGER NOT NULL,
                  alignment_v1 REAL,
                  k_projects_decided_v1 INTEGER,
                  k_changes_v1 INTEGER,
                  k_changes_total_v1 INTEGER,
                  n_merged_v1 INTEGER,
                  n_closed_unmerged_v1 INTEGER,
                  n_open_v1 INTEGER,
                  n_superseded_v1 INTEGER,
                  alignment_pcb REAL,
                  k_projects_decided_pcb INTEGER,
                  k_changes_pcb INTEGER,
                  k_changes_total_pcb INTEGER,
                  n_merged_pcb INTEGER,
                  n_closed_unmerged_pcb INTEGER,
                  n_open_pcb INTEGER,
                  n_superseded_pcb INTEGER,
                  computed_at TEXT NOT NULL,
                  UNIQUE(ecosystem, package, from_version, to_version)
                );

                CREATE TABLE IF NOT EXISTS pr_closing_comment (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  pr_id INTEGER NOT NULL REFERENCES pr(id) ON DELETE CASCADE,
                  author_login TEXT,
                  author_type TEXT,
                  body TEXT NOT NULL,
                  created_at TEXT,
                  fetched_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_change_triple
                  ON change(package, from_version, to_version);
                CREATE INDEX IF NOT EXISTS idx_change_pr
                  ON change(pr_id);
                CREATE INDEX IF NOT EXISTS idx_classification_dimension_label_change
                  ON classification(dimension, classifier_version, label, change_id);
                CREATE INDEX IF NOT EXISTS idx_pr_repo
                  ON pr(repo_owner, repo_name);
                CREATE INDEX IF NOT EXISTS idx_pr_enrichment
                  ON pr(title, passes_manifest_only_filter);
                CREATE INDEX IF NOT EXISTS idx_triple_alignment_k
                  ON triple_alignment(k_projects_decided);
                CREATE INDEX IF NOT EXISTS idx_triple_alignment_eco
                  ON triple_alignment(ecosystem);
                CREATE INDEX IF NOT EXISTS idx_pr_closing_comment_pr
                  ON pr_closing_comment(pr_id);
                """
            )
            self._migrate_schema(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pr_outcome_comments
                  ON pr(outcome_titlebody, closing_comments_fetched_at, event_created_at, id)
                """
            )

    def insert_pr_batch(self, rows: list[dict]) -> int:
        """Insert discovered PR rows, ignoring duplicates."""
        if not rows:
            return 0

        values = [tuple(_sqlite_value(row.get(column)) for column in DISCOVERY_COLUMNS) for row in rows]
        placeholders = ", ".join("?" for _ in DISCOVERY_COLUMNS)
        columns = ", ".join(DISCOVERY_COLUMNS)

        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                f"INSERT OR IGNORE INTO pr ({columns}) VALUES ({placeholders})",
                values,
            )
            return conn.total_changes - before

    def update_pr_enrichment(self, pr_id: int, fields: dict) -> None:
        """Update enrichment columns for one PR row."""
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in ENRICHMENT_COLUMNS:
                continue
            if key == "files_json" and isinstance(value, list):
                updates[key] = json.dumps(value)
            else:
                updates[key] = _sqlite_value(value)

        if not updates:
            return

        assignments = ", ".join(f"{column} = ?" for column in updates)
        values = [updates[column] for column in updates]
        values.append(pr_id)

        with self._connect() as conn:
            conn.execute(f"UPDATE pr SET {assignments} WHERE id = ?", values)

    def insert_changes(self, pr_id: int, changes: list[Change | dict]) -> int:
        """Insert parsed dependency changes for one PR."""
        if not changes:
            return 0

        values = []
        group_size = len(changes)
        for change in changes:
            change_dict = asdict(change) if is_dataclass(change) else dict(change)
            values.append(
                (
                    pr_id,
                    change_dict["ecosystem"],
                    change_dict["package"],
                    change_dict.get("from_version"),
                    change_dict["to_version"],
                    change_dict["manifest_path"],
                    int(bool(change_dict["is_lockfile"])),
                    group_size,
                )
            )

        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO change (
                  pr_id, ecosystem, package, from_version, to_version, manifest_path,
                  is_lockfile, group_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            return conn.total_changes - before

    def mark_parse_attempted(self, pr_id: int, attempted_at: str | None = None) -> None:
        """Mark a PR as having reached the REST diff parsing step."""
        timestamp = attempted_at or datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE pr SET parse_attempted_at = ? WHERE id = ?",
                (timestamp, pr_id),
            )

    def iter_prs_needing_closing_comments(
        self,
        limit: int | None = None,
    ) -> Iterator[sqlite3.Row]:
        """Yield closed-unmerged PRs whose closing comments have not been fetched."""
        sql = """
            SELECT pr.*
            FROM pr
            WHERE pr.outcome_titlebody = 'closed-unmerged'
              AND pr.closing_comments_fetched_at IS NULL
            ORDER BY pr.event_created_at ASC, pr.id ASC
        """
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        yield from rows

    def replace_pr_closing_comments(
        self,
        pr_id: int,
        comments: list[dict[str, Any]],
        fetched_at: str | None = None,
    ) -> int:
        """Replace cached closing comments for one PR and mark it fetched."""
        timestamp = fetched_at or datetime.now(UTC).isoformat()
        values = [
            (
                pr_id,
                comment.get("author_login"),
                comment.get("author_type"),
                comment.get("body") or "",
                comment.get("created_at"),
                timestamp,
            )
            for comment in comments
        ]
        with self._connect() as conn:
            conn.execute("DELETE FROM pr_closing_comment WHERE pr_id = ?", (pr_id,))
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO pr_closing_comment (
                  pr_id, author_login, author_type, body, created_at, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            inserted = conn.total_changes - before
            conn.execute(
                "UPDATE pr SET closing_comments_fetched_at = ? WHERE id = ?",
                (timestamp, pr_id),
            )
        return inserted

    def update_pr_superseded_reasons(self, reasons: dict[int, str | None]) -> int:
        """Persist superseded reasons derived during outcome classification."""
        if not reasons:
            return 0
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "UPDATE pr SET superseded_reason = ? WHERE id = ?",
                [(reason, pr_id) for pr_id, reason in reasons.items()],
            )
            return conn.total_changes - before

    def insert_classifications(self, rows: list[dict]) -> int:
        """Insert classification rows, ignoring duplicates."""
        if not rows:
            return 0

        values = [
            (
                row["change_id"],
                row["dimension"],
                row["label"],
                row["classifier_version"],
                row["classified_at"],
            )
            for row in rows
        ]

        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO classification (
                  change_id, dimension, label, classifier_version, classified_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                values,
            )
            return conn.total_changes - before

    def delete_classifications_by_dimensions(
        self,
        dimensions: list[str],
        classifier_version: int,
    ) -> int:
        """Delete classifications for the requested dimensions and version."""
        if not dimensions:
            return 0
        placeholders = ", ".join("?" for _ in dimensions)
        with self._connect() as conn:
            before = conn.total_changes
            conn.execute(
                f"""
                DELETE FROM classification
                WHERE classifier_version = ?
                  AND dimension IN ({placeholders})
                """,
                (classifier_version, *dimensions),
            )
            return conn.total_changes - before

    def replace_triple_alignment(self, rows: list[dict]) -> int:
        """Replace the entire triple-alignment table with fresh rows."""
        with self._connect() as conn:
            backup_rows = conn.execute(
                """
                SELECT
                  ecosystem, package, from_version, to_version,
                  alignment_v1, k_projects_decided_v1, k_changes_v1, k_changes_total_v1,
                  n_merged_v1, n_closed_unmerged_v1, n_open_v1, n_superseded_v1,
                  alignment_pcb, k_projects_decided_pcb, k_changes_pcb, k_changes_total_pcb,
                  n_merged_pcb, n_closed_unmerged_pcb, n_open_pcb, n_superseded_pcb
                FROM triple_alignment
                WHERE alignment_v1 IS NOT NULL OR alignment_pcb IS NOT NULL
                """
            ).fetchall()
        v1_by_key = {
            (
                row["ecosystem"],
                row["package"],
                row["from_version"],
                row["to_version"],
            ): {column: row[column] for column in TRIPLE_ALIGNMENT_V1_COLUMNS}
            for row in backup_rows
            if row["alignment_v1"] is not None
        }
        pcb_by_key = {
            (
                row["ecosystem"],
                row["package"],
                row["from_version"],
                row["to_version"],
            ): {column: row[column] for column in TRIPLE_ALIGNMENT_PCB_COLUMNS}
            for row in backup_rows
            if row["alignment_pcb"] is not None
        }

        values = [
            self._alignment_insert_values(row, v1_by_key, pcb_by_key)
            for row in rows
        ]

        with self._connect() as conn:
            conn.execute("DELETE FROM triple_alignment")
            conn.executemany(
                """
                INSERT INTO triple_alignment (
                  ecosystem, package, from_version, to_version, semver_tier,
                  k_projects_all, k_projects_decided, k_changes, k_changes_total,
                  n_merged, n_closed_unmerged, n_open, n_superseded,
                  is_security_any, is_security_all,
                  n_dependabot, n_renovate, n_human, n_other_bot, source_mix,
                  alignment, mean_group_size, median_group_size, max_group_size,
                  all_singleton,
                  alignment_v1, k_projects_decided_v1, k_changes_v1, k_changes_total_v1,
                  n_merged_v1, n_closed_unmerged_v1, n_open_v1, n_superseded_v1,
                  alignment_pcb, k_projects_decided_pcb, k_changes_pcb, k_changes_total_pcb,
                  n_merged_pcb, n_closed_unmerged_pcb, n_open_pcb, n_superseded_pcb,
                  computed_at
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                values,
            )
        return len(rows)

    def backup_alignment_v1(self) -> dict[str, int]:
        """Copy current primary alignment columns into v1 backup columns once."""
        with self._connect() as conn:
            total_rows = conn.execute("SELECT COUNT(*) AS count FROM triple_alignment").fetchone()[
                "count"
            ]
            already_backed_up = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM triple_alignment
                WHERE alignment_v1 IS NOT NULL
                """
            ).fetchone()["count"]
            if total_rows > 0 and already_backed_up == total_rows:
                return {"rows_updated": 0, "already_backed_up": already_backed_up}

            before = conn.total_changes
            conn.execute(
                """
                UPDATE triple_alignment
                SET
                  alignment_v1 = alignment,
                  k_projects_decided_v1 = k_projects_decided,
                  k_changes_v1 = k_changes,
                  k_changes_total_v1 = k_changes_total,
                  n_merged_v1 = n_merged,
                  n_closed_unmerged_v1 = n_closed_unmerged,
                  n_open_v1 = n_open,
                  n_superseded_v1 = n_superseded
                WHERE alignment_v1 IS NULL
                """
            )
            rows_updated = conn.total_changes - before
        return {"rows_updated": rows_updated, "already_backed_up": already_backed_up}

    def backup_alignment_pcb(self) -> dict[str, int]:
        """Copy current primary alignment columns into pre-comment-baseline columns once."""
        with self._connect() as conn:
            total_rows = conn.execute("SELECT COUNT(*) AS count FROM triple_alignment").fetchone()[
                "count"
            ]
            already_backed_up = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM triple_alignment
                WHERE alignment_pcb IS NOT NULL
                """
            ).fetchone()["count"]
            if total_rows > 0 and already_backed_up == total_rows:
                return {"rows_updated": 0, "already_backed_up": already_backed_up}

            before = conn.total_changes
            conn.execute(
                """
                UPDATE triple_alignment
                SET
                  alignment_pcb = alignment,
                  k_projects_decided_pcb = k_projects_decided,
                  k_changes_pcb = k_changes,
                  k_changes_total_pcb = k_changes_total,
                  n_merged_pcb = n_merged,
                  n_closed_unmerged_pcb = n_closed_unmerged,
                  n_open_pcb = n_open,
                  n_superseded_pcb = n_superseded
                WHERE alignment_pcb IS NULL
                """
            )
            rows_updated = conn.total_changes - before
        return {"rows_updated": rows_updated, "already_backed_up": already_backed_up}

    def _alignment_insert_values(
        self,
        row: dict,
        v1_by_key: dict[tuple[str, str, str | None, str], dict[str, Any]],
        pcb_by_key: dict[tuple[str, str, str | None, str], dict[str, Any]],
    ) -> tuple:
        """Build a triple_alignment insert tuple, carrying backup columns forward."""
        key = (row["ecosystem"], row["package"], row.get("from_version"), row["to_version"])
        v1_values = {
            column: row.get(column, v1_by_key.get(key, {}).get(column))
            for column in TRIPLE_ALIGNMENT_V1_COLUMNS
        }
        pcb_values = {
            column: row.get(column, pcb_by_key.get(key, {}).get(column))
            for column in TRIPLE_ALIGNMENT_PCB_COLUMNS
        }
        return (
            row["ecosystem"],
            row["package"],
            row.get("from_version"),
            row["to_version"],
            row["semver_tier"],
            row["k_projects_all"],
            row["k_projects_decided"],
            row["k_changes"],
            row["k_changes_total"],
            row["n_merged"],
            row["n_closed_unmerged"],
            row["n_open"],
            row["n_superseded"],
            row["is_security_any"],
            row["is_security_all"],
            row["n_dependabot"],
            row["n_renovate"],
            row["n_human"],
            row["n_other_bot"],
            row["source_mix"],
            row["alignment"],
            row["mean_group_size"],
            row["median_group_size"],
            row["max_group_size"],
            row["all_singleton"],
            v1_values["alignment_v1"],
            v1_values["k_projects_decided_v1"],
            v1_values["k_changes_v1"],
            v1_values["k_changes_total_v1"],
            v1_values["n_merged_v1"],
            v1_values["n_closed_unmerged_v1"],
            v1_values["n_open_v1"],
            v1_values["n_superseded_v1"],
            pcb_values["alignment_pcb"],
            pcb_values["k_projects_decided_pcb"],
            pcb_values["k_changes_pcb"],
            pcb_values["k_changes_total_pcb"],
            pcb_values["n_merged_pcb"],
            pcb_values["n_closed_unmerged_pcb"],
            pcb_values["n_open_pcb"],
            pcb_values["n_superseded_pcb"],
            row["computed_at"],
        )

    def insert_pipeline_error(
        self,
        stage: str,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        pr_number: int | None = None,
        error_type: str | None = None,
        message: str | None = None,
    ) -> None:
        """Record a pipeline error event."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_error (
                  stage, repo_owner, repo_name, pr_number, error_type, message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage,
                    repo_owner,
                    repo_name,
                    pr_number,
                    error_type,
                    message,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def error_counts_by_stage(self) -> dict:
        """Summarize pipeline errors by stage and error type."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT stage, COALESCE(error_type, 'unknown') AS error_type, COUNT(*) AS count
                FROM pipeline_error
                GROUP BY stage, COALESCE(error_type, 'unknown')
                ORDER BY stage, error_type
                """
            ).fetchall()

        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["stage"], {})[row["error_type"]] = row["count"]
        return counts

    def iter_prs_needing_enrichment(self, limit: int | None = None) -> Iterator[sqlite3.Row]:
        """Yield PR rows that still need enrichment."""
        sql = "SELECT * FROM pr WHERE enriched_at IS NULL ORDER BY event_created_at ASC, id ASC"
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        yield from rows

    def iter_prs_needing_parsing(
        self,
        include_deferred: bool = False,
    ) -> Iterator[sqlite3.Row]:
        """Yield enriched PR rows that need parsing and have no changes yet."""
        priority_filter = ""
        if not include_deferred:
            priority_filter = """
                  AND (pr.parse_priority IS NULL OR pr.parse_priority = 'priority')
            """
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT pr.*
                FROM pr
                WHERE pr.passes_manifest_only_filter = 1
                  {priority_filter}
                  AND pr.parse_attempted_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM change WHERE change.pr_id = pr.id
                  )
                ORDER BY pr.event_created_at ASC, pr.id ASC
                """
            ).fetchall()
        yield from rows

    def iter_prs_with_parse_errors(
        self,
        include_deferred: bool = False,
    ) -> Iterator[sqlite3.Row]:
        """Yield PR rows with recorded parse errors for retry."""
        priority_filter = ""
        if not include_deferred:
            priority_filter = """
                  AND (pr.parse_priority IS NULL OR pr.parse_priority = 'priority')
            """
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT pr.*
                FROM pr
                JOIN pipeline_error
                  ON pipeline_error.repo_owner = pr.repo_owner
                 AND pipeline_error.repo_name = pr.repo_name
                 AND pipeline_error.pr_number = pr.pr_number
                WHERE pipeline_error.stage = 'parse'
                  AND pr.passes_manifest_only_filter = 1
                  {priority_filter}
                  AND NOT EXISTS (
                    SELECT 1 FROM change WHERE change.pr_id = pr.id
                  )
                ORDER BY pr.event_created_at ASC, pr.id ASC
                """
            ).fetchall()
        yield from rows

    def delete_parse_errors_for_pr(self, row: sqlite3.Row) -> int:
        """Delete parse errors for a PR after a successful retry."""
        with self._connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                DELETE FROM pipeline_error
                WHERE stage = 'parse'
                  AND repo_owner = ?
                  AND repo_name = ?
                  AND pr_number = ?
                """,
                (row["repo_owner"], row["repo_name"], row["pr_number"]),
            )
            return conn.total_changes - before

    def pr_counts_by_stage(self) -> dict:
        """Return summary counts for the main processing stages."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM pr) AS total_prs,
                  (SELECT COUNT(*) FROM pr WHERE title IS NOT NULL) AS enriched_prs,
                  (SELECT COUNT(*) FROM pr WHERE enriched_at IS NOT NULL) AS enrichment_attempted_prs,
                  (
                    SELECT COUNT(*) FROM pr
                    WHERE title IS NOT NULL AND passes_manifest_only_filter = 1
                  ) AS passed_manifest_only_filter,
                  (
                    SELECT COUNT(*) FROM pr
                    WHERE title IS NOT NULL AND passes_manifest_only_filter = 0
                  ) AS failed_manifest_only_filter,
                  (
                    SELECT COUNT(*) FROM pr
                    WHERE enriched_at IS NOT NULL AND title IS NULL
                  ) AS enrichment_error_prs,
                  (
                    SELECT COUNT(*) FROM pr WHERE files_has_next_page = 1
                  ) AS files_has_next_page,
                  (SELECT COUNT(DISTINCT pr_id) FROM change) AS parsed_prs,
                  (SELECT COUNT(*) FROM change) AS total_changes
                """
            ).fetchone()
            reason_rows = conn.execute(
                """
                SELECT COALESCE(manifest_filter_reason, 'unknown') AS reason, COUNT(*) AS count
                FROM pr
                WHERE passes_manifest_only_filter IS NOT NULL
                GROUP BY COALESCE(manifest_filter_reason, 'unknown')
                ORDER BY reason
                """
            ).fetchall()
        result = dict(row)
        result["manifest_filter_reasons"] = {
            reason_row["reason"]: reason_row["count"] for reason_row in reason_rows
        }
        result["pipeline_errors"] = self.error_counts_by_stage()
        return result

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Apply additive schema migrations for diagnostics columns."""
        existing_pr_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pr)").fetchall()
        }
        for column, column_type in PR_DIAGNOSTIC_COLUMNS.items():
            if column not in existing_pr_columns:
                conn.execute(f"ALTER TABLE pr ADD COLUMN {column} {column_type}")

        existing_change_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(change)").fetchall()
        }
        for column, column_type in CHANGE_DIAGNOSTIC_COLUMNS.items():
            if column not in existing_change_columns:
                conn.execute(f"ALTER TABLE change ADD COLUMN {column} {column_type}")

        existing_alignment_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(triple_alignment)").fetchall()
        }
        if "k_projects" in existing_alignment_columns and "k_projects_all" not in existing_alignment_columns:
            conn.execute("ALTER TABLE triple_alignment RENAME COLUMN k_projects TO k_projects_all")
            existing_alignment_columns.remove("k_projects")
            existing_alignment_columns.add("k_projects_all")
        for column, column_type in TRIPLE_ALIGNMENT_COLUMNS.items():
            if column not in existing_alignment_columns:
                conn.execute(f"ALTER TABLE triple_alignment ADD COLUMN {column} {column_type}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_triple_alignment_k "
            "ON triple_alignment(k_projects_decided)"
        )

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with the expected row and FK settings."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


def _sqlite_value(value: Any) -> Any:
    """Convert Python values into SQLite-friendly scalar values."""
    if isinstance(value, bool):
        return int(value)
    return value
