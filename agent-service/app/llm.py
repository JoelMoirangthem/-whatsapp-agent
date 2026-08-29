"""LLM access via AgentRouter (OpenAI-compatible) through LangChain.

Latency contract: no hidden retries, connect fails fast (~2s default), and
callers consume tokens via `astream` as they arrive instead of waiting for
the full completion.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from langchain_openai import ChatOpenAI


def build_llm(settings) -> ChatOpenAI:
    """ChatOpenAI pointed at the AgentRouter endpoint.

    - max_retries=0: a dead provider surfaces immediately instead of silently
      doubling request latency.
    - httpx.Timeout separates connect (fail fast) from read (let generation
      finish; streaming keeps time-to-first-token low regardless).

    The custom User-Agent is REQUIRED by agentrouter.org (403 otherwise).
    """
    return ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=0,
        max_retries=0,
        timeout=httpx.Timeout(
            settings.llm_timeout_seconds,
            connect=settings.llm_connect_timeout_seconds,
            pool=settings.llm_connect_timeout_seconds,
        ),
        default_headers={"User-Agent": settings.llm_user_agent},
    )


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON object extraction from a model response."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    text = text[start : i + 1]
                    break
        else:
            text = text[start:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
