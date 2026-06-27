"""Click command line interface for deptracker."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import click
from dotenv import load_dotenv

from deptracker.alignment import compute_alignment as run_compute_alignment
from deptracker.classify import classify as run_classify
from deptracker.discover import discover as run_discover
from deptracker.diagnose import diagnose as run_diagnose
from deptracker.enrich import enrich as run_enrich
from deptracker.gold import summarize_gold_set
from deptracker.parse import parse as run_parse
from deptracker.sq1_analysis import analyze_sq1_effects
from deptracker.sq1_analysis import format_sq1_summary
from deptracker.sq2_strata import analyze_sq2_strata
from deptracker.sq2_strata import format_sq2_summary
from deptracker.store import Store
from deptracker.supersession import fetch_closing_comments as run_fetch_closing_comments
from deptracker.supersession import write_supersession_unmatched

DEFAULT_DB_PATH = Path("./data/deptracker.sqlite")
ECOSYSTEM_CHOICES = {"maven", "npm", "cargo", "pip", "go"}


def _parse_ecosystem_priority(value: str | None) -> set[str] | None:
    """Parse a comma-separated ecosystem priority list."""
    if not value:
        return None
    ecosystems = {part.strip() for part in value.split(",") if part.strip()}
    invalid = ecosystems - ECOSYSTEM_CHOICES
    if invalid:
        raise click.ClickException(
            "invalid ecosystem(s) for --ecosystem-priority: "
            + ", ".join(sorted(invalid))
        )
    return ecosystems


def _parse_priority_targets(value: str | None) -> dict[str, int] | None:
    """Parse ecosystem target specs such as maven:50,pip:150."""
    if not value:
        return None
    targets: dict[str, int] = {}
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            raise click.ClickException(
                f"invalid --stop-when-priority-targets-met entry: {item}"
            )
        ecosystem, target_text = item.split(":", 1)
        ecosystem = ecosystem.strip()
        if ecosystem not in ECOSYSTEM_CHOICES:
            raise click.ClickException(f"invalid priority target ecosystem: {ecosystem}")
        try:
            target = int(target_text)
        except ValueError as exc:
            raise click.ClickException(
                f"invalid priority target count for {ecosystem}: {target_text}"
            ) from exc
        if target < 0:
            raise click.ClickException(f"priority target for {ecosystem} must be >= 0")
        targets[ecosystem] = target
    return targets or None


@click.group()
def main() -> None:
    """Top-level deptracker CLI group."""
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


@main.command()
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
def init(db_path: Path) -> None:
    """Initialize the SQLite database schema."""
    store = Store(db_path)
    store.init_schema()
    click.echo(f"initialized {db_path}")


@main.command()
@click.option("--date", required=True)
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option("--limit", type=int)
def discover(date: str, db_path: Path, limit: int | None) -> None:
    """Discover candidate PR events for one GH Archive day."""
    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id:
        raise click.ClickException("GCP_PROJECT_ID must be set")

    store = Store(db_path)
    store.init_schema()
    inserted = run_discover(project_id=project_id, date=date, store=store, limit=limit)
    click.echo(f"inserted {inserted} candidate PRs")


@main.command()
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option("--batch-size", default=25, show_default=True, type=int)
@click.option("--max-prs", type=int)
@click.option("--stop-remaining", type=int)
@click.option("--max-elapsed-seconds", type=int)
@click.option("--progress-every", type=int)
@click.option("--ecosystem-priority")
@click.option("--stop-when-priority-targets-met")
def enrich(
    db_path: Path,
    batch_size: int,
    max_prs: int | None,
    stop_remaining: int | None,
    max_elapsed_seconds: int | None,
    progress_every: int | None,
    ecosystem_priority: str | None,
    stop_when_priority_targets_met: str | None,
) -> None:
    """Enrich pending PRs from the GitHub GraphQL API."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise click.ClickException("GITHUB_TOKEN must be set")

    store = Store(db_path)
    store.init_schema()
    result = run_enrich(
        store=store,
        token=token,
        batch_size=batch_size,
        max_prs=max_prs,
        stop_remaining=stop_remaining,
        max_elapsed_seconds=max_elapsed_seconds,
        progress_every=progress_every,
        ecosystem_priority=_parse_ecosystem_priority(ecosystem_priority),
        stop_when_priority_targets_met=_parse_priority_targets(
            stop_when_priority_targets_met
        ),
    )
    click.echo(json.dumps(result, indent=2))


@main.command("fetch-closing-comments")
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option("--batch-size", default=25, show_default=True, type=int)
@click.option("--max-prs", type=int)
@click.option("--stop-remaining", default=200, show_default=True, type=int)
@click.option("--max-elapsed-seconds", type=int)
@click.option("--progress-every", type=int)
@click.option(
    "--unmatched-output",
    default=Path("data/supersession_unmatched.json"),
    type=click.Path(path_type=Path),
)
def fetch_closing_comments(
    db_path: Path,
    batch_size: int,
    max_prs: int | None,
    stop_remaining: int | None,
    max_elapsed_seconds: int | None,
    progress_every: int | None,
    unmatched_output: Path,
) -> None:
    """Fetch bot closing comments for closed-unmerged PRs."""
    store = Store(db_path)
    store.init_schema()
    result = run_fetch_closing_comments(
        store=store,
        token=os.getenv("GITHUB_TOKEN"),
        batch_size=batch_size,
        max_prs=max_prs,
        stop_remaining=stop_remaining,
        max_elapsed_seconds=max_elapsed_seconds,
        progress_every=progress_every,
    )
    result["unmatched"] = write_supersession_unmatched(
        store,
        output_path=unmatched_output,
    )
    click.echo(json.dumps(result, indent=2))


@main.command()
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option("--max-prs", type=int)
@click.option("--stop-remaining", type=int)
@click.option("--include-deferred", is_flag=True)
@click.option("--max-elapsed-seconds", type=int)
@click.option("--progress-every", type=int)
@click.option("--retry-errors", is_flag=True)
def parse(
    db_path: Path,
    max_prs: int | None,
    stop_remaining: int | None,
    include_deferred: bool,
    max_elapsed_seconds: int | None,
    progress_every: int | None,
    retry_errors: bool,
) -> None:
    """Parse dependency changes from enriched PR diffs."""
    token = os.getenv("GITHUB_TOKEN", "")
    store = Store(db_path)
    store.init_schema()
    result = run_parse(
        store=store,
        token=token,
        max_prs=max_prs,
        stop_remaining=stop_remaining,
        include_deferred=include_deferred,
        max_elapsed_seconds=max_elapsed_seconds,
        progress_every=progress_every,
        retry_errors=retry_errors,
    )
    click.echo(json.dumps(result, indent=2))


@main.command()
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
def stats(db_path: Path) -> None:
    """Print a compact summary of PR pipeline counts."""
    store = Store(db_path)
    store.init_schema()
    click.echo(json.dumps(store.pr_counts_by_stage(), indent=2))


@main.command()
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option(
    "--output",
    "output_path",
    default=Path("data/diagnose.json"),
    type=click.Path(path_type=Path),
)
def diagnose(db_path: Path, output_path: Path) -> None:
    """Write the full diagnostics report to JSON."""
    store = Store(db_path)
    result = run_diagnose(store, output_path=output_path)
    click.echo(json.dumps(result, indent=2))


@main.command()
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option("--force-dimensions")
def classify(db_path: Path, force_dimensions: str | None) -> None:
    """Run the heuristic classifier for pending changes."""
    store = Store(db_path)
    store.init_schema()
    dimensions = (
        [dimension.strip() for dimension in force_dimensions.split(",") if dimension.strip()]
        if force_dimensions
        else None
    )
    result = run_classify(store, force_dimensions=dimensions)
    click.echo(json.dumps(result, indent=2))




@main.command("label-summary")
@click.option(
    "--gold-file",
    "gold_file",
    default=Path("data/gold_set.jsonl"),
    type=click.Path(path_type=Path),
)
def label_summary(gold_file: Path) -> None:
    """Summarize the gold-set JSONL file."""
    result = summarize_gold_set(gold_file=gold_file)
    click.echo(json.dumps(result, indent=2))



@main.command("compute-alignment")
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
def compute_alignment(db_path: Path) -> None:
    """Compute the SQ2 alignment dataset."""
    store = Store(db_path)
    store.init_schema()
    result = run_compute_alignment(store)
    click.echo(json.dumps(result, indent=2))


@main.command("sq1-effects")
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option(
    "--output",
    "output_path",
    default=Path("data/sq1_effect_sizes.json"),
    type=click.Path(path_type=Path),
)
def sq1_effects(db_path: Path, output_path: Path) -> None:
    """Compute SQ1 Cramer's V effect sizes."""
    store = Store(db_path)
    store.init_schema()
    result = analyze_sq1_effects(store, output_path=output_path)
    click.echo(format_sq1_summary(result))


@main.command("sq2-strata")
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, type=click.Path(path_type=Path))
@click.option(
    "--output",
    "output_path",
    default=Path("data/sq2_strata.json"),
    type=click.Path(path_type=Path),
)
@click.option("--min-k", "min_k", default=5, show_default=True, type=int)
def sq2_strata(db_path: Path, output_path: Path, min_k: int) -> None:
    """Compute stratified SQ2 alignment summaries."""
    store = Store(db_path)
    store.init_schema()
    result = analyze_sq2_strata(
        store,
        output_path=output_path,
        min_k_projects_decided=min_k,
    )
    click.echo(format_sq2_summary(result))
