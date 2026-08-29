"""ElevenLabs Voice synthesis client and text normalizer for WhatsApp Voice Agent.

Production-grade improvements:
- tts_normalize: Hindi-safe, SSML-aware, URL/code-fence handling, 4000-char cap
- chunk_text_for_streaming: sentence-boundary chunker for low-latency streaming
- ElevenLabsClient: pooled httpx client, retry, quota-aware errors, stream fallback
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator

import httpx

logger = logging.getLogger("whatsapp-agent.voice")

PRESET_VOICES = [
    {"name": "George (Warm & Storyteller)", "voice_id": "JBFqnCBsd6RMkjVDRZzb"},
    {"name": "Rachel (Calm & Conversational)", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    {"name": "Sarah (Reassuring & Confident)", "voice_id": "EXAVITQu4vr4xnSDxMaL"},
    {"name": "Charlie (Energetic & Dynamic)", "voice_id": "IKne3meq5aSn9XLyUdCD"},
    {"name": "River (Neutral & Informative)", "voice_id": "SAz9YHcvj6GT2YYXdXww"},
    {"name": "Laura (Upbeat & Quirky)", "voice_id": "FGY2WhTYpPnrIDTdsKH5"},
]

# ElevenLabs hard limits
_MAX_TTS_CHARS = 4000
_MAX_TTS_CHARS_PER_REQUEST = 1000  # streaming chunk target
_VOICE_CACHE_TTL = 600  # seconds

# Simple in-memory voice list cache
_voice_cache: dict[str, tuple[float, list[dict]]] = {}


def tts_normalize(text: str, max_chars: int = _MAX_TTS_CHARS) -> str:
    """Normalize text for natural TTS. Hindi-safe, preserves intent.

    - Code fences -> "[code snippet omitted]"
    - Inline code -> plain text (no backticks)
    - Markdown links [t](url) -> t
    - Headers/bullets stripped, bold/italic markers removed
    - URLs removed cleanly (no dangling punctuation)
    - Extra whitespace collapsed
    - Truncated to max_chars on word boundary with ellipsis
    """
    if not text:
        return ""
    # Code blocks: replace whole block with placeholder (preserve sentence flow)
    text = re.sub(r"```[\s\S]*?```", " [code snippet omitted] ", text)
    # Inline code `x` -> x
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Markdown links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Headers (even indented) and list markers
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[\*\-\+]\s+", "", text, flags=re.MULTILINE)
    # Lone # leftovers
    text = re.sub(r"#", "", text)
    # Bold/italic: keep inner text, strip markers (handles *text* **text** _text_ ***text***)
    text = re.sub(r"[\*_]{1,3}([^\*_]+?)[\*_]{1,3}", r"\1", text)
    # URLs: remove but ensure no double-space or dangling "on !" case
    text = re.sub(r"https?://\S+", "", text)
    # Email-like? keep as-is (TTS can handle) — don't strip
    # Collapse whitespace, preserve single sentence spacing
    text = re.sub(r"\s+", " ", text).strip()
    # Remove isolated punctuation artifacts from URL removal: "on !" -> "on"
    text = re.sub(r"\s+([.,!?:;])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if len(text) > max_chars:
        # Truncate on word boundary
        truncated = text[: max_chars - 3].rsplit(" ", 1)[0] if " " in text[:max_chars] else text[: max_chars - 3]
        text = truncated.strip() + "..."
    return text


def clean_text_for_speech(text: str) -> str:
    """Backward-compatible alias for tts_normalize."""
    return tts_normalize(text)


def chunk_text_for_streaming(text: str, max_chars: int = 280) -> list[str]:
    """Split normalized text into sentence-boundary chunks for streaming TTS.

    Why 280? ElevenLabs streaming works best with short sentences (100-300 chars)
    for low TTFB. We greedily pack complete sentences.
    """
    clean = tts_normalize(text, max_chars=_MAX_TTS_CHARS)
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    # Split on sentence terminators while keeping delimiter
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        if not sent.strip():
            continue
        # If single sentence exceeds max, hard-split on comma or word
        if len(sent) > max_chars:
            if cur:
                chunks.append(cur.strip())
                cur = ""
            # Split long sentence on commas then words
            parts = re.split(r",\s+", sent)
            sub_cur = ""
            for part in parts:
                if len(sub_cur) + len(part) + 2 <= max_chars:
                    sub_cur = f"{sub_cur}, {part}" if sub_cur else part
                else:
                    if sub_cur:
                        chunks.append(sub_cur.strip())
                    # Still too long: word-boundary split (not mid-word)
                    if len(part) > max_chars:
                        words = part.split()
                        wcur = ""
                        for w in words:
                            if len(w) > max_chars:
                                # ultra-long token (e.g., URL remnant): hard split but keep word fragments
                                if wcur:
                                    chunks.append(wcur)
                                    wcur = ""
                                for i in range(0, len(w), max_chars):
                                    chunks.append(w[i : i + max_chars])
                                continue
                            if not wcur:
                                wcur = w
                            elif len(wcur) + 1 + len(w) <= max_chars:
                                wcur += " " + w
                            else:
                                chunks.append(wcur)
                                wcur = w
                        if wcur:
                            chunks.append(wcur)
                        sub_cur = ""
                    else:
                        sub_cur = part
            if sub_cur:
                chunks.append(sub_cur.strip())
            continue
        if len(cur) + len(sent) + 1 <= max_chars:
            cur = f"{cur} {sent}" if cur else sent
        else:
            if cur:
                chunks.append(cur.strip())
            cur = sent
    if cur:
        chunks.append(cur.strip())
    return [c for c in chunks if c]


class ElevenLabsClient:
    """Async client for ElevenLabs TTS streaming and generation.

    Production features:
    - Pooled httpx client (no per-request creation overhead)
    - Automatic retry on 429/5xx with exponential backoff
    - Friendly errors for quota/rate-limit
    - Graceful degrade when api_key missing
    """

    def __init__(
        self,
        api_key: str,
        default_voice_id: str = "JBFqnCBsd6RMkjVDRZzb",
        model_id: str = "eleven_turbo_v2_5",
    ):
        self.api_key = (api_key or "").strip()
        self.default_voice_id = (default_voice_id or "JBFqnCBsd6RMkjVDRZzb").strip()
        self.model_id = (model_id or "eleven_turbo_v2_5").strip()
        self.base_url = "https://api.elevenlabs.io/v1"
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._client_lock:
                if self._client is None or self._client.is_closed:
                    self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # Alias for compat
    close = aclose

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        """Synthesize full text into MP3 bytes. Length-guarded, retry-aware."""
        if not self.api_key:
            raise RuntimeError("ElevenLabs not configured — set ELEVENLABS_API_KEY")
        clean = tts_normalize(text)
        if not clean:
            clean = "I have processed your request."
        # Guard: ElevenLabs rejects >5000, we cap at 4000 earlier
        if len(clean) > _MAX_TTS_CHARS:
            clean = tts_normalize(clean, max_chars=_MAX_TTS_CHARS)

        target_voice = (voice_id or self.default_voice_id).strip() or self.default_voice_id
        url = f"{self.base_url}/text-to-speech/{target_voice}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": clean,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "style": 0.0,
                "use_speaker_boost": True,
            },
        }

        client = await self._get_client()
        last_err: str = ""
        for attempt in range(3):
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
                last_err = str(exc)
                if attempt < 2:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                raise RuntimeError(f"ElevenLabs connect failed: {exc}") from exc
            if resp.status_code == 200:
                return resp.content
            # Parse error body
            body = resp.text[:500] if hasattr(resp, "text") else ""
            if resp.status_code == 401:
                raise RuntimeError("ElevenLabs authentication failed — check ELEVENLABS_API_KEY")
            if resp.status_code == 429:
                last_err = f"rate limited (429): {body[:120]}"
                # Respect Retry-After if present
                retry_after = resp.headers.get("retry-after")
                try:
                    wait = float(retry_after) if retry_after else 1.5 * (attempt + 1)
                except ValueError:
                    wait = 1.5 * (attempt + 1)
                if attempt < 2:
                    await asyncio.sleep(min(wait, 5))
                    continue
                raise RuntimeError(f"ElevenLabs rate limited: {body[:120]}")
            if resp.status_code in (500, 502, 503, 504):
                last_err = f"server {resp.status_code}: {body[:120]}"
                if attempt < 2:
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                raise RuntimeError(f"ElevenLabs server error ({resp.status_code}): {body[:120]}")
            logger.error("ElevenLabs TTS error %d: %s", resp.status_code, body[:200])
            raise RuntimeError(f"ElevenLabs TTS failed ({resp.status_code}): {body[:120]}")
        raise RuntimeError(f"ElevenLabs TTS failed after retries: {last_err}")

    async def synthesize_stream(
        self, text: str, voice_id: str | None = None
    ) -> AsyncGenerator[tuple[int, bytes], None]:
        """Yield (index, audio_bytes) per sentence chunk. Fallback-sequential.

        For true low-latency, caller should stream LLM sentences and call this per chunk.
        This helper just chunks locally and synthesizes sequentially — can be upgraded
        to websocket streaming later without changing caller.
        """
        chunks = chunk_text_for_streaming(text, max_chars=_MAX_TTS_CHARS_PER_REQUEST)
        for idx, chunk in enumerate(chunks):
            try:
                audio = await self.synthesize(chunk, voice_id=voice_id)
                yield idx, audio
            except Exception as exc:
                logger.warning("TTS chunk %d failed: %s", idx, exc)
                # Yield nothing for failed chunk — caller can fallback to browser TTS for remaining
                continue

    async def list_voices(self) -> list[dict]:
        """Fetch available voices from ElevenLabs account, with preset fallback and cache."""
        if not self.api_key:
            return PRESET_VOICES
        # Check cache
        cached = _voice_cache.get(self.api_key)
        if cached and (time.time() - cached[0] < _VOICE_CACHE_TTL):
            return cached[1]
        url = f"{self.base_url}/voices"
        headers = {"xi-api-key": self.api_key}
        try:
            client = await self._get_client()
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                voices = data.get("voices", [])
                parsed = [
                    {"name": v.get("name", "Voice"), "voice_id": v.get("voice_id", "")}
                    for v in voices
                    if v.get("voice_id")
                ]
                if parsed:
                    _voice_cache[self.api_key] = (time.time(), parsed)
                    return parsed
            elif resp.status_code == 401:
                logger.warning("ElevenLabs list_voices auth failed — using presets")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch remote ElevenLabs voices: %s", exc)
        return PRESET_VOICES
