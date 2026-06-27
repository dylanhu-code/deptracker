"""Parse dependency changes from enriched pull request diffs."""

from __future__ import annotations

import logging
import time
from itertools import islice

import httpx
from unidiff import PatchSet

from deptracker.adapters.base import adapter_for_file
from deptracker.diffutils import strip_diff_path
from deptracker.store import Store

LOGGER = logging.getLogger(__name__)


def parse(
    store: Store,
    token: str,
    max_prs: int | None = None,
    stop_remaining: int | None = None,
    include_deferred: bool = False,
    max_elapsed_seconds: int | None = None,
    progress_every: int | None = None,
    retry_errors: bool = False,
) -> dict:
    """Fetch PR diffs from GitHub and parse dependency changes into the store."""
    rows_iter = (
        store.iter_prs_with_parse_errors(include_deferred=include_deferred)
        if retry_errors
        else store.iter_prs_needing_parsing(include_deferred=include_deferred)
    )
    rows = list(islice(rows_iter, max_prs)) if max_prs is not None else list(rows_iter)
    started = time.perf_counter()
    totals = {
        "parsed": 0,
        "changes_inserted": 0,
        "errors": 0,
        "stopped_for_rate_limit": False,
        "stopped_for_elapsed_time": False,
        "rate_limit_remaining": None,
        "rate_limit_reset": None,
        "parse_errors_removed": 0,
        "elapsed_seconds": 0.0,
    }
    next_progress = progress_every if progress_every and progress_every > 0 else None

    headers = {"Accept": "application/vnd.github.v3.diff"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for row in rows:
            if max_elapsed_seconds is not None and time.perf_counter() - started >= max_elapsed_seconds:
                totals["stopped_for_elapsed_time"] = True
                LOGGER.info(
                    "stopping parse because elapsed time reached %s seconds",
                    max_elapsed_seconds,
                )
                break

            url = (
                "https://api.github.com/repos/"
                f"{row['repo_owner']}/{row['repo_name']}/pulls/{row['pr_number']}.diff"
            )
            try:
                response = client.get(url, headers=headers)
                totals["rate_limit_remaining"] = _rate_limit_remaining(response)
                totals["rate_limit_reset"] = _rate_limit_reset(response)
                if _should_stop_for_rate_limit(response, stop_remaining):
                    totals["stopped_for_rate_limit"] = True
                    LOGGER.info(
                        "stopping parse for GitHub REST rate limit: remaining=%s",
                        totals["rate_limit_remaining"],
                    )
                    break
                store.mark_parse_attempted(row["id"])
                response.raise_for_status()
                changes = _changes_from_diff(response.text)
                totals["changes_inserted"] += store.insert_changes(row["id"], changes)
                totals["parsed"] += 1
                if retry_errors:
                    totals["parse_errors_removed"] += store.delete_parse_errors_for_pr(row)
                LOGGER.debug(
                    "parsed %s/%s#%s",
                    row["repo_owner"],
                    row["repo_name"],
                    row["pr_number"],
                )
            except Exception as exc:
                store.mark_parse_attempted(row["id"])
                store.insert_pipeline_error(
                    stage="parse",
                    repo_owner=row["repo_owner"],
                    repo_name=row["repo_name"],
                    pr_number=row["pr_number"],
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                LOGGER.warning(
                    "failed to parse %s/%s#%s: %s",
                    row["repo_owner"],
                    row["repo_name"],
                    row["pr_number"],
                    exc,
                )
                totals["errors"] += 1

            processed = totals["parsed"] + totals["errors"]
            if next_progress is not None and processed >= next_progress:
                LOGGER.info(
                    "parse progress: processed=%s parsed=%s changes_inserted=%s errors=%s "
                    "rate_limit_remaining=%s elapsed_seconds=%.2f",
                    processed,
                    totals["parsed"],
                    totals["changes_inserted"],
                    totals["errors"],
                    totals["rate_limit_remaining"],
                    time.perf_counter() - started,
                )
                while next_progress is not None and processed >= next_progress:
                    next_progress += progress_every or 0

    totals["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    return totals


def _changes_from_diff(diff_text: str):
    """Parse a unified diff into adapter-specific dependency changes."""
    changes = []
    patch_set = PatchSet(diff_text.splitlines(keepends=True))
    for patched_file in patch_set:
        target_path = patched_file.target_file
        if target_path == "/dev/null":
            target_path = patched_file.source_file
        path = strip_diff_path(target_path)
        adapter = adapter_for_file(path)
        if not adapter:
            continue
        changes.extend(adapter.parse_diff(path, str(patched_file)))
    return changes


def _rate_limit_remaining(response: httpx.Response) -> int | None:
    """Read the remaining GitHub rate limit from a response header."""
    value = response.headers.get("x-ratelimit-remaining")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _rate_limit_reset(response: httpx.Response) -> int | None:
    """Read the GitHub rate-limit reset epoch from a response header."""
    value = response.headers.get("x-ratelimit-reset")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _should_stop_for_rate_limit(
    response: httpx.Response,
    stop_remaining: int | None,
) -> bool:
    """Decide whether parsing should stop because GitHub rate limit is too low."""
    remaining = _rate_limit_remaining(response)
    if stop_remaining is not None and remaining is not None and remaining < stop_remaining:
        return True
    if response.status_code not in {403, 429}:
        return False
    body = response.text.lower()
    return "rate limit" in body or "secondary rate" in body
