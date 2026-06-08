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
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_region: str
    llm_max_tokens: int
    llm_temperature: float

    # --- Crawl behaviour ---
    respect_robots: bool
    request_delay_seconds: float
    http_timeout: float
    enable_browser: bool
    max_concurrency: int
    matcher_concurrency: int
    user_agent: str

    # --- Vision (for the apply agent's visual form QA) ---
    vision_model: str
    enable_vision: bool

    # --- Paths ---
    output_dir: Path
    logs_dir: Path

    @property
    def is_bedrock(self) -> bool:
        return self.llm_provider == "bedrock"

    @property
    def llm_configured(self) -> bool:
        """True only when we have everything needed to call the LLM."""
        if self.is_bedrock:
            # Native Bedrock (Converse) needs a key, model and region only.
            return bool(self.llm_api_key and self.llm_model and self.llm_region)
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


def _resolve_llm() -> tuple:
    """Resolve (provider, base_url, api_key, region) across providers.

    For OpenAI-compatible providers, only base_url/api_key/model matter.
    For Amazon Bedrock we use the native Converse API (full model catalog,
    including Claude), so we mainly need a region + bearer key:
      * region from LLM_REGION / AWS_REGION (default us-east-1),
      * api_key falls back to the standard AWS_BEARER_TOKEN_BEDROCK env var.
    """
    provider = (os.getenv("LLM_PROVIDER", "").strip().lower() or "openai")
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    region = ""

    if provider == "bedrock":
        region = (
            os.getenv("LLM_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-1"
        ).strip()
        if not api_key:
            api_key = os.getenv("AWS_BEARER_TOKEN_BEDROCK", "").strip()

    return provider, base_url, api_key, region


def load_config() -> Config:
    output_dir = PROJECT_ROOT / "output"
    logs_dir = PROJECT_ROOT / "logs"
    output_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)

    provider, base_url, api_key, region = _resolve_llm()

    return Config(
        llm_provider=provider,
        llm_base_url=base_url,
        llm_api_key=api_key,
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_region=region,
        llm_max_tokens=_get_int("LLM_MAX_TOKENS", 2048),
        llm_temperature=_get_float("LLM_TEMPERATURE", 0.0),
        respect_robots=_get_bool("RESPECT_ROBOTS", True),
        request_delay_seconds=_get_float("REQUEST_DELAY_SECONDS", 1.0),
        http_timeout=_get_float("HTTP_TIMEOUT", 25.0),
        enable_browser=_get_bool("ENABLE_BROWSER", True),
        max_concurrency=_get_int("MAX_CONCURRENCY", 4),
        matcher_concurrency=_get_int("MATCHER_CONCURRENCY", 8),
        vision_model=os.getenv("VISION_MODEL", "qwen/qwen3.5-9b").strip(),
        enable_vision=_get_bool("ENABLE_VISION", True),
        user_agent=os.getenv(
            "USER_AGENT", "OpenHunter/1.0 (+job-search-assistant)"
        ).strip(),
        output_dir=output_dir,
        logs_dir=logs_dir,
    )
