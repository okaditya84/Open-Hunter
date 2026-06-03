"""Provider-agnostic LLM layer.

Any OpenAI-compatible endpoint works (OpenRouter, Groq, Ollama, DeepSeek,
Gemini's OpenAI-compatible API, Together, ...). We never hard-code a vendor:
the base URL, key, and model all come from configuration.

Two call styles are exposed:
  * chat()        -> free-form text
  * complete_json -> parsed JSON, with defensive extraction so a chatty model
                     that wraps JSON in prose or code fences still works.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from openai import OpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config
from .logging_util import get_logger

log = get_logger(__name__)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, config: Config):
        if not config.llm_configured:
            raise LLMError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY and "
                "LLM_MODEL in your .env file."
            )
        self.config = config
        self.client = OpenAI(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
        )

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        reraise=True,
    )
    def chat(
        self,
        messages: List[dict],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> str:
        kwargs: dict = {
            "model": self.config.llm_model,
            "messages": messages,
            "temperature": (
                self.config.llm_temperature
                if temperature is None
                else temperature
            ),
            "max_tokens": max_tokens or self.config.llm_max_tokens,
        }
        # response_format is honoured by many providers; harmless if ignored,
        # but some reject it, so we only try it when explicitly asked and
        # silently fall back on error.
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if json_mode and "response_format" in str(exc).lower():
                kwargs.pop("response_format", None)
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise
        content = resp.choices[0].message.content or ""
        return content.strip()

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """Return parsed JSON from the model, tolerating common quirks."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        raw = self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        parsed = _extract_json(raw)
        if parsed is None:
            log.debug("LLM returned non-JSON output: %s", raw[:500])
            raise LLMError("LLM did not return valid JSON")
        return parsed


def _extract_json(text: str) -> Optional[Any]:
    """Best-effort JSON extraction from a model response."""
    if not text:
        return None

    # 1) Direct parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) Strip ```json ... ``` fences.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3) Grab the outermost {...} or [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                continue

    return None
