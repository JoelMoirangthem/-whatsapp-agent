"""Tests for Voice Agent (ElevenLabs TTS, text normalization, and endpoints)."""

from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

import app.main as main_mod
from app.voice import ElevenLabsClient, clean_text_for_speech

from .conftest import auth_headers, build_container


def test_clean_text_for_speech():
    """Text cleaner strips markdown symbols, code fences, URLs, and bolding."""
    raw = """
    # Meeting Summary for WhatsApp
    Here is the update:
    * Action item 1: Check `config.py`
    * Action item 2: Review [Documentation](https://example.com/docs)
    ```python
    print("code snippet")
    ```
    **Important:** Please approve the message on https://whatsapp.com!
    """
    clean = clean_text_for_speech(raw)
    assert "#" not in clean
    assert "https://" not in clean
    assert "**" not in clean
    assert "[code snippet omitted]" in clean
    assert "Check config.py" in clean
    assert "Review Documentation" in clean
    assert "Important: Please approve the message on !" in clean or "Important: Please approve" in clean


@pytest.mark.asyncio
async def test_elevenlabs_client_synthesis(monkeypatch):
    """ElevenLabsClient invokes API and returns audio bytes."""
    client = ElevenLabsClient(api_key="test-key", default_voice_id="JBFqnCBsd6RMkjVDRZzb")

    class MockResponse:
        status_code = 200
        content = b"ID3\x03\x00\x00\x00fake-mp3-audio-bytes"

    async def mock_post(self, url, json=None, headers=None):
        assert headers.get("xi-api-key") == "test-key"
        assert json["model_id"] == "eleven_turbo_v2_5"
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)
    audio = await client.synthesize("Hello world!")
    assert audio.startswith(b"ID3")


@pytest.mark.asyncio
async def test_elevenlabs_client_list_voices(monkeypatch):
    """ElevenLabsClient list_voices fetches voices or falls back to presets."""
    client = ElevenLabsClient(api_key="test-key")

    class MockResponse:
        status_code = 200
        def json(self):
            return {"voices": [{"name": "Custom Voice", "voice_id": "cust-1"}]}

    async def mock_get(self, url, headers=None):
        return MockResponse()

    monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
    voices = await client.list_voices()
    assert len(voices) == 1
    assert voices[0]["voice_id"] == "cust-1"


@pytest.mark.asyncio
async def test_voice_endpoints(tmp_path, monkeypatch):
    """Test /agents/voice/synthesize, /agents/voice/chat, and /agents/voice/voices."""
    container = build_container(tmp_path, [])
    main_mod.set_container(container)

    # Mock voice synthesis on container
    async def mock_synthesize(text, voice_id=None):
        return b"mock-mp3-bytes"

    async def mock_list_voices():
        return [{"name": "George", "voice_id": "JBFqnCBsd6RMkjVDRZzb"}]

    monkeypatch.setattr(container.voice, "synthesize", mock_synthesize)
    monkeypatch.setattr(container.voice, "list_voices", mock_list_voices)

    async with AsyncClient(
        transport=ASGITransport(app=main_mod.app), base_url="http://t"
    ) as ac:
        # 1. Unauthenticated requests must return 401
        r_unauth = await ac.post("/agents/voice/synthesize", json={"text": "hello"})
        assert r_unauth.status_code == 401

        r_unauth_chat = await ac.post("/agents/voice/chat", json={"message": "hello"})
        assert r_unauth_chat.status_code == 401

        # 2. Voice synthesize
        r_synth = await ac.post(
            "/agents/voice/synthesize",
            headers=auth_headers(),
            json={"text": "Hello, this is a voice test.", "voice_id": "JBFqnCBsd6RMkjVDRZzb"},
        )
        assert r_synth.status_code == 200
        assert r_synth.headers.get("content-type") == "audio/mpeg"
        assert r_synth.content == b"mock-mp3-bytes"

        # 3. Voice chat pipeline
        r_chat = await ac.post(
            "/agents/voice/chat",
            headers=auth_headers(),
            json={"message": "What is the status?", "chat_jid": "917409193202@s.whatsapp.net"},
        )
        assert r_chat.status_code == 200
        data = r_chat.json()
        assert "text" in data
        assert "audio_base64" in data
        assert base64.b64decode(data["audio_base64"]) == b"mock-mp3-bytes"

        # 4. Voices list
        r_voices = await ac.get("/agents/voice/voices", headers=auth_headers())
        assert r_voices.status_code == 200
        v_data = r_voices.json()
        assert len(v_data["voices"]) == 1
        assert v_data["voices"][0]["voice_id"] == "JBFqnCBsd6RMkjVDRZzb"
