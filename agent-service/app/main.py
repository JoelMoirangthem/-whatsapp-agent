"""WhatsApp Agent Service — FastAPI application.

Endpoints:
   POST /agents/whatsapp                      → answer | approval_required | blocked
   POST /agents/whatsapp/stream               → same pipeline over SSE
                                                (meta/tool/delta + terminal event)
   POST /agents/whatsapp/approve/{action_id}  → {approved: true|false}
   GET  /agents/whatsapp/actions              → pending actions (for the UI)
   GET  /agents/whatsapp/actions/{action_id}  → action status
   GET  /audit                                → recent audit trail
   GET  /health                               → liveness/config/latency probe
   GET  /approvals                            → minimal approval UI
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import socketio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders

from app.agent import AgentResult, WhatsAppAgent
from app.bridge import BridgeClient, BridgeError, MessageArchive
from app.config import Settings, load_settings
from app.db import AgentStore
from app.llm import build_llm
from app.voice import ElevenLabsClient

logger = logging.getLogger("whatsapp-agent.latency")

# Rolling window of request latencies (ms) for /health percentiles.
_LATENCIES: deque[float] = deque(maxlen=256)

# Avatar miss-cache: JIDs known to have no photo. Stops every chat-list
# render from re-querying WhatsApp for contacts that have none.
_AVATAR_MISS_TTL = 900  # seconds
_avatar_miss_cache: dict[str, float] = {}
_avatar_fetch_lock: asyncio.Lock | None = None

# Voice TTS rate limiter: per-user token bucket (in-memory)
_VOICE_RATE_LIMIT = 8  # requests per window
_VOICE_RATE_WINDOW = 60.0  # seconds
_VOICE_MAX_CHARS = 2000  # max synthesis input
_voice_rate_store: dict[str, deque[float]] = {}


def _check_voice_rate_limit(user_id: str) -> None:
    now = time.time()
    dq = _voice_rate_store.get(user_id)
    if dq is None:
        dq = deque()
        _voice_rate_store[user_id] = dq
    # expire old entries
    while dq and now - dq[0] > _VOICE_RATE_WINDOW:
        dq.popleft()
    if len(dq) >= _VOICE_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="voice rate limit exceeded — try again in a moment")
    dq.append(now)


def _build_voice_answer_text(result_type: str, payload: dict, container) -> str:
    """Server-authoritative spoken prompt for voice flow."""
    if result_type == "approval_required":
        tool = payload.get("tool", "")
        args = payload.get("args", {}) or {}
        reason = payload.get("reason", "") or ""
        if tool == "send_message":
            recipient = str(args.get("recipient") or "")
            msg = str(args.get("message") or "")
            friendly = recipient
            try:
                for c in container.archive.list_chats(limit=200):
                    if c.get("jid") == recipient:
                        friendly = c.get("name") or recipient
                        break
            except Exception:
                pass
            preview = msg.strip().replace("\n", " ")[:90]
            if len(msg.strip()) > 90:
                preview += "..."
            return f'You asked me to send "{preview}" to {friendly}. Should I execute? Say yes to approve, or no to cancel.'
        if tool == "delete_message":
            jid = str(args.get("chat_jid") or "this chat")
            friendly = jid
            try:
                for c in container.archive.list_chats(limit=200):
                    if c.get("jid") == jid:
                        friendly = c.get("name") or jid
                        break
            except Exception:
                pass
            return f'You asked to delete a message in {friendly}. Should I execute? Say yes or no.'
        if tool == "initiate_audio_call":
            recipient = str(args.get("recipient") or "")
            friendly = recipient
            try:
                for c in container.archive.list_chats(limit=200):
                    if c.get("jid") == recipient:
                        friendly = c.get("name") or recipient
                        break
            except Exception:
                pass
            return f'You asked to start an audio call to {friendly}. Should I dial? Say yes to approve, or no to cancel.'
        if tool == "initiate_video_call":
            recipient = str(args.get("recipient") or "")
            friendly = recipient
            try:
                for c in container.archive.list_chats(limit=200):
                    if c.get("jid") == recipient:
                        friendly = c.get("name") or recipient
                        break
            except Exception:
                pass
            return f'You asked to start a video call to {friendly}. Should I dial? Say yes to approve, or no to cancel.'
        detail = reason[:100] if reason else tool
        return f'Request to execute {tool}: {detail}. Should I proceed? Say yes or no.'
    return payload.get("text") or payload.get("message") or payload.get("reason") or "Done."


sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[])


def emit_live(event: str, data: dict, room: str = "ui") -> None:
    """Fire-and-forget push to all connected UI clients (or one chat room)."""

    async def _send():
        try:
            await sio.emit(event, data, room=room)
        except Exception:  # noqa: BLE001 - telemetry never breaks the agent
            pass

    try:
        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        pass  # no running loop (e.g. pure-sync context): drop


def wire_event_bridge(container: Container) -> None:
    """Publish every audit write onto the live event bus."""

    def hook(event: dict) -> None:
        emit_live("audit", event)

    container.store.audit_hook = hook


@sio.event
async def connect(sid, environ, auth):
    """Handshake auth: require the service token."""
    supplied = str((auth or {}).get("token", ""))
    container = get_container()
    if not container.settings.service_token or supplied != container.settings.service_token:
        raise socketio.exceptions.ConnectionRefusedError("invalid token")
    await sio.enter_room(sid, "ui")


@sio.on("subscribe_chat")
async def subscribe_chat(sid, data):
    jid = str((data or {}).get("jid", ""))
    if jid:
        await sio.enter_room(sid, f"chat:{jid}")


@sio.on("unsubscribe_chat")
async def unsubscribe_chat(sid, data):
    jid = str((data or {}).get("jid", ""))
    if jid:
        await sio.leave_room(sid, f"chat:{jid}")


class Container:
    """Dependency container; rebuilt via set_container() in tests."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = AgentStore(settings.agent_db_path)
        self.archive = MessageArchive(settings.messages_db_path)
        self.bridge = BridgeClient(settings.bridge_api_url, settings.bridge_token)
        self.llm = build_llm(settings) if settings.llm_api_key else None
        self.agent = WhatsAppAgent(settings, self.store, self.archive, self.bridge, self.llm)
        self.voice = ElevenLabsClient(
            settings.elevenlabs_api_key,
            settings.elevenlabs_voice_id,
            settings.elevenlabs_model,
        )
        wire_event_bridge(self)


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = Container(load_settings())
    return _container


def set_container(container: Container | None) -> None:
    global _container
    _container = container


async def require_user(
    authorization: str = Header(default=""),
    x_user_id: str = Header(default="local"),
) -> str:
    expected = get_container().settings.service_token
    supplied = authorization.removeprefix("Bearer ").strip()
    if not expected or not supplied or supplied != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    if not x_user_id.strip():
        raise HTTPException(status_code=400, detail="empty user id")
    return x_user_id.strip()


async def health_monitor() -> None:
    """Server-side watcher: pushes a `health` SSE event only on state change.

    Replaces per-browser polling — one shared 15s check for all clients.
    """
    last: dict | None = None
    while True:
        try:
            container = get_container()
            try:
                resp = await container.bridge.health()
                snap = {
                    "bridge_up": True,
                    "whatsapp_connected": bool(resp.data.get("connected")),
                    "logged_in": bool(resp.data.get("logged_in")),
                    "llm_configured": bool(container.settings.llm_api_key),
                    "model": container.settings.llm_model,
                }
            except Exception:  # noqa: BLE001
                snap = {
                    "bridge_up": False,
                    "whatsapp_connected": False,
                    "logged_in": False,
                    "llm_configured": bool(container.settings.llm_api_key),
                    "model": container.settings.llm_model,
                }
            if snap != last:
                emit_live("health", snap)
                last = snap
        except Exception:  # noqa: BLE001 - monitor survives anything
            pass
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(_: FastAPI):
    container = get_container()  # eager init on startup
    wire_event_bridge(container)
    monitor = asyncio.create_task(health_monitor())
    yield
    monitor.cancel()
    try:
        await container.voice.aclose()
    except Exception:
        pass
    await container.bridge.aclose()
    container.archive.close()
    container.store.close()


app = FastAPI(title="whatsapp-agent-service", version="0.2.0", lifespan=lifespan)
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _latency_warn_ms() -> float:
    # Read straight from env so the middleware never forces container init.
    try:
        return float(os.environ.get("LATENCY_WARN_MS", "2000"))
    except ValueError:
        return 2000.0


class NoCacheHTMLMiddleware:
    """Fresh HTML/JS on every load so UI updates are never shadowed by cache."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]

        async def send_no_cache(message):
            if message["type"] == "http.response.start":
                if path == "/" or not path.startswith(("/static/", "/agents", "/audit", "/health")):
                    headers = MutableHeaders(scope=message)
                    headers["Cache-Control"] = "no-cache, must-revalidate"
            await send(message)

        await self.app(scope, receive, send_no_cache)


class LatencyMiddleware:
    """Every response carries X-Process-Time-Ms; slow ones are logged so
    regressions against the 2s budget are visible immediately."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()

        async def send_timed(message):
            if message["type"] == "http.response.start":
                ms = (time.perf_counter() - start) * 1000
                headers = MutableHeaders(scope=message)
                headers["X-Process-Time-Ms"] = f"{ms:.1f}"
                _LATENCIES.append(ms)
                if ms >= _latency_warn_ms():
                    logger.warning(
                        "slow request path=%s method=%s status=%s latency_ms=%.1f",
                        scope["path"],
                        scope["method"],
                        message["status"],
                        ms,
                    )
            await send(message)

        await self.app(scope, receive, send_timed)


app.add_middleware(LatencyMiddleware)
app.add_middleware(NoCacheHTMLMiddleware)


@app.get("/pair-token", include_in_schema=False)
async def pair_token(request: Request):
    """Hand the service token to LOOPBACK callers only.

    The whole service binds to 127.0.0.1; anything that can reach this
    endpoint could equally read data/.agent-token from disk. This exists to
    make local browser pairing zero-friction — never expose the service
    beyond loopback without deleting this route first.
    """
    client_host = request.client.host if request.client else ""
    if client_host not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="loopback only")
    return {"token": get_container().settings.service_token}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        "<stop offset='0' stop-color='#6366f1'/><stop offset='1' stop-color='#ec4899'/>"
        "</linearGradient></defs><rect width='24' height='24' rx='6' fill='url(#g)'/>"
        "<path d='M12 5l7 4v6l-7 4-7-4V9z' fill='#fff' opacity='.95'/></svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


class AgentRequest(BaseModel):
    message: str


class ApprovalDecision(BaseModel):
    approved: bool


class DirectSend(BaseModel):
    recipient: str
    message: str
    quoted_message_id: str | None = None


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round(pct / 100 * (len(sorted_vals) - 1))))
    return round(sorted_vals[idx], 1)


@app.get("/health")
async def health() -> dict:
    container = get_container()
    s = container.settings
    # Any HTTP response means the bridge process is up; its own body tells us
    # whether a WhatsApp session is actually connected (503 while unpaired).
    bridge_status, bridge_connected, bridge_logged_in = 0, None, None
    try:
        resp = await container.bridge.health()
        bridge_status = resp.status
        bridge_connected = bool(resp.data.get("connected"))
        bridge_logged_in = resp.data.get("logged_in")
    except Exception:  # noqa: BLE001 - health must never raise
        pass
    latencies = sorted(_LATENCIES)
    return {
        "status": "ok",
        "provider": "agentrouter" if s.llm_api_key else "unconfigured",
        "llm": {"configured": bool(s.llm_api_key), "model": s.llm_model},
        "bridge": {
            "up": bridge_status != 0,
            "http_status": bridge_status,
            "whatsapp_connected": bridge_connected,
            "logged_in": bridge_logged_in,
            "url": s.bridge_api_url,
        },
        "archive": {"available": container.archive.available()},
        "voice": {
            "name": "Uhu",
            "wake_word": s.voice_wake_word,
            "elevenlabs_configured": bool(s.elevenlabs_api_key),
            "model": s.elevenlabs_model,
            "voice_id": s.elevenlabs_voice_id,
            "wake_greeting": s.voice_wake_greeting,
        },
        "latency": {
            "budget_warn_ms": _latency_warn_ms(),
            "samples": len(latencies),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
        },
    }


@app.post("/agents/whatsapp")
async def whatsapp_agent(request: AgentRequest, user_id: str = Depends(require_user)):
    result = await get_container().agent.handle_request(request.message, user_id)
    status_code = {"answer": 200, "approval_required": 202, "blocked": 422, "error": 503}.get(
        result.type, 500
    )
    body = {"type": result.type, **result.payload}
    if result.type == "approval_required":
        body["status"] = "pending"
    return JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------- #
# Streaming (SSE over POST; consumed via fetch ReadableStream)
# --------------------------------------------------------------------- #


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/agents/whatsapp/stream")
async def whatsapp_agent_stream(request: AgentRequest, user_id: str = Depends(require_user)):
    container = get_container()
    started = time.perf_counter()

    async def event_gen():
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def on_event(event: str, data: dict) -> None:
            await queue.put((event, data))

        async def run():
            try:
                result = await container.agent.handle_request(request.message, user_id, on_event)
            except Exception as exc:  # noqa: BLE001 - terminal error event
                result = AgentResult("error", {"message": str(exc)[:300]})
            await queue.put(("__done__", result))

        yield _sse(
            "meta", {"requestId": uuid.uuid4().hex[:12], "model": container.settings.llm_model}
        )
        task = asyncio.create_task(run())
        try:
            while True:
                name, data = await queue.get()
                if name == "__done__":
                    result: AgentResult = data
                    break
                yield _sse(name, data)
            payload = {"type": result.type, **result.payload}
            payload.setdefault("processTimeMs", round((time.perf_counter() - started) * 1000, 1))
            yield _sse(result.type, payload)
        finally:
            task.cancel()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/agents/whatsapp/approve/{action_id}")
async def approve_action(
    action_id: str, decision: ApprovalDecision, user_id: str = Depends(require_user)
):
    code, body = await get_container().agent.decide(action_id, decision.approved, user_id)
    return JSONResponse(status_code=code, content=body)


# --------------------------------------------------------------------- #
# Voice Agent (ElevenLabs TTS & Real-Time Conversational AI)
# --------------------------------------------------------------------- #


class VoiceSynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice_id: str | None = Field(default=None, max_length=64)


class VoiceChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    chat_jid: str | None = Field(default=None, max_length=128)
    voice_id: str | None = Field(default=None, max_length=64)


@app.post("/agents/voice/synthesize")
async def voice_synthesize(req: VoiceSynthesizeRequest, user_id: str = Depends(require_user)):
    """Synthesize text into natural speech audio stream via ElevenLabs."""
    _check_voice_rate_limit(user_id)
    container = get_container()
    if not container.settings.elevenlabs_api_key:
        raise HTTPException(
            status_code=503,
            detail="ElevenLabs not configured — set ELEVENLABS_API_KEY",
        )
    if len(req.text.strip()) > _VOICE_MAX_CHARS:
        raise HTTPException(status_code=422, detail=f"text too long (max {_VOICE_MAX_CHARS} chars)")
    try:
        audio_bytes = await container.voice.synthesize(req.text, voice_id=req.voice_id)
        container.store.audit("voice_synthesize", user_id, chars=len(req.text), voice_id=req.voice_id or "")
        return Response(content=audio_bytes, media_type="audio/mpeg")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voice synthesize failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Voice synthesis error: {exc}") from exc


@app.post("/agents/voice/chat")
async def voice_chat(req: VoiceChatRequest, user_id: str = Depends(require_user)):
    """Interactive voice pipeline: Agent reasoning -> safe spoken text -> ElevenLabs TTS audio.

    Production-grade: rate-limited, length-guarded, server-authoritative approval prompt,
    audited, with graceful browser-TTS fallback.
    """
    _check_voice_rate_limit(user_id)
    container = get_container()
    user_prompt = req.message
    if req.chat_jid:
        user_prompt = f"[Active Chat context: {req.chat_jid}]\n{user_prompt}"

    steps: list[dict] = []

    async def on_event(event: str, data: dict) -> None:
        if event == "delta":
            return
        step_item = {"event": event, "data": data, "ts": time.time()}
        steps.append(step_item)
        emit_live("voice_activity", step_item)

    try:
        emit_live("voice_activity", {"event": "stage", "data": {"phase": "analyzing", "text": "Analyzing voice instruction..."}})
        result = await container.agent.handle_request(
            user_prompt, user_id, on_event=on_event, voice_mode=True
        )
        payload = {"type": result.type, **result.payload}
        answer_text = _build_voice_answer_text(result.type, payload, container)
        if result.type == "approval_required":
            emit_live("voice_activity", {"event": "approval_required", "data": {"tool": payload.get("tool",""), "args": payload.get("args",{})}})

        emit_live("voice_activity", {"event": "stage", "data": {"phase": "synthesizing", "text": "Generating ElevenLabs voice..."}})

        audio_b64 = None
        if container.settings.elevenlabs_api_key and answer_text.strip():
            try:
                # Guard length: tts_normalize already caps at 4000, but we also cap spoken prompt at 800
                from app.voice import tts_normalize

                tts_text = tts_normalize(answer_text, max_chars=800)
                audio_bytes = await container.voice.synthesize(tts_text, voice_id=req.voice_id)
                audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            except Exception as exc:  # noqa: BLE001 - degrade to browser TTS
                logger.warning("ElevenLabs synthesis failed, using browser TTS: %s", exc)

        emit_live("voice_activity", {"event": "stage", "data": {"phase": "ready", "text": "Speaking response..."}})
        container.store.audit(
            "voice_chat", user_id,
            type=result.type,
            text_len=len(answer_text),
            has_audio=bool(audio_b64),
            voice_id=req.voice_id or "",
        )
        # Record transcript for audit trail (privacy: store truncated)
        container.store.add_memory(user_id, "user", f"[voice] {req.message[:300]}")
        container.store.add_memory(user_id, "assistant", answer_text[:500])

        return {
            "type": result.type,
            "text": answer_text,
            "audio_base64": audio_b64,
            "steps": steps,
            "payload": payload,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voice chat error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Voice agent error: {exc}") from exc


@app.post("/agents/voice/stream")
async def voice_chat_stream(req: VoiceChatRequest, user_id: str = Depends(require_user)):
    """Streaming voice pipeline — agent reasoning + chunked TTS audio via SSE.

    Events:
      meta {requestId, model}
      stage/tool {phase, tool, args}
      delta {text}  (answer tokens, if any)
      voice_result {type, text, payload}  (terminal agent result)
      audio_chunk {index, b64, isLast}  (chunked MP3, streamed as synthesized)
      error {message}

    This achieves <1s first-audio for short answers vs 4-8s batch.
    """
    _check_voice_rate_limit(user_id)
    container = get_container()
    started = time.perf_counter()
    user_prompt = req.message
    if req.chat_jid:
        user_prompt = f"[Active Chat context: {req.chat_jid}]\n{user_prompt}"

    async def event_gen():
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def on_event(event: str, data: dict) -> None:
            await queue.put((event, data))

        async def run_agent():
            try:
                result = await container.agent.handle_request(user_prompt, user_id, on_event=on_event, voice_mode=True)
                await queue.put(("__done__", result))
            except Exception as exc:  # noqa: BLE001
                await queue.put(("__error__", str(exc)[:300]))

        # Initial meta
        yield _sse("meta", {"requestId": uuid.uuid4().hex[:12], "model": container.settings.llm_model, "voice_id": req.voice_id or container.settings.elevenlabs_voice_id})

        task = asyncio.create_task(run_agent())
        result: AgentResult | None = None
        # Forward stage/tool/delta until agent completes
        try:
            while True:
                name, data = await queue.get()
                if name == "__done__":
                    result = data
                    break
                if name == "__error__":
                    yield _sse("error", {"message": data})
                    return
                # Forward with live bus as well
                if name != "delta":
                    emit_live("voice_activity", {"event": name, "data": data})
                yield _sse(name, data)
            assert result is not None
            payload = {"type": result.type, **result.payload}
            answer_text = _build_voice_answer_text(result.type, payload, container)
            # Include expiresAt for approval so client can show countdown (server-authoritative)
            voice_result = {
                "type": result.type,
                "text": answer_text,
                "payload": payload,
                "expiresAt": payload.get("expiresAt"),
                "processTimeMs": round((time.perf_counter() - started) * 1000, 1),
            }
            # Emit approval stage
            if result.type == "approval_required":
                emit_live("voice_activity", {"event": "approval_required", "data": {"tool": payload.get("tool",""), "args": payload.get("args",{})}})
            # Terminal agent result
            yield _sse("voice_result", voice_result)
            emit_live("voice_activity", {"event": "stage", "data": {"phase": "synthesizing", "text": "Generating voice..."}})

            # Streamed TTS: chunked, sequential, each emitted as audio_chunk
            from app.voice import chunk_text_for_streaming

            chunks = chunk_text_for_streaming(answer_text, max_chars=280) if answer_text.strip() else []
            if container.settings.elevenlabs_api_key and chunks:
                for idx, chunk in enumerate(chunks):
                    try:
                        audio_bytes = await container.voice.synthesize(chunk, voice_id=req.voice_id)
                        b64 = base64.b64encode(audio_bytes).decode("ascii")
                        is_last = idx == len(chunks) - 1
                        yield _sse("audio_chunk", {"index": idx, "b64": b64, "isLast": is_last, "text": chunk[:80]})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("TTS chunk %d failed: %s", idx, exc)
                        yield _sse("audio_chunk", {"index": idx, "error": str(exc)[:120], "isLast": idx == len(chunks) - 1})
                    await asyncio.sleep(0)
            else:
                yield _sse("audio_chunk", {"index": 0, "b64": None, "isLast": True, "fallback": "browser"})

            emit_live("voice_activity", {"event": "stage", "data": {"phase": "ready", "text": "Speaking response..."}})
            container.store.audit("voice_stream", user_id, type=result.type, text_len=len(answer_text), chunks=len(chunks) if container.settings.elevenlabs_api_key else 0)
            # Final done marker
            yield _sse("done", {"processTimeMs": round((time.perf_counter() - started) * 1000, 1)})
        finally:
            task.cancel()

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/agents/voice/voices")
async def voice_list(_: str = Depends(require_user)):
    """List available ElevenLabs voices (cached)."""
    container = get_container()
    voices = await container.voice.list_voices()
    return {"voices": voices}


# --------------------------------------------------------------------- #
# Chats UI (WhatsApp-web-style browsing of the local archive)
# --------------------------------------------------------------------- #


@app.get("/agents/whatsapp/chats")
async def list_chats(query: str = "", limit: int = 60, _: str = Depends(require_user)):
    """Chat list from the local archive, newest first, optional name/JID filter."""
    archive = get_container().archive
    chats = archive.list_chats(limit=200)
    q = query.strip().lower()
    if q:
        chats = [c for c in chats if q in (c["name"] or "").lower() or q in c["jid"].lower()]
    return {"chats": chats[: min(limit, 100)]}


@app.get("/agents/whatsapp/avatar")
async def contact_avatar(
    jid: str,
    refresh: str = "",
    token: str = "",
    authorization: str = Header(default=""),
):
    """Profile photo for a contact/group; 404 when none exists (initials fallback)."""
    container = get_container()
    supplied_token = token or authorization.removeprefix("Bearer ").strip()
    if not container.settings.service_token or supplied_token != container.settings.service_token:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    if jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        raise HTTPException(status_code=404, detail="no profile picture")

    global _avatar_fetch_lock
    now = time.time()
    if not refresh and _avatar_miss_cache.get(jid, 0) > now:
        raise HTTPException(status_code=404, detail="no profile picture")

    if _avatar_fetch_lock is None:
        _avatar_fetch_lock = asyncio.Lock()
    async with _avatar_fetch_lock:  # serialize avatar fetches (rate-limit safe)
        if not refresh and _avatar_miss_cache.get(jid, 0) > time.time():
            raise HTTPException(status_code=404, detail="no profile picture")
        ok, data, ctype, err = await container.bridge.get_avatar(jid)
        if not ok or not data:
            _avatar_miss_cache[jid] = time.time() + _AVATAR_MISS_TTL
            raise HTTPException(status_code=404, detail=err or "no profile picture")

    return Response(
        content=data,
        media_type=ctype or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/internal/webhook")
async def bridge_webhook(request: Request):
    """Receiver for the Go bridge's outbound message webhooks.

    Authenticated by the shared bridge token (X-Bridge-Token header); turns
    inbound WhatsApp traffic into live SSE pings so open conversations
    refresh without polling.
    """
    container = get_container()
    supplied = (
        request.headers.get("X-Bridge-Token")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    if not container.settings.bridge_token or supplied != container.settings.bridge_token:
        raise HTTPException(status_code=401, detail="invalid bridge token")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    chat_jid = str(payload.get("chatJID") or payload.get("chat_jid") or "")
    event_type = str(payload.get("eventType") or payload.get("type") or "message")
    sender = str(payload.get("sender") or "")
    if chat_jid:
        incoming = {
            "chat_jid": chat_jid,
            "event_type": event_type,
            "sender": sender,
            "message_id": str(payload.get("messageId") or payload.get("id") or ""),
        }
        emit_live("incoming", incoming)
        emit_live("incoming", incoming, room=f"chat:{chat_jid}")
    return {"received": True}


@app.get("/agents/whatsapp/media")
async def media_file(
    chat_jid: str,
    message_id: str,
    token: str = "",
    authorization: str = Header(default=""),
):
    """Serve a stored media blob; fetches it from WhatsApp on first request.

    Accepts the service token via Authorization header OR ?token= query —
    browser <img>/<video>/<audio> tags cannot set headers.
    """
    container = get_container()
    supplied_token = token or authorization.removeprefix("Bearer ").strip()
    if not container.settings.service_token or supplied_token != container.settings.service_token:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    path = container.archive.media_path(chat_jid, message_id, container.settings.whatsapp_store_dir)
    if path is None:
        # Not on disk yet: ask the bridge to pull it from WhatsApp.
        try:
            await container.bridge.download_media(chat_jid, message_id)
        except Exception:  # noqa: BLE001 - fall through to 404 below
            pass
        path = container.archive.media_path(
            chat_jid, message_id, container.settings.whatsapp_store_dir
        )
        if path is None:
            # The bridge saves under ITS OWN generated timestamp name, not the
            # row's original filename — honor the returned Filename/Path.
            import os

            try:
                probe = container.bridge.last_download_info
                fname = (probe or {}).get("filename")
                fpath = (probe or {}).get("path")
                base = container.settings.whatsapp_store_dir
                for cand in (
                    fpath,
                    os.path.join(base, str(chat_jid), str(fname)) if fname else None,
                ):
                    if cand and os.path.isfile(cand):
                        path = cand
                        break
            except Exception:  # noqa: BLE001
                pass
    if path is None:
        raise HTTPException(status_code=404, detail="media not synced yet")
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime)


class ReactRequest(BaseModel):
    chat_jid: str
    message_id: str
    emoji: str = ""
    sender_jid: str | None = None


@app.post("/agents/whatsapp/react")
async def react_to_message(req: ReactRequest, user_id: str = Depends(require_user)):
    """Send/clear this account's reaction on a real WhatsApp message.

    Executes immediately (no approval queue): reactions are lightweight,
    reversible social signals — tapping again clears.
    """
    container = get_container()
    msg = container.archive.get_message(req.message_id, req.chat_jid)
    from_me = bool(msg.is_from_me) if msg else False
    if msg is None:
        if not req.sender_jid and req.chat_jid.endswith("@g.us"):
            raise HTTPException(
                status_code=422,
                detail="cannot determine message author; pass sender_jid",
            )

    try:
        # WhatsApp requires a FULL JID here; archive rows store bare numbers
        # ("917409193202") which the bridge rejects as Invalid sender_jid.
        sender = req.sender_jid or (msg.sender if msg and not from_me else None)
        if sender and "@" not in sender:
            sender = f"{sender}@s.whatsapp.net"
        await container.bridge.react(
            req.chat_jid,
            req.message_id,
            req.emoji,
            from_me=from_me,
            sender_jid=sender,
        )
    except BridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc

    container.store.set_my_reaction(req.chat_jid, req.message_id, req.emoji)
    emit_live(
        "reaction",
        {
            "chat_jid": req.chat_jid,
            "message_id": req.message_id,
            "emoji": req.emoji,
            "mine": True,
        },
        room=f"chat:{req.chat_jid}",
    )
    return {"ok": True, "emoji": req.emoji}


@app.get("/agents/whatsapp/chats/messages")
async def chat_messages(chat_jid: str, limit: int = 80, _: str = Depends(require_user)):
    """Chronological messages for one chat (rendered as bubbles by the UI).

    Reaction rows are split into a separate `reactions[]` array (they have no
    parent-link column) and media rows carry `resolvable` so the UI can show
    a fetch-chip before attempting a load.
    """
    container = get_container()
    msgs = container.archive.get_messages(chat_jid, limit=min(limit, 200))
    store_dir = container.settings.whatsapp_store_dir
    sender_names: dict[str, str] = {}

    def name_for(sender: str) -> str | None:
        if not sender:
            return None
        if sender not in sender_names:
            sender_names[sender] = container.archive.resolve_contact_name(sender) or ""
        return sender_names[sender] or None

    messages_out: list[dict] = []
    reactions: list[dict] = []
    my_reactions = container.store.get_my_reactions(chat_jid)
    for m in reversed(msgs):
        if (m.media_type or "").lower() == "reaction":
            reactions.append(
                {
                    "id": m.id,
                    "sender": m.sender,
                    "sender_name": name_for(m.sender),
                    "content": m.content or "👍",
                    "time": m.timestamp,
                }
            )
            continue
        entry = {
            "id": m.id,
            "sender": m.sender,
            "sender_name": name_for(m.sender) if not m.is_from_me else None,
            "from_me": m.is_from_me,
            "content": m.content,
            "time": m.timestamp,
            "type": m.media_type or "text",
            "filename": m.filename,
            "deleted": m.deleted,
        }
        if m.media_type and m.filename:
            entry["resolvable"] = container.archive.blob_exists(chat_jid, m.filename, store_dir)
        entry["my_reaction"] = my_reactions.get(m.id)
        messages_out.append(entry)

    return {
        "chat_jid": chat_jid,
        "messages": messages_out,
        "reactions": reactions,
    }


@app.post("/agents/whatsapp/send")
async def direct_send(payload: DirectSend, user_id: str = Depends(require_user)):
    """Copilot composer: propose a send without LLM involvement.
    Same approval pipeline as agent proposals."""
    result = await get_container().agent.propose_send(
        payload.recipient, payload.message, user_id, payload.quoted_message_id
    )
    status_code = {
        "approval_required": 202,
        "blocked": 422,
        "error": 503,
    }.get(result.type, 500)
    return JSONResponse(status_code=status_code, content={"type": result.type, **result.payload})


@app.get("/agents/whatsapp/actions")
async def list_actions(status: str = "pending", _: str = Depends(require_user)):
    actions = get_container().store.list_actions(status=status or None)
    return {
        "actions": [
            {
                "actionId": a.id,
                "tool": a.tool,
                "args": a.args,
                "reason": a.reason,
                "warnings": a.warnings,
                "status": a.status,
                "createdAt": a.created_at,
                "expiresAt": a.expires_at,
            }
            for a in actions
        ]
    }


@app.get("/agents/whatsapp/actions/{action_id}")
async def get_action(action_id: str, _: str = Depends(require_user)):
    store = get_container().store
    store.expire_stale()
    action = store.get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="no such action")
    return {
        "actionId": action.id,
        "tool": action.tool,
        "args": action.args,
        "status": action.status,
        "reason": action.reason,
        "warnings": action.warnings,
        "result": action.result,
    }


@app.get("/audit")
async def audit(limit: int = 100, _: str = Depends(require_user)):
    return {"events": get_container().store.get_audit(limit=min(limit, 1000))}


# --------------------------------------------------------------------- #
# Live pairing (browser QR linking)
# --------------------------------------------------------------------- #


@app.get("/agents/whatsapp/qr/meta")
async def qr_meta(_: str = Depends(require_user)):
    container = get_container()
    try:
        resp = await container.bridge.health()
        logged_in = bool(resp.data.get("logged_in"))
    except Exception:  # noqa: BLE001
        logged_in = False
    meta = await container.bridge.qr_meta()
    return {
        "available": bool(meta.get("available")),
        "generated_at": meta.get("generated_at"),
        "logged_in": logged_in,
        "bridge_up": bool(meta) or logged_in,
    }


@app.get("/agents/whatsapp/qr.png", include_in_schema=False)
async def qr_png(_: str = Depends(require_user)):
    png_bytes = await get_container().bridge.qr_png()
    if png_bytes is None:
        raise HTTPException(status_code=404, detail="no QR available yet")
    return Response(
        content=png_bytes, media_type="image/png", headers={"Cache-Control": "no-store"}
    )


_STATIC_DIR = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index_ui():
    return FileResponse(_STATIC_DIR / "index.html")


# Backwards-compatible deep link — same SPA, approvals tab is default view there.
@app.get("/approvals", include_in_schema=False)
async def approvals_ui():
    return FileResponse(_STATIC_DIR / "index.html")


# Dedicated live-QR device-linking page.
@app.get("/pair", include_in_schema=False)
async def pair_ui():
    return FileResponse(_STATIC_DIR / "pair.html")
