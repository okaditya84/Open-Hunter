"""CLI for the auto-apply agent: `python apply.py output/jobs_*.csv [opts]`."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from rich.console import Console

from ..config import load_config
from ..llm import LLMClient, LLMError
from ..logging_util import get_logger, setup_logging
from .profile import load_profile
from .runner import ApplyRunner

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="open-hunter-apply",
        description="Fill (and, with approval, submit) job applications from a "
                    "jobs CSV produced by Open Hunter.",
    )
    p.add_argument("jobs_csv", help="Path to a jobs_*.csv from the pipeline.")
    p.add_argument(
        "--mode", choices=["review", "draft", "auto"], default="review",
        help="review = fill then pause for your one-click submit (default); "
             "draft = fill + screenshot, never submit; "
             "auto = fill and submit automatically (use with caution).",
    )
    p.add_argument("--limit", type=int, default=0, help="Only the first N jobs.")
    p.add_argument("--match-only", action="store_true",
                   help="Only jobs with relevance == 'match'.")
    p.add_argument("--company", help="Only jobs from this company (substring).")
    p.add_argument("--headless", action="store_true",
                   help="Run the browser headless (for draft/testing only).")
    p.add_argument("--allow-resubmit", action="store_true",
                   help="Re-apply even to jobs already in the applied ledger "
                        "(by default those are skipped so you never apply twice).")
    p.add_argument("--answers", help="Path to apply_answers.json.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _load_jobs(path: str, match_only: bool, company: str) -> list:
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    jobs = []
    for r in rows:
        if match_only and r.get("relevance") != "match":
            continue
        if company and company.lower() not in (r.get("company_name", "").lower()):
            continue
        if not r.get("apply_url"):
            continue
        jobs.append(r)
    return jobs


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    setup_logging(config.logs_dir, verbose=args.verbose)
    log = get_logger("open_hunter.apply")

    if not Path(args.jobs_csv).exists():
        console.print(f"[red]Jobs CSV not found:[/red] {args.jobs_csv}")
        return 2

    profile = load_profile(answers_path=args.answers)
    if not profile.full_name:
        console.print("[red]No applicant profile.[/red] Fill data/apply_answers.json.")
        return 2

    llm = None
    if config.llm_configured:
        try:
            llm = LLMClient(config)
        except LLMError as exc:
            console.print(f"[yellow]LLM disabled:[/yellow] {exc}")

    jobs = _load_jobs(args.jobs_csv, args.match_only, args.company or "")
    if not jobs:
        console.print("[yellow]No matching jobs in that CSV.[/yellow]")
        return 1

    todo = profile.unconfirmed_fields()
    console.print(
        f"[bold]Auto-apply[/bold] — {len(jobs)} jobs, mode=[cyan]{args.mode}[/cyan], "
        f"applicant: {profile.full_name}"
    )
    if todo:
        console.print(
            f"[yellow]⚠ Unconfirmed profile fields (fix in apply_answers.json "
            f"before real submits):[/yellow] {', '.join(todo)}"
        )
    if args.mode == "auto":
        console.print("[red]AUTO mode will SUBMIT without review. Ctrl+C to abort.[/red]")

    runner = ApplyRunner(config, profile, llm)
    run_dir = runner.run(jobs, mode=args.mode, headless=args.headless,
                         limit=args.limit, allow_resubmit=args.allow_resubmit)
    console.print(f"\n[green]Done.[/green] Logs, screenshots & answers: {run_dir}")
    log.info("apply complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
