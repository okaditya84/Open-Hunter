"""Load, extract, and compile a candidate's profile from resumes, LinkedIn, and GitHub.

Caches the compiled profile in data/candidate_profile.json to avoid repeating LLM
calls and allowing manual editing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

from pypdf import PdfReader

from .config import Config
from .llm import LLMClient, LLMError
from .logging_util import get_logger

log = get_logger(__name__)

_PROFILE_SYSTEM = (
    "You are an expert career assistant. Analyze a candidate's resume/LinkedIn text "
    "and a list of their GitHub repositories. Synthesize them into a structured, "
    "clean candidate profile in JSON format. Be accurate and honest about their "
    "experience level. Do not inflate internships to full-time years unless "
    "specified.\n"
    "\n"
    "Respond ONLY with a JSON object containing these keys:\n"
    "  \"name\": string,\n"
    "  \"skills\": list of strings (primary technical skills and tools),\n"
    "  \"experience_years\": float (total years of software/ML/professional experience),\n"
    "  \"experience_summary\": string (short summary of key roles, internships, and timeline),\n"
    "  \"projects\": list of objects (each with \"name\", \"description\", and \"technologies\" list),\n"
    "  \"target_roles\": list of strings (roles they are qualified for based on their background),\n"
    "  \"experience_level_restriction\": string (explicit instructions for filtering senior/entry roles, "
    "e.g. '0-2 years of experience; reject any senior, lead, staff, or manager positions; target entry/fresher/junior roles')\n"
)


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract plain text from a PDF file."""
    try:
        reader = PdfReader(pdf_path)
        text = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text.append(t)
        return "\n".join(text)
    except Exception as exc:
        log.warning("Failed to extract text from %s: %s", pdf_path, exc)
        return ""


def extract_github_username(text: str) -> Optional[str]:
    """Search for a GitHub profile link in the text and return the username."""
    # Matches github.com/username (ignoring common query params/tabs)
    m = re.search(r"github\.com/([a-zA-Z0-9_-]+)", text, re.IGNORECASE)
    if m:
        username = m.group(1).strip()
        # Exclude common noise or generic words
        if username.lower() not in {"settings", "search", "explore", "features", "about", "join"}:
            return username
    return None


def fetch_github_repos(username: str) -> List[Dict[str, Any]]:
    """Fetch user's public repositories, filter forks, and collect basic info."""
    url = f"https://api.github.com/users/{username}/repos?per_page=100"
    headers = {"User-Agent": "OpenHunter/1.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            log.warning("GitHub API returned status %d for %s", resp.status_code, username)
            return []
        
        repos = resp.json()
        non_forks = [r for r in repos if not r.get("fork")]
        
        # Sort by updated_at or stars to get the most relevant first
        non_forks.sort(key=lambda r: r.get("stargazers_count", 0), reverse=True)
        
        compiled_repos = []
        # Limit the number of README fetches to avoid rate limits
        for r in non_forks[:12]:
            repo_info = {
                "name": r.get("name"),
                "description": r.get("description") or "",
                "language": r.get("language") or "",
                "stars": r.get("stargazers_count", 0),
            }
            
            # Attempt to fetch the README
            readme_text = ""
            for branch in ("main", "master"):
                readme_url = f"https://raw.githubusercontent.com/{username}/{r['name']}/{branch}/README.md"
                try:
                    r_resp = requests.get(readme_url, headers=headers, timeout=5)
                    if r_resp.status_code == 200:
                        readme_text = r_resp.text
                        break
                except Exception:
                    continue
            
            if readme_text:
                # Keep only a summary of the README to avoid huge token usage
                readme_lines = readme_text.split("\n")
                summary_lines = []
                for line in readme_lines[:30]:
                    if line.strip() and not line.startswith("[!"):
                        summary_lines.append(line)
                repo_info["readme_snippet"] = "\n".join(summary_lines)[:1000]
            
            compiled_repos.append(repo_info)
            
        return compiled_repos
    except Exception as exc:
        log.warning("Failed to fetch GitHub repositories for %s: %s", username, exc)
        return []


def load_candidate_profile(
    config: Config,
    profile_dir: Optional[Path] = None,
    github_user: Optional[str] = None,
    llm: Optional[LLMClient] = None,
) -> Optional[Dict[str, Any]]:
    """Orchestrate loading of the candidate profile.
    
    Checks for a cached profile first, otherwise builds one from scratch.
    """
    if profile_dir is None:
        profile_dir = config.output_dir.parent / "data"

    cache_path = profile_dir / "candidate_profile.json"
    if cache_path.exists():
        try:
            profile_data = json.loads(cache_path.read_text(encoding="utf-8"))
            log.info("Loaded cached candidate profile from %s", cache_path)
            return profile_data
        except Exception as exc:
            log.warning("Failed to load cached profile from %s: %s", cache_path, exc)

    # Cache missed, extract from PDFs
    if not profile_dir.exists():
        log.warning("Profile directory %s does not exist; skipping profile parsing.", profile_dir)
        return None

    pdf_files = list(profile_dir.glob("*.pdf"))
    if not pdf_files:
        log.info("No PDF files found in %s; skipping profile parsing.", profile_dir)
        return None

    log.info("Parsing candidate documents in %s...", profile_dir)
    pdf_texts = []
    for pdf_path in pdf_files:
        log.debug("Reading PDF: %s", pdf_path.name)
        text = extract_text_from_pdf(pdf_path)
        if text:
            pdf_texts.append(f"--- Document: {pdf_path.name} ---\n{text}")

    if not pdf_texts:
        log.warning("Could not extract text from any PDF in %s.", profile_dir)
        return None

    combined_text = "\n\n".join(pdf_texts)

    # Try to extract GitHub username if not provided
    if not github_user:
        github_user = extract_github_username(combined_text)
        if github_user:
            log.info("Extracted GitHub username from documents: %s", github_user)

    github_repos = []
    if github_user:
        log.info("Fetching public repositories for GitHub user '%s'...", github_user)
        github_repos = fetch_github_repos(github_user)

    # Compile the final profile with the LLM
    if not llm:
        log.warning("LLM client not available; cannot synthesize candidate profile.")
        return None

    log.info("Synthesizing candidate profile using LLM...")
    user_payload = {
        "resume_texts": combined_text[:30000],  # Protect token limits
        "github_repositories": github_repos
    }

    try:
        profile = llm.complete_json(_PROFILE_SYSTEM, json.dumps(user_payload, ensure_ascii=False))
        if isinstance(profile, dict):
            # Save cache
            cache_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("Saved synthesized candidate profile to cache: %s", cache_path)
            return profile
        else:
            log.warning("LLM returned invalid profile format.")
            return None
    except (LLMError, Exception) as exc:
        log.error("Failed to synthesize candidate profile: %s", exc)
        return None
