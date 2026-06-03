"""Load the user's company list from CSV or a plain text file.

Accepted formats:
  * .csv with a header — recognised columns: name/company/company_name,
    website/url/site, notes. Without a header, the first column is the name.
  * .txt — one company per line. A line may optionally include a website
    after a comma, e.g. "Acme Corp, acme.com".
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Optional

from .logging_util import get_logger
from .models import CompanyInput

log = get_logger(__name__)

_NAME_KEYS = {"name", "company", "company_name", "companies", "employer"}
_SITE_KEYS = {"website", "url", "site", "homepage", "domain", "web"}
_NOTE_KEYS = {"notes", "note", "comment", "comments"}


def load_companies(path: str) -> List[CompanyInput]:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {p}")

    if p.suffix.lower() == ".csv":
        return _load_csv(p)
    return _load_text(p)


def _load_csv(p: Path) -> List[CompanyInput]:
    with p.open(newline="", encoding="utf-8-sig") as f:
        # Header detection: trust recognised column names over csv.Sniffer,
        # which is unreliable on small files.
        first_row = next(csv.reader(f), [])
        f.seek(0)
        recognised = _NAME_KEYS | _SITE_KEYS | _NOTE_KEYS
        has_header = any(
            (cell or "").strip().lower() in recognised for cell in first_row
        )

        if has_header:
            reader = csv.DictReader(f)
            companies = []
            name_key = _match_key(reader.fieldnames, _NAME_KEYS)
            site_key = _match_key(reader.fieldnames, _SITE_KEYS)
            note_key = _match_key(reader.fieldnames, _NOTE_KEYS)
            for row in reader:
                name = (row.get(name_key) if name_key else None) or _first_value(row)
                name = (name or "").strip()
                if not name:
                    continue
                companies.append(
                    CompanyInput(
                        name=name,
                        website=_clean(row.get(site_key)) if site_key else None,
                        notes=_clean(row.get(note_key)) if note_key else None,
                    )
                )
            return _dedupe(companies)

        # No header: first column = name, optional second = website.
        f.seek(0)
        reader2 = csv.reader(f)
        companies = []
        for row in reader2:
            if not row or not row[0].strip():
                continue
            companies.append(
                CompanyInput(
                    name=row[0].strip(),
                    website=row[1].strip() if len(row) > 1 and row[1].strip() else None,
                )
            )
        return _dedupe(companies)


def _load_text(p: Path) -> List[CompanyInput]:
    companies = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        website = None
        if "," in line:
            left, right = line.split(",", 1)
            right = right.strip()
            if _looks_like_site(right):
                line, website = left.strip(), right
        companies.append(CompanyInput(name=line, website=website))
    return _dedupe(companies)


def _match_key(fieldnames, keys) -> Optional[str]:
    if not fieldnames:
        return None
    for fn in fieldnames:
        if fn and fn.strip().lower() in keys:
            return fn
    return None


def _first_value(row: dict) -> Optional[str]:
    for v in row.values():
        if v and str(v).strip():
            return str(v).strip()
    return None


def _clean(v) -> Optional[str]:
    v = (v or "").strip()
    return v or None


def _looks_like_site(s: str) -> bool:
    s = s.lower()
    return s.startswith(("http://", "https://", "www.")) or (
        "." in s and " " not in s
    )


def _dedupe(companies: List[CompanyInput]) -> List[CompanyInput]:
    seen = set()
    out = []
    for c in companies:
        key = c.name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out
