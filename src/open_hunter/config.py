"""Central configuration, loaded once from the environment / .env file.

Everything tunable lives here so the rest of the code never reads os.environ
directly. The LLM layer is fully provider-agnostic: it only needs a base URL,
an API key, and a model id, which means any OpenAI-compatible provider works
without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root = two levels up from this file (src/open_hunter/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load .env from the project root if present. Real environment variables
# always win over the file, which is the behaviour we want.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # --- LLM ---
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_max_tokens: int
    llm_temperature: float

    # --- Crawl behaviour ---
    respect_robots: bool
    request_delay_seconds: float
    http_timeout: float
    enable_browser: bool
    max_concurrency: int
    user_agent: str

    # --- Paths ---
    output_dir: Path
    logs_dir: Path

    @property
    def llm_configured(self) -> bool:
        """True only when we have everything needed to call the LLM."""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


def load_config() -> Config:
    output_dir = PROJECT_ROOT / "output"
    logs_dir = PROJECT_ROOT / "logs"
    output_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    return Config(
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_max_tokens=_get_int("LLM_MAX_TOKENS", 2048),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.0),
        respect_robots=_get_bool("RESPECT_ROBOTS", True),
        request_delay_seconds=_get_float("REQUEST_DELAY_SECONDS", 1.0),
        http_timeout=_get_float("HTTP_TIMEOUT", 25.0),
        enable_browser=_get_bool("ENABLE_BROWSER", True),
        max_concurrency=_get_int("MAX_CONCURRENCY", 4),
        user_agent=os.getenv(
            "USER_AGENT", "OpenHunter/1.0 (+job-search-assistant)"
        ).strip(),
        output_dir=output_dir,
        logs_dir=logs_dir,
    )
