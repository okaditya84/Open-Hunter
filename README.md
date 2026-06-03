# Open Hunter

Give it a list of company **names** (CSV or text). It finds each company's real
website, locates the careers source, pulls every open role, filters them
against *your* criteria with an LLM, and writes a clean CSV you can apply from
directly — one clickable `apply_url` per row.

It is built to be **honest and accurate** before exhaustive: every row is a
real, verified posting, and any company it can't fully resolve is reported in a
separate file rather than guessed at or silently dropped.

---

## How it works

```
company name
   └─ resolve → real website          (LLM + web search, or user-supplied)
        └─ discover careers source
             ├─ ATS link in page HTML
             ├─ guess-and-verify ATS probe   ← catches JS-embedded boards
             └─ careers-page crawl
                  ├─ ATS API client   ← preferred: structured, accurate, no bot walls
                  └─ generic LLM extractor   ← fallback for bespoke sites (Playwright)
   └─ de-duplicate → score relevance (inclusive) → CSV
```

### Why it's reliable
Most companies host jobs on a handful of **Applicant Tracking Systems** that
expose clean public JSON APIs. Open Hunter reads those APIs directly, which is
far more accurate than scraping and sidesteps bot-detection entirely (these
endpoints are *meant* to be read). Supported ATS APIs:

`Greenhouse · Lever · Ashby · SmartRecruiters · Recruitee · Workable · BambooHR · Workday`

For sites not on a known ATS, it renders the page with a headless browser
(Playwright) and uses the LLM to extract postings — but **only from links and
text actually present on the page**, so apply URLs are never fabricated.

### What it deliberately does *not* do
It does **not** crack CAPTCHAs, bypass human-verification, or evade bot
detection. When a site genuinely blocks automated access, that company is
marked `blocked` in the report. This keeps results trustworthy.

---

## Setup

```bash
cd "Open Hunter"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # for the deep-crawl fallback
cp .env.example .env                               # then edit .env
```

### Configure your LLM (any OpenAI-compatible provider)
Edit `.env` — only three variables matter:

```
LLM_BASE_URL=https://openrouter.ai/api/v1     # or Groq, Ollama, DeepSeek, Gemini-compat…
LLM_API_KEY=your-key-here
LLM_MODEL=deepseek/deepseek-chat              # whatever model your provider exposes
```

Verify it works:

```bash
.venv/bin/python run.py --check-llm
```

> The LLM powers company→website resolution, generic extraction, and relevance
> scoring. Without it, Open Hunter still runs in a limited mode (ATS sources via
> verified probing only, no relevance filtering).

---

## Usage

```bash
# Basic: a CSV of companies + what you're looking for
.venv/bin/python run.py data/companies_sample.csv \
    -c "Backend or platform engineer, Python/Go, 3+ years, remote or Bangalore"

# Criteria from a file
.venv/bin/python run.py companies.csv --criteria-file my_criteria.txt

# Test on the first 5 companies, no browser, more detail
.venv/bin/python run.py companies.csv -c "data engineer" --limit 5 --no-browser -v
```

### Input formats
**CSV** (header optional; recognised columns `name`/`company`, `website`, `notes`):
```csv
name,website,notes
Anthropic,,AI safety
Stripe,stripe.com,payments
```
**Text** (one per line, optional `, website`):
```
Anthropic
Stripe, stripe.com
```

### Output (in `output/`, timestamped)
- **`jobs_*.csv`** — the roles. Columns:
  `company_name, title, location, job_id, apply_url, posting_url, posted_date,
  employment_type, department, skills, relevance, relevance_score,
  relevance_reason, description, source`.
  Sorted most-relevant first. `relevance` is `match` / `maybe` / `no` (you chose
  inclusive matching, so borderline roles are kept as `maybe`). Non-matches are
  excluded unless you pass `--include-no`.
- **`report_*.csv`** — one row per company: status (`ok` / `no_jobs` /
  `unresolved` / `blocked` / `error`), the source used, counts, and an honest
  note for anything that didn't fully resolve.

---

## Key options
| Flag | Meaning |
|------|---------|
| `-c, --criteria` | What you want (titles, skills, seniority, location). Empty = keep all roles. |
| `--criteria-file` | Read criteria from a file. |
| `--include-no` | Also write roles judged not relevant. |
| `--limit N` | Only process the first N companies. |
| `--max-concurrency N` | Override `MAX_CONCURRENCY`. |
| `--no-browser` | Disable the Playwright fallback for this run. |
| `-v, --verbose` | Debug logging (also written to `logs/open_hunter.log`). |
| `--check-llm` | Test the LLM connection and exit. |

## Tuning (`.env`)
`RESPECT_ROBOTS`, `REQUEST_DELAY_SECONDS`, `HTTP_TIMEOUT`, `ENABLE_BROWSER`,
`MAX_CONCURRENCY`, `USER_AGENT`. See `.env.example` for details.

---

## Honest limitations
- A company that posts **only** on LinkedIn/Indeed, or behind a login, or on an
  ATS not yet supported, will show as `unresolved`/`no_jobs` — by design, rather
  than inventing data. Adding a new ATS is a small, self-contained client.
- Relevance is an LLM judgement; inclusive mode favours recall, so skim the
  `maybe` rows.
- Generic (non-ATS) extraction depends on page quality and the LLM; ATS sources
  are the gold standard.
