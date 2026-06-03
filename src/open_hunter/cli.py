"""Command-line interface for Open Hunter."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import load_config
from .input_loader import load_companies
from .llm import LLMClient, LLMError
from .logging_util import get_logger, setup_logging
from .pipeline import Pipeline
from .writer import write_all

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="open-hunter",
        description=(
            "Find relevant, real job postings for a list of companies and "
            "write them to a CSV you can apply from directly."
        ),
    )
    p.add_argument(
        "input",
        nargs="?",
        help="Path to a .csv or .txt file of company names (and optional sites).",
    )
    p.add_argument(
        "-c", "--criteria",
        default="",
        help="What you're looking for: titles, skills, seniority, location...",
    )
    p.add_argument(
        "--criteria-file",
        help="Read the criteria text from a file instead of --criteria.",
    )
    p.add_argument("--output-dir", help="Where to write the CSVs.")
    p.add_argument(
        "--include-no",
        action="store_true",
        help="Also include jobs judged not relevant in the jobs CSV.",
    )
    p.add_argument(
        "--limit", type=int, help="Only process the first N companies (testing)."
    )
    p.add_argument(
        "--max-concurrency", type=int, help="Override MAX_CONCURRENCY."
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Disable the Playwright fallback for this run.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    p.add_argument(
        "--check-llm",
        action="store_true",
        help="Test the configured LLM connection and exit.",
    )
    return p


def _apply_overrides(config, args):
    import dataclasses

    changes = {}
    if args.output_dir:
        out = Path(args.output_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        changes["output_dir"] = out
    if args.max_concurrency:
        changes["max_concurrency"] = max(1, args.max_concurrency)
    if args.no_browser:
        changes["enable_browser"] = False
    return dataclasses.replace(config, **changes) if changes else config


def _check_llm(config) -> int:
    if not config.llm_configured:
        console.print(
            "[red]LLM not configured.[/red] Set LLM_BASE_URL, LLM_API_KEY and "
            "LLM_MODEL in your .env file."
        )
        return 1
    try:
        client = LLMClient(config)
        reply = client.chat(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=10,
        )
        console.print(
            f"[green]LLM OK[/green] — provider responded: {reply!r}\n"
            f"  base_url: {config.llm_base_url}\n  model: {config.llm_model}"
        )
        return 0
    except (LLMError, Exception) as exc:  # noqa: BLE001
        console.print(f"[red]LLM check failed:[/red] {exc}")
        return 1


def _summary(results) -> None:
    table = Table(title="Open Hunter — run summary")
    table.add_column("Company", overflow="fold")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Jobs", justify="right")
    table.add_column("Kept", justify="right")
    status_color = {
        "ok": "green",
        "no_jobs": "yellow",
        "unresolved": "yellow",
        "blocked": "red",
        "error": "red",
    }
    for r in sorted(results, key=lambda r: r.company.name.lower()):
        kept = sum(1 for j in r.jobs if j.relevance != "no")
        color = status_color.get(r.status, "white")
        table.add_row(
            r.company.name,
            f"[{color}]{r.status}[/{color}]",
            r.company.source or "-",
            str(len(r.jobs)),
            str(kept),
        )
    console.print(table)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    setup_logging(config.logs_dir, verbose=args.verbose)
    log = get_logger("open_hunter.cli")

    config = _apply_overrides(config, args)

    if args.check_llm:
        return _check_llm(config)

    if not args.input:
        console.print("[red]Error:[/red] no input file given. See --help.")
        return 2

    criteria = args.criteria
    if args.criteria_file:
        criteria = Path(args.criteria_file).expanduser().read_text(
            encoding="utf-8"
        )

    try:
        companies = load_companies(args.input)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Error loading input:[/red] {exc}")
        return 2

    if args.limit:
        companies = companies[: args.limit]

    if not companies:
        console.print("[yellow]No companies found in the input file.[/yellow]")
        return 1

    console.print(
        f"[bold]Open Hunter[/bold] — {len(companies)} companies, "
        f"criteria: {criteria.strip()[:80] or '(none — keeping all roles)'}"
    )
    if not config.llm_configured:
        console.print(
            "[yellow]Heads up:[/yellow] no LLM configured — running in "
            "limited mode (ATS sources only, no relevance scoring)."
        )

    pipeline = Pipeline(config)
    try:
        results = pipeline.run(companies, criteria)
    finally:
        pipeline.close()

    jobs_path, report_path = write_all(
        config.output_dir, results, include_no=args.include_no
    )

    _summary(results)
    total_kept = sum(
        1 for r in results for j in r.jobs if j.relevance != "no"
    )
    console.print(
        f"\n[bold green]Done.[/bold green] {total_kept} relevant roles.\n"
        f"  Jobs CSV:   {jobs_path}\n"
        f"  Run report: {report_path}"
    )
    log.info("run complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
