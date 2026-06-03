"""Write results to CSV.

Two files per run, both timestamped:
  * jobs_*.csv    — the roles you can apply to (one clickable apply_url/row).
  * report_*.csv  — one row per company: what we found, the source, and an
                    honest note for anything unresolved or blocked.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Tuple

from .logging_util import get_logger
from .models import JOB_CSV_COLUMNS, CompanyResult, Job

log = get_logger(__name__)

_REPORT_COLUMNS = [
    "company_name",
    "status",
    "website",
    "careers_url",
    "source",
    "jobs_found",
    "jobs_kept",
    "resolution_confidence",
    "note",
]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_jobs(
    output_dir: Path,
    jobs: Iterable[Job],
    *,
    include_no: bool = False,
    stamp: str = "",
) -> Path:
    stamp = stamp or _stamp()
    path = output_dir / f"jobs_{stamp}.csv"
    rows = [
        j for j in jobs if include_no or j.relevance != "no"
    ]
    # Most relevant first, then by company.
    rows.sort(
        key=lambda j: (
            {"match": 0, "maybe": 1, "unknown": 2, "no": 3}.get(j.relevance, 4),
            -j.relevance_score,
            j.company_name.lower(),
        )
    )
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=JOB_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for j in rows:
            writer.writerow(j.as_row())
    log.info("wrote %d jobs to %s", len(rows), path)
    return path


def write_report(
    output_dir: Path,
    results: List[CompanyResult],
    *,
    stamp: str = "",
) -> Path:
    stamp = stamp or _stamp()
    path = output_dir / f"report_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_REPORT_COLUMNS)
        writer.writeheader()
        for r in results:
            kept = sum(1 for j in r.jobs if j.relevance != "no")
            writer.writerow(
                {
                    "company_name": r.company.name,
                    "status": r.status,
                    "website": r.company.website or "",
                    "careers_url": r.company.careers_url or "",
                    "source": r.company.source or "",
                    "jobs_found": len(r.jobs),
                    "jobs_kept": kept,
                    "resolution_confidence": round(
                        r.company.resolution_confidence, 2
                    ),
                    "note": r.error or r.company.resolution_note or "",
                }
            )
    log.info("wrote run report to %s", path)
    return path


def write_all(
    output_dir: Path,
    results: List[CompanyResult],
    *,
    include_no: bool = False,
) -> Tuple[Path, Path]:
    stamp = _stamp()
    all_jobs: List[Job] = []
    for r in results:
        all_jobs.extend(r.jobs)
    jobs_path = write_jobs(
        output_dir, all_jobs, include_no=include_no, stamp=stamp
    )
    report_path = write_report(output_dir, results, stamp=stamp)
    return jobs_path, report_path
