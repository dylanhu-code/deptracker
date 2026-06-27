"""Discover candidate pull request events from GH Archive BigQuery tables."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from google.cloud import bigquery

from deptracker.store import Store

LOGGER = logging.getLogger(__name__)


def discover(project_id: str, date: str, store: Store, limit: int | None = None) -> int:
    """Discover candidate PR events for a GH Archive date and store them."""
    if not re.fullmatch(r"\d{8}", date):
        raise ValueError("date must be exactly YYYYMMDD digits")
    if limit is not None and limit < 1:
        raise ValueError("limit must be a positive integer")

    table = f"githubarchive.day.{date}"
    sql = f"""
    SELECT
      created_at AS event_created_at,
      SPLIT(repo.name, '/')[SAFE_OFFSET(0)] AS repo_owner,
      SPLIT(repo.name, '/')[SAFE_OFFSET(1)] AS repo_name,
      actor.login AS actor_login,
      JSON_VALUE(payload, '$.action') AS pr_action,
      CAST(JSON_VALUE(payload, '$.number') AS INT64) AS pr_number
    FROM `{table}`
    WHERE type = 'PullRequestEvent'
      AND JSON_VALUE(payload, '$.action') IN ('opened', 'synchronize', 'reopened')
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY repo.name, CAST(JSON_VALUE(payload, '$.number') AS INT64)
      ORDER BY created_at ASC
    ) = 1
    ORDER BY event_created_at ASC
    """

    job_config = None
    if limit is not None:
        sql += "\nLIMIT @limit"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
        )

    LOGGER.info("querying BigQuery table %s", table)
    started = time.monotonic()
    client = bigquery.Client(project=project_id)
    query_job = client.query(sql, job_config=job_config)

    inserted = 0
    fetched = 0
    batch: list[dict[str, Any]] = []
    for row in query_job.result(page_size=1000):
        fetched += 1
        batch.append(
            {
                "event_created_at": _serialize_value(row["event_created_at"]),
                "repo_owner": row["repo_owner"],
                "repo_name": row["repo_name"],
                "actor_login": row["actor_login"],
                "pr_action": row["pr_action"],
                "pr_number": row["pr_number"],
            }
        )
        if len(batch) >= 1000:
            inserted += store.insert_pr_batch(batch)
            batch.clear()

    if batch:
        inserted += store.insert_pr_batch(batch)

    elapsed = time.monotonic() - started
    LOGGER.info("fetched %s BigQuery rows", fetched)
    LOGGER.info("inserted %s PR rows", inserted)
    LOGGER.info("ignored %s duplicate PR rows", fetched - inserted)
    LOGGER.info("BigQuery bytes processed: %s", query_job.total_bytes_processed)
    LOGGER.info("discovery elapsed seconds: %.2f", elapsed)
    return inserted


def _serialize_value(value: Any) -> Any:
    """Serialize BigQuery scalar values into JSON-friendly values."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
