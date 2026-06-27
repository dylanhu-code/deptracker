"""Fetch and classify bot comment evidence for superseded/workflow closures."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any

import httpx

from deptracker.store import Store

LOGGER = logging.getLogger(__name__)
GRAPHQL_URL = "https://api.github.com/graphql"
COMMENT_PAGE_SIZE = 15
DEFAULT_STOP_REMAINING = 200

CURATED_BOT_LOGINS = frozenset(
    {
        "pyup-bot",
        "pre-commit-ci",
        "whitesource-bolt-for-github",
        "snyk-bot",
        "mend-for-github-com",
        "restyled-io",
        "github-actions",
        "imgbot",
        "allcontributors",
    }
)

EXCLUDE_PATTERNS = (
    re.compile(r"won.?t notify you again about this release", re.IGNORECASE),
)


@dataclass(frozen=True)
class SupersessionPattern:
    """One bot-family-specific supersession or workflow pattern."""

    bot_family: str
    regex: re.Pattern[str]
    reason: str
    name: str


@dataclass(frozen=True)
class SupersessionMatch:
    """Positive bot-comment match used to reclassify a PR as superseded."""

    reason: str
    bot_family: str
    pattern_name: str
    comment_text: str
    created_at: str | None


COMMENT_PATTERNS = (
    SupersessionPattern(
        "dependabot",
        re.compile(r"superseded by #?\d+", re.IGNORECASE),
        "comment_superseded",
        "dependabot_superseded_by_pr",
    ),
    SupersessionPattern(
        "dependabot",
        re.compile(r"updatable in another way", re.IGNORECASE),
        "comment_grouped",
        "dependabot_updatable_elsewhere",
    ),
    SupersessionPattern(
        "dependabot",
        re.compile(r"no longer needed", re.IGNORECASE),
        "comment_grouped",
        "dependabot_no_longer_needed",
    ),
    SupersessionPattern(
        "dependabot",
        re.compile(r"up[- ]to[- ]date now", re.IGNORECASE),
        "comment_grouped",
        "dependabot_up_to_date_now",
    ),
    SupersessionPattern(
        "renovate",
        re.compile(r"superseded by #?\d+", re.IGNORECASE),
        "comment_superseded",
        "renovate_superseded_by_pr",
    ),
    SupersessionPattern(
        "renovate",
        re.compile(r"autoclosing this pr", re.IGNORECASE),
        "comment_grouped",
        "renovate_autoclosing",
    ),
    SupersessionPattern(
        "renovate",
        re.compile(r"superseded", re.IGNORECASE),
        "comment_grouped",
        "renovate_superseded_generic",
    ),
    SupersessionPattern(
        "renovate",
        re.compile(r"renovate.*clos", re.IGNORECASE | re.DOTALL),
        "comment_grouped",
        "renovate_closure_generic",
    ),
    SupersessionPattern(
        "other-bot",
        re.compile(r"superseded by #?\d+", re.IGNORECASE),
        "comment_superseded",
        "generic_superseded_by_pr",
    ),
    SupersessionPattern(
        "other-bot",
        re.compile(r"updatable in another way|no longer needed|up[- ]to[- ]date now", re.IGNORECASE),
        "comment_grouped",
        "generic_grouped_or_obsolete",
    ),
)


def fetch_closing_comments(
    store: Store,
    token: str | None = None,
    *,
    batch_size: int = 25,
    max_prs: int | None = None,
    stop_remaining: int | None = DEFAULT_STOP_REMAINING,
    max_elapsed_seconds: int | None = None,
    progress_every: int | None = None,
) -> dict[str, Any]:
    """Fetch last PR comments for closed-unmerged PRs into the local cache."""
    rows = list(store.iter_prs_needing_closing_comments(limit=max_prs))
    started_at = time.monotonic()
    totals: dict[str, Any] = {
        "prs_selected": len(rows),
        "prs_fetched": 0,
        "comments_cached": 0,
        "errors": 0,
        "rate_limit_remaining": None,
        "stopped_for_rate_limit": False,
        "stopped_for_elapsed": False,
        "elapsed_seconds": 0.0,
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "deptracker-closing-comments",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    next_progress = progress_every if progress_every and progress_every > 0 else None
    with httpx.Client(timeout=30) as client:
        for batch in _batched(rows, batch_size):
            if _elapsed_exceeded(started_at, max_elapsed_seconds):
                totals["stopped_for_elapsed"] = True
                break
            query, aliases = _build_comments_query(batch)
            try:
                response = _post_graphql(client, query, headers)
            except httpx.HTTPError as exc:
                totals["errors"] += len(batch)
                for row in batch:
                    store.insert_pipeline_error(
                        stage="fetch_closing_comments",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                continue

            remaining = _rate_limit_remaining(response)
            totals["rate_limit_remaining"] = remaining
            payload = response.json()
            errors_by_alias = _errors_by_alias(payload.get("errors") or [])
            data = payload.get("data") or {}
            fetched_at = datetime.now(UTC).isoformat()

            for alias, row in aliases.items():
                error = errors_by_alias.get(alias)
                repository = data.get(alias)
                pr_data = (repository or {}).get("pullRequest") if isinstance(repository, dict) else None
                if error:
                    totals["errors"] += 1
                    store.insert_pipeline_error(
                        stage="fetch_closing_comments",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type=error.get("type", "graphql_error"),
                        message=error.get("message"),
                    )
                    store.replace_pr_closing_comments(int(row["id"]), [], fetched_at=fetched_at)
                    totals["prs_fetched"] += 1
                    continue
                if pr_data is None:
                    totals["errors"] += 1
                    store.insert_pipeline_error(
                        stage="fetch_closing_comments",
                        repo_owner=row["repo_owner"],
                        repo_name=row["repo_name"],
                        pr_number=row["pr_number"],
                        error_type="NOT_FOUND",
                        message="repository or pull request not found",
                    )
                    store.replace_pr_closing_comments(int(row["id"]), [], fetched_at=fetched_at)
                    totals["prs_fetched"] += 1
                    continue
                comments = _comment_nodes(pr_data)
                totals["comments_cached"] += store.replace_pr_closing_comments(
                    int(row["id"]),
                    comments,
                    fetched_at=fetched_at,
                )
                totals["prs_fetched"] += 1

            processed = totals["prs_fetched"] + totals["errors"]
            if next_progress is not None and processed >= next_progress:
                LOGGER.info(
                    "closing-comment fetch progress: fetched=%s errors=%s comments=%s "
                    "remaining=%s elapsed=%.1f",
                    totals["prs_fetched"],
                    totals["errors"],
                    totals["comments_cached"],
                    totals["rate_limit_remaining"],
                    time.monotonic() - started_at,
                )
                while next_progress is not None and processed >= next_progress:
                    next_progress += progress_every or 0

            if stop_remaining is not None and remaining is not None and remaining < stop_remaining:
                totals["stopped_for_rate_limit"] = True
                break
            _respect_rate_limit(response)

    totals["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
    return totals


def detect_supersession_from_comments(
    comments: list[dict[str, Any]] | str | None,
) -> SupersessionMatch | None:
    """Return the latest positive bot-comment supersession/workflow match."""
    parsed_comments = parse_comments(comments)
    for comment in reversed(parsed_comments):
        body = str(comment.get("body") or "")
        if not body or any(pattern.search(body) for pattern in EXCLUDE_PATTERNS):
            continue
        family = bot_family_for_author(
            comment.get("author_login"),
            comment.get("author_type"),
        )
        if family == "human":
            continue
        for pattern in COMMENT_PATTERNS:
            if pattern.bot_family != family:
                continue
            if pattern.regex.search(body):
                return SupersessionMatch(
                    reason=pattern.reason,
                    bot_family=family,
                    pattern_name=pattern.name,
                    comment_text=body,
                    created_at=comment.get("created_at"),
                )
    return None


def parse_comments(comments: list[dict[str, Any]] | str | None) -> list[dict[str, Any]]:
    """Normalize cached comment input from JSON text or Python objects."""
    if comments is None:
        return []
    if isinstance(comments, str):
        if not comments.strip():
            return []
        try:
            parsed = json.loads(comments)
        except json.JSONDecodeError:
            return []
    else:
        parsed = comments
    if not isinstance(parsed, list):
        return []
    return [comment for comment in parsed if isinstance(comment, dict)]


def bot_family_for_author(login: Any, author_type: Any = None) -> str:
    """Classify a comment author into a bot family or human."""
    login_text = str(login or "").lower()
    type_text = str(author_type or "").lower()
    normalized = login_text.removesuffix("[bot]")
    if normalized == "dependabot":
        return "dependabot"
    if normalized in {"renovate", "renovate-bot"}:
        return "renovate"
    if type_text == "bot" or login_text.endswith("[bot]") or normalized in CURATED_BOT_LOGINS:
        return "other-bot"
    return "human"


def write_supersession_unmatched(
    store: Store,
    output_path: str | Path = "data/supersession_unmatched.json",
    *,
    sample_size: int = 10,
    seed: int = 42,
) -> dict[str, Any]:
    """Write unmatched bot-comment coverage diagnostics for closed-unmerged PRs."""
    rng = random.Random(seed)
    by_family: dict[str, dict[str, Any]] = {}
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT
              pr.id AS pr_id,
              (
                SELECT COALESCE(
                  json_group_array(
                    json_object(
                      'author_login', c.author_login,
                      'author_type', c.author_type,
                      'body', c.body,
                      'created_at', c.created_at
                    )
                  ),
                  '[]'
                )
                FROM (
                  SELECT author_login, author_type, body, created_at
                  FROM pr_closing_comment
                  WHERE pr_id = pr.id
                  ORDER BY created_at ASC, id ASC
                ) AS c
              ) AS closing_comments_json
            FROM pr
            WHERE pr.closing_comments_fetched_at IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM change
                JOIN classification AS outcome ON outcome.change_id = change.id
                  AND outcome.dimension = 'outcome'
                  AND outcome.classifier_version = 1
                WHERE change.pr_id = pr.id
                  AND outcome.label = 'closed-unmerged'
              )
            """
        ).fetchall()

    for row in rows:
        comments = parse_comments(row["closing_comments_json"])
        bot_comments = [
            comment
            for comment in comments
            if bot_family_for_author(comment.get("author_login"), comment.get("author_type")) != "human"
        ]
        if not bot_comments or detect_supersession_from_comments(bot_comments):
            continue
        last_comment = bot_comments[-1]
        family = bot_family_for_author(last_comment.get("author_login"), last_comment.get("author_type"))
        bucket = by_family.setdefault(family, {"count": 0, "samples": []})
        bucket["count"] += 1
        sample = {
            "pr_id": row["pr_id"],
            "last_comment_text": _truncate(str(last_comment.get("body") or ""), 500),
            "created_at": last_comment.get("created_at"),
        }
        samples = bucket["samples"]
        if len(samples) < sample_size:
            samples.append(sample)
        else:
            index = rng.randint(0, bucket["count"] - 1)
            if index < sample_size:
                samples[index] = sample

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sample_size": sample_size,
        "seed": seed,
        "by_bot_family": by_family,
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _batched(rows: list[Any], size: int) -> list[list[Any]]:
    """Split rows into fixed-size GraphQL batches."""
    iterator = iter(rows)
    batches: list[list[Any]] = []
    while batch := list(islice(iterator, size)):
        batches.append(batch)
    return batches


def _build_comments_query(rows: list[Any]) -> tuple[str, dict[str, Any]]:
    """Build a batched GraphQL query for PR closing comments."""
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
                comments(last: {COMMENT_PAGE_SIZE}) {{
                  nodes {{
                    body
                    createdAt
                    author {{
                      __typename
                      login
                    }}
                  }}
                }}
              }}
            }}
            """
        )
    parts.append("}")
    return "\n".join(parts), aliases


def _comment_nodes(pr_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize GraphQL comment nodes into cache rows."""
    nodes = ((pr_data.get("comments") or {}).get("nodes") or [])
    comments = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        author = node.get("author") if isinstance(node.get("author"), dict) else {}
        body = node.get("body")
        if not isinstance(body, str):
            continue
        comments.append(
            {
                "body": body,
                "created_at": node.get("createdAt"),
                "author_login": author.get("login"),
                "author_type": author.get("__typename"),
            }
        )
    return comments


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


def _errors_by_alias(errors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group GraphQL errors by the alias in their path."""
    by_alias: dict[str, dict[str, Any]] = {}
    for error in errors:
        path = error.get("path") or []
        if path:
            by_alias[str(path[0])] = error
    return by_alias


def _rate_limit_remaining(response: httpx.Response) -> int | None:
    """Read the remaining GitHub API budget from response headers."""
    try:
        return int(response.headers["x-ratelimit-remaining"])
    except (KeyError, ValueError):
        return None


def _rate_limit_reset(response: httpx.Response) -> int | None:
    """Read the GitHub API reset epoch from response headers."""
    try:
        return int(response.headers["x-ratelimit-reset"])
    except (KeyError, ValueError):
        return None


def _respect_rate_limit(response: httpx.Response) -> None:
    """Sleep when the response indicates a low remaining rate limit."""
    remaining = _rate_limit_remaining(response)
    if remaining is None or remaining >= 100:
        return
    reset = _rate_limit_reset(response)
    if reset is None:
        return
    sleep_for = max(0, reset - int(time.time()))
    if sleep_for:
        LOGGER.info("GitHub rate limit low; sleeping %s seconds", sleep_for)
        time.sleep(sleep_for)


def _elapsed_exceeded(started_at: float, max_elapsed_seconds: int | None) -> bool:
    """Return whether the configured elapsed-time cap has been reached."""
    return max_elapsed_seconds is not None and time.monotonic() - started_at >= max_elapsed_seconds


def _truncate(text: str, limit: int) -> str:
    """Truncate long audit text without losing that truncation occurred."""
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
