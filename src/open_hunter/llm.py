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
import os
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
                "LLM is not configured. Set the provider variables in your "
                ".env (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL, or for Bedrock: "
                "LLM_PROVIDER=bedrock + LLM_API_KEY + LLM_REGION + LLM_MODEL)."
            )
        self.config = config
        self.client = None
        self._bedrock = None

        if config.is_bedrock:
            self._init_bedrock()
        else:
            self.client = OpenAI(
                base_url=config.llm_base_url,
                api_key=config.llm_api_key,
            )

    def _init_bedrock(self) -> None:
        """Native Amazon Bedrock client via the Converse API (boto3).

        Converse exposes the full Bedrock catalog (Claude, Llama, Nova,
        Mistral, ...), unlike the OpenAI-compatible endpoint which only serves
        a curated subset. Authentication uses the Bedrock API key (bearer
        token) through the standard AWS_BEARER_TOKEN_BEDROCK env var.
        """
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "Bedrock provider needs boto3. Run: pip install boto3"
            ) from exc

        if self.config.llm_api_key and not os.environ.get(
            "AWS_BEARER_TOKEN_BEDROCK"
        ):
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self.config.llm_api_key

        self._bedrock = boto3.client(
            "bedrock-runtime", region_name=self.config.llm_region or "us-east-1"
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
        if self.config.is_bedrock:
            return self._chat_bedrock(messages, temperature, max_tokens)

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
        except Exception:  # noqa: BLE001
            # Some providers/models (varies across the Bedrock catalog) reject
            # response_format. If we asked for it, retry once without it — our
            # complete_json() still extracts JSON defensively from plain text.
            if "response_format" in kwargs:
                kwargs.pop("response_format", None)
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise
        content = resp.choices[0].message.content or ""
        return content.strip()

    def _chat_bedrock(
        self,
        messages: List[dict],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        """Call Bedrock's Converse API, translating OpenAI-style messages."""
        system_blocks = []
        conv = []
        for m in messages:
            role = m.get("role")
            text = m.get("content") or ""
            if not text:
                continue
            if role == "system":
                system_blocks.append({"text": text})
            else:
                conv.append(
                    {
                        "role": "assistant" if role == "assistant" else "user",
                        "content": [{"text": text}],
                    }
                )

        kwargs: dict = {
            "modelId": self.config.llm_model,
            "messages": conv,
            "inferenceConfig": {
                "maxTokens": max_tokens or self.config.llm_max_tokens,
                "temperature": (
                    self.config.llm_temperature
                    if temperature is None
                    else temperature
                ),
            },
        }
        if system_blocks:
            kwargs["system"] = system_blocks

        resp = self._bedrock.converse(**kwargs)
        parts = resp["output"]["message"]["content"]
        text = "".join(p.get("text", "") for p in parts)
        return text.strip()

    @property
    def vision_available(self) -> bool:
        # Vision uses the OpenAI-compatible image_url content format, available
        # on the standard client (e.g. OpenRouter), not the Bedrock path.
        return self.client is not None

    def vision_json(
        self, prompt: str, image_path: str, model: str,
        *, max_tokens: int = 900,
    ) -> Any:
        """Send an image + prompt to a vision model, parse JSON from the reply."""
        if self.client is None:
            raise LLMError("vision requires an OpenAI-compatible client")
        import base64

        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        resp = self.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            temperature=0,
            max_tokens=max_tokens,
        )
        return _extract_json(resp.choices[0].message.content or "")

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
