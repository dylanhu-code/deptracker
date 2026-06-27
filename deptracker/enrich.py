"""Enrich candidate pull requests through the GitHub GraphQL API."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from itertools import islice
from typing import Any

import httpx

from deptracker.adapters.base import adapter_for_file
from deptracker.adapters.base import is_manifest_path
from deptracker.store import Store

LOGGER = logging.getLogger(__name__)
GRAPHQL_URL = "https://api.github.com/graphql"
FILE_PAGE_SIZE = 100
MAX_FILES_PER_PR = 500


class RateLimitStop(Exception):
    """Signal that enrichment should stop because the rate limit is too low."""

    def __init__(self, remaining: int) -> None:
        """Create a stop signal carrying the remaining API budget."""
        super().__init__(f"rate-limit remaining {remaining} is below stop threshold")
        self.remaining = remaining


class GraphQLDataError(Exception):
    """Represent malformed or incomplete GraphQL response data."""

    def __init__(self, error_type: str, message: str | None = None) -> None:
        """Create a typed GraphQL data error for per-PR diagnostics."""
        super().__init__(message or error_type)
        self.error_type = error_type
        self.message = message


def enrich(
    store: Store,
    token: str,
    batch_size: int = 25,
    max_prs: int | None = None,
    stop_remaining: int | None = None,
    max_elapsed_seconds: int | None = None,
    progress_every: int | None = None,
    ecosystem_priority: set[str] | None = None,
    stop_when_priority_targets_met: dict[str, int] | None = None,
) -> dict:
    """Enrich pending PR rows from GitHub GraphQL."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    rows = list(store.iter_prs_needing_enrichment(limit=max_prs))
    return enrich_rows(
        store=store,
        token=token,
        rows=rows,
        batch_size=batch_size,
        stop_remaining=stop_remaining,
        max_elapsed_seconds=max_elapsed_seconds,
        progress_every=progress_every,
        ecosystem_priority=ecosystem_priority,
        stop_when_priority_targets_met=stop_when_priority_targets_met,
    )


def enrich_rows(
    store: Store,
    token: str,
    rows: list[Any],
    batch_size: int = 25,
    stop_remaining: int | None = None,
    max_elapsed_seconds: int | None = None,
    progress_every: int | None = None,
    ecosystem_priority: set[str] | None = None,
    stop_when_priority_targets_met: dict[str, int] | None = None,
) -> dict:
    """Enrich an explicit list of PR rows."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    totals = {
        "enriched": 0,
        "passes_filter": 0,
        "errors": 0,
        "stopped_for_rate_limit": False,
        "stopped_for_elapsed_time": False,
        "stopped_for_priority_targets": False,
        "rate_limit_remaining": None,
        "priority_enriched": 0,
        "deferred_enriched": 0,
        "priority_filter_passes": 0,
        "deferred_filter_passes": 0,
        "priority_target_summary": None,
        "elapsed_seconds": 0.0,
    }

    started_at = time.monotonic()
    next_progress = progress_every if progress_every and progress_every > 0 else None
    priority_ecosystems = ecosystem_priority or set()
    next_priority_target_check = 0 if stop_when_priority_targets_met else None

    headers = {
        "Authorization": f"bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=30) as client:
        for batch in _batched(rows, batch_size):
            if _elapsed_exceeded(started_at, max_elapsed_seconds):
                totals["stopped_for_elapsed_time"] = True
                totals["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
                LOGGER.info(
                    "stopping enrichment because elapsed time reached %s seconds",
                    max_elapsed_seconds,
                )
                return totals

            query, aliases = _build_query(batch)
            try:
                response = _post_graphql(client, query, headers)
            except httpx.HTTPError as exc:
                LOGGER.warning("GraphQL batch failed after retry: %s", exc)
                totals["errors"] += len(batch)
                for row in batch:
                    store.insert_pipeline_error(
                        stage="enrich",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type="network_or_http",
                        message=str(exc),
                    )
                continue

            remaining = _rate_limit_remaining(response)
            totals["rate_limit_remaining"] = remaining
            payload = response.json()
            errors_by_alias = _errors_by_alias(payload.get("errors") or [])
            if payload.get("errors"):
                LOGGER.warning("GraphQL returned %s error(s)", len(payload["errors"]))

            data = payload.get("data") or {}
            for alias, row in aliases.items():
                repo_data = data.get(alias)
                if not repo_data:
                    graph_error = errors_by_alias.get(alias, {})
                    totals["errors"] += 1
                    store.insert_pipeline_error(
                        stage="enrich",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type=graph_error.get("type", "graphql_error"),
                        message=graph_error.get("message"),
                    )
                    store.update_pr_enrichment(
                        row["id"],
                        {
                            "passes_manifest_only_filter": 0,
                            "manifest_filter_reason": _graphql_filter_reason(
                                graph_error.get("type")
                            ),
                            "enriched_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    continue

                pr_data = repo_data.get("pullRequest")
                if not pr_data:
                    graph_error = errors_by_alias.get(alias, {})
                    totals["errors"] += 1
                    store.insert_pipeline_error(
                        stage="enrich",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type=graph_error.get("type", "graphql_error"),
                        message=graph_error.get("message"),
                    )
                    store.update_pr_enrichment(
                        row["id"],
                        {
                            "passes_manifest_only_filter": 0,
                            "manifest_filter_reason": _graphql_filter_reason(
                                graph_error.get("type")
                            ),
                            "enriched_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    continue

                try:
                    pr_data = _with_paginated_files(
                        client=client,
                        headers=headers,
                        row=row,
                        pr_data=pr_data,
                        stop_remaining=stop_remaining,
                    )
                except RateLimitStop as exc:
                    totals["stopped_for_rate_limit"] = True
                    totals["rate_limit_remaining"] = exc.remaining
                    LOGGER.info(
                        "stopping enrichment because rate-limit remaining %s is below %s",
                        exc.remaining,
                        stop_remaining,
                    )
                    totals["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
                    return totals
                except httpx.HTTPError as exc:
                    totals["errors"] += 1
                    store.insert_pipeline_error(
                        stage="enrich",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type="network_or_http",
                        message=str(exc),
                    )
                    continue
                except GraphQLDataError as exc:
                    totals["errors"] += 1
                    store.insert_pipeline_error(
                        stage="enrich",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type=exc.error_type,
                        message=exc.message,
                    )
                    continue

                fields = _enrichment_fields(
                    pr_data,
                    priority_ecosystems=priority_ecosystems or None,
                )
                store.update_pr_enrichment(row["id"], fields)
                totals["enriched"] += 1
                if fields["passes_manifest_only_filter"]:
                    totals["passes_filter"] += 1
                if fields.get("parse_priority") == "priority":
                    totals["priority_enriched"] += 1
                    totals["priority_filter_passes"] += 1
                elif fields.get("parse_priority") == "deferred_non_priority":
                    totals["deferred_enriched"] += 1
                    totals["deferred_filter_passes"] += 1
                LOGGER.debug(
                    "enriched %s/%s#%s",
                    row["repo_owner"],
                    row["repo_name"],
                    row["pr_number"],
                )

            processed = totals["enriched"] + totals["errors"]
            if next_progress is not None and processed >= next_progress:
                elapsed = round(time.monotonic() - started_at, 2)
                if priority_ecosystems:
                    LOGGER.info(
                        "enrichment progress: processed=%s enriched=%s "
                        "(priority=%s deferred=%s) filter_passes=%s "
                        "(priority=%s deferred=%s) rate_limit_remaining=%s "
                        "elapsed_seconds=%s",
                        processed,
                        totals["enriched"],
                        totals["priority_enriched"],
                        totals["deferred_enriched"],
                        totals["passes_filter"],
                        totals["priority_filter_passes"],
                        totals["deferred_filter_passes"],
                        totals["rate_limit_remaining"],
                        elapsed,
                    )
                else:
                    LOGGER.info(
                        "enrichment progress: processed=%s enriched=%s passes_filter=%s "
                        "rate_limit_remaining=%s elapsed_seconds=%s",
                        processed,
                        totals["enriched"],
                        totals["passes_filter"],
                        totals["rate_limit_remaining"],
                        elapsed,
                    )
                while next_progress is not None and processed >= next_progress:
                    next_progress += progress_every or 0

            if (
                stop_when_priority_targets_met
                and next_priority_target_check is not None
                and processed >= next_priority_target_check
            ):
                target_summary = _priority_target_summary(
                    store,
                    stop_when_priority_targets_met,
                )
                totals["priority_target_summary"] = target_summary
                if target_summary["targets_met"]:
                    totals["stopped_for_priority_targets"] = True
                    totals["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
                    LOGGER.info(
                        "stopping enrichment because priority alignment targets are met: %s",
                        target_summary["counts"],
                    )
                    return totals
                next_priority_target_check = processed + 2000

            if stop_remaining is not None and remaining is not None and remaining < stop_remaining:
                totals["stopped_for_rate_limit"] = True
                LOGGER.info(
                    "stopping enrichment because rate-limit remaining %s is below %s",
                    remaining,
                    stop_remaining,
                )
                break
            _respect_rate_limit(response)

    totals["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
    return totals


def _elapsed_exceeded(started_at: float, max_elapsed_seconds: int | None) -> bool:
    """Check whether the configured runtime limit has been reached."""
    return max_elapsed_seconds is not None and time.monotonic() - started_at >= max_elapsed_seconds


def _batched(rows: list[Any], size: int) -> list[list[Any]]:
    """Split a sequence into fixed-size batches."""
    iterator = iter(rows)
    batches: list[list[Any]] = []
    while batch := list(islice(iterator, size)):
        batches.append(batch)
    return batches


def _post_graphql(client: httpx.Client, query: str, headers: dict[str, str]) -> httpx.Response:
    """Post a GraphQL query with one retry after transient failures."""
    try:
        response = client.post(GRAPHQL_URL, json={"query": query}, headers=headers)
        response.raise_for_status()
        return response
    except httpx.HTTPError:
        time.sleep(5)
        response = client.post(GRAPHQL_URL, json={"query": query}, headers=headers)
        response.raise_for_status()
        return response


def _build_query(rows: list[Any]) -> tuple[str, dict[str, Any]]:
    """Build a batched GraphQL query for PR enrichment."""
    aliases: dict[str, Any] = {}
    parts = ["query {"]
    for index, row in enumerate(rows):
        alias = f"pr_{row['id']}_{index}"
        aliases[alias] = row
        owner = json.dumps(row["repo_owner"])
        repo = json.dumps(row["repo_name"])
        number = int(row["pr_number"])
        parts.append(
            f"""
            {alias}: repository(owner: {owner}, name: {repo}) {{
              pullRequest(number: {number}) {{
                createdAt
                title
                body
                author {{
                  __typename
                  login
                }}
                merged
                state
                closedAt
                mergedAt
                additions
                deletions
                baseRefOid
                headRefOid
                isCrossRepository
                commits(first: 50) {{
                  nodes {{
                    commit {{
                      message
                    }}
                  }}
                  pageInfo {{
                    hasNextPage
                  }}
                }}
                repository {{
                  isFork
                  isArchived
                  object(expression: "HEAD~10000") {{
                    ... on Commit {{
                      oid
                    }}
                  }}
                }}
                files(first: {FILE_PAGE_SIZE}) {{
                  nodes {{
                    path
                    additions
                    deletions
                  }}
                  pageInfo {{
                    hasNextPage
                    endCursor
                  }}
                }}
              }}
            }}
            """
        )
    parts.append("}")
    return "\n".join(parts), aliases


def _with_paginated_files(
    client: httpx.Client,
    headers: dict[str, str],
    row: Any,
    pr_data: dict[str, Any],
    stop_remaining: int | None = None,
) -> dict[str, Any]:
    """Fetch all PR files with pagination and a hard file cap."""
    files_connection = pr_data.get("files") or {}
    page_info = files_connection.get("pageInfo") or {}
    nodes = list(files_connection.get("nodes") or [])
    cap_exceeded = False

    while page_info.get("hasNextPage"):
        if len(nodes) >= MAX_FILES_PER_PR:
            cap_exceeded = True
            break

        cursor = page_info.get("endCursor")
        query = _build_files_page_query(row, cursor, min(FILE_PAGE_SIZE, MAX_FILES_PER_PR - len(nodes)))
        response = _post_graphql(client, query, headers)
        remaining = _rate_limit_remaining(response)
        payload = response.json()
        if payload.get("errors"):
            error = payload["errors"][0]
            raise GraphQLDataError(error.get("type", "graphql_error"), error.get("message"))

        files_page = (
            ((payload.get("data") or {}).get("repository") or {})
            .get("pullRequest", {})
            .get("files")
        )
        if not files_page:
            raise GraphQLDataError("graphql_error", "Missing files page in GraphQL response")

        nodes.extend(files_page.get("nodes") or [])
        page_info = files_page.get("pageInfo") or {}

        if stop_remaining is not None and remaining is not None and remaining < stop_remaining:
            raise RateLimitStop(remaining)
        _respect_rate_limit(response)

    updated = dict(pr_data)
    updated["files"] = {
        "nodes": nodes[:MAX_FILES_PER_PR],
        "pageInfo": {
            "hasNextPage": bool(cap_exceeded or page_info.get("hasNextPage")),
            "endCursor": page_info.get("endCursor"),
            "capExceeded": cap_exceeded,
        },
    }
    return updated


def _build_files_page_query(row: Any, cursor: str | None, first: int) -> str:
    """Build one GraphQL page request for PR files."""
    owner = json.dumps(row["repo_owner"])
    repo = json.dumps(row["repo_name"])
    number = int(row["pr_number"])
    after = f", after: {json.dumps(cursor)}" if cursor else ""
    return f"""
    query {{
      repository(owner: {owner}, name: {repo}) {{
        pullRequest(number: {number}) {{
          files(first: {first}{after}) {{
            nodes {{
              path
              additions
              deletions
            }}
            pageInfo {{
              hasNextPage
              endCursor
            }}
          }}
        }}
      }}
    }}
    """


def _enrichment_fields(
    pr_data: dict[str, Any],
    priority_ecosystems: set[str] | None = None,
) -> dict[str, Any]:
    """Extract normalized enrichment fields from GraphQL PR data."""
    file_nodes = pr_data.get("files", {}).get("nodes") or []
    page_info = pr_data.get("files", {}).get("pageInfo") or {}
    cap_exceeded = bool(page_info.get("capExceeded"))
    files = [
        {
            "path": node.get("path"),
            "additions": node.get("additions"),
            "deletions": node.get("deletions"),
        }
        for node in file_nodes
        if node.get("path")
    ]
    has_next_page = bool(page_info.get("hasNextPage"))
    manifest_paths = [is_manifest_path(file_info["path"]) for file_info in files]
    file_count = len(files)
    manifest_file_count = sum(1 for matched in manifest_paths if matched)
    non_manifest_file_count = file_count - manifest_file_count
    passes_filter = (
        bool(files)
        and manifest_file_count > 0
        and non_manifest_file_count == 0
        and not has_next_page
        and not cap_exceeded
    )
    manifest_filter_reason = _manifest_filter_reason(
        passes_filter=passes_filter,
        has_next_page=has_next_page,
        cap_exceeded=cap_exceeded,
        manifest_file_count=manifest_file_count,
        non_manifest_file_count=non_manifest_file_count,
    )

    repository = pr_data.get("repository") or {}
    author = pr_data.get("author") or {}
    commit_messages_concat, commits_truncated = _commit_messages_fields(pr_data)
    fields = {
        "pr_created_at": pr_data.get("createdAt"),
        "title": pr_data.get("title"),
        "body": pr_data.get("body"),
        "author_login": author.get("login"),
        "author_type": author.get("__typename"),
        "state": pr_data.get("state"),
        "merged": pr_data.get("merged"),
        "merged_at": pr_data.get("mergedAt"),
        "closed_at": pr_data.get("closedAt"),
        "additions": pr_data.get("additions"),
        "deletions": pr_data.get("deletions"),
        "base_sha": pr_data.get("baseRefOid"),
        "head_sha": pr_data.get("headRefOid"),
        "is_cross_repository": pr_data.get("isCrossRepository"),
        "is_fork": repository.get("isFork"),
        "is_archived": repository.get("isArchived"),
        "root_commit_sha": ((repository.get("object") or {}).get("oid")),
        "commit_messages_concat": commit_messages_concat,
        "commits_truncated": int(commits_truncated),
        "files_json": files,
        "passes_manifest_only_filter": int(passes_filter),
        "files_has_next_page": int(has_next_page),
        "file_count": file_count,
        "manifest_file_count": manifest_file_count,
        "non_manifest_file_count": non_manifest_file_count,
        "manifest_filter_reason": manifest_filter_reason,
        "enriched_at": datetime.now(UTC).isoformat(),
    }
    if priority_ecosystems is not None:
        fields["parse_priority"] = _parse_priority_for_files(
            files,
            passes_filter=passes_filter,
            priority_ecosystems=priority_ecosystems,
        )
    return fields


def _parse_priority_for_files(
    files: list[dict[str, Any]],
    passes_filter: bool,
    priority_ecosystems: set[str],
) -> str | None:
    """Classify a filter-passing PR as priority or deferred for parsing."""
    if not passes_filter:
        return None
    ecosystems = ecosystems_for_manifest_files(files)
    if ecosystems & priority_ecosystems:
        return "priority"
    return "deferred_non_priority"


def ecosystems_for_manifest_files(files: list[dict[str, Any]]) -> set[str]:
    """Infer ecosystem adapters represented by a list of changed manifest files."""
    ecosystems: set[str] = set()
    for file_info in files:
        path = file_info.get("path")
        if not path:
            continue
        adapter = adapter_for_file(path)
        if adapter:
            ecosystems.add(adapter.name)
    return ecosystems


def _priority_target_summary(store: Store, targets: dict[str, int]) -> dict[str, Any]:
    """Recompute alignment and check whether priority ecosystem targets are met."""
    from deptracker.alignment import compute_alignment

    summary = compute_alignment(store)
    counts = summary.get("triples_k_ge_5_per_ecosystem", {})
    return {
        "targets": targets,
        "counts": {ecosystem: counts.get(ecosystem, 0) for ecosystem in targets},
        "targets_met": all(counts.get(ecosystem, 0) >= target for ecosystem, target in targets.items()),
    }


def _commit_messages_fields(pr_data: dict[str, Any]) -> tuple[str, bool]:
    """Concatenate commit messages and note whether pagination was truncated."""
    commits = pr_data.get("commits") or {}
    messages = []
    for node in commits.get("nodes") or []:
        commit = node.get("commit") if isinstance(node, dict) else None
        message = commit.get("message") if isinstance(commit, dict) else None
        if isinstance(message, str) and message:
            messages.append(message)
    page_info = commits.get("pageInfo") or {}
    return "\n---\n".join(messages), bool(page_info.get("hasNextPage"))


def _manifest_filter_reason(
    passes_filter: bool,
    has_next_page: bool,
    cap_exceeded: bool,
    manifest_file_count: int,
    non_manifest_file_count: int,
) -> str:
    """Derive the manifest filter reason for an enrichment result."""
    if passes_filter:
        return "passed"
    if cap_exceeded:
        return "too_many_files_cap"
    if has_next_page:
        return "too_many_files"
    if manifest_file_count == 0:
        return "no_manifest_files"
    if non_manifest_file_count > 0:
        return "mixed_manifest_and_non_manifest_files"
    return "other"


def _errors_by_alias(errors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group GraphQL errors by query alias."""
    by_alias: dict[str, dict[str, Any]] = {}
    for error in errors:
        path = error.get("path") or []
        if path:
            by_alias[str(path[0])] = error
    return by_alias


def _graphql_filter_reason(error_type: str | None) -> str:
    """Map GraphQL error types to manifest-filter reasons."""
    if error_type == "NOT_FOUND":
        return "graphql_not_found"
    return "graphql_error"


def _rate_limit_remaining(response: httpx.Response) -> int | None:
    """Read the remaining GraphQL rate limit from response headers."""
    try:
        return int(response.headers["x-ratelimit-remaining"])
    except (KeyError, ValueError):
        return None


def _respect_rate_limit(response: httpx.Response) -> None:
    """Sleep when the remaining rate limit drops below the local threshold."""
    try:
        remaining = int(response.headers.get("x-ratelimit-remaining", "999999"))
        reset = int(response.headers.get("x-ratelimit-reset", "0"))
    except ValueError:
        return

    if remaining < 100:
        sleep_for = max(0, reset - int(time.time()))
        if sleep_for:
            LOGGER.info("GitHub rate limit low; sleeping %s seconds", sleep_for)
            time.sleep(sleep_for)
