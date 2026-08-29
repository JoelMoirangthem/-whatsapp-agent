"""Central configuration loaded from environment (optionally via .env)."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines; existing env wins."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_service_token(data_dir: Path) -> str:
    """Token from env, else from token file, else generate + persist (0600)."""
    token = os.environ.get("AGENT_SERVICE_TOKEN", "").strip()
    if token:
        return token
    token_file = Path(os.environ.get("AGENT_TOKEN_FILE", str(data_dir / ".agent-token")))
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(f"Generated agent service token at {token_file}")
    return token


def _resolve_bridge_token(bridge_token_file: str) -> str:
    token_file = Path(bridge_token_file)
    if token_file.is_file():
        try:
            return token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


@dataclass(frozen=True)
class Settings:
    # AgentRouter LLM
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_user_agent: str
    llm_timeout_seconds: float
    llm_connect_timeout_seconds: float
    llm_max_tool_rounds: int

    # Bridge
    bridge_api_url: str
    bridge_token: str

    # Storage
    messages_db_path: str
    agent_db_path: str
    whatsapp_store_dir: str

    # Auth
    service_token: str

    # Approval workflow
    pending_action_ttl_seconds: float

    # Latency observability
    latency_warn_ms: float

    # Supervisor node (intent classification + context rewriting)
    supervisor_enabled: bool

    # ElevenLabs Voice Agent (Wiki Wiki)
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model: str
    voice_wake_greeting: str
    voice_wake_word: str


def load_settings(env_file: str = ".env") -> Settings:
    _load_dotenv(env_file)
    agent_db_path = os.environ.get("AGENT_DB_PATH", "./data/agent.db")
    settings = Settings(
        llm_base_url=os.environ.get("AGENTROUTER_BASE_URL", "https://agentrouter.org/v1"),
        llm_api_key=os.environ.get("AGENTROUTER_API_KEY", ""),
        llm_model=os.environ.get("AGENTROUTER_MODEL", "gpt-5.6-sol"),
        llm_user_agent=os.environ.get("AGENTROUTER_USER_AGENT", "opencode/1.0.0"),
        llm_timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", "30")),
        llm_connect_timeout_seconds=float(os.environ.get("LLM_CONNECT_TIMEOUT_SECONDS", "2")),
        llm_max_tool_rounds=int(os.environ.get("LLM_MAX_TOOL_ROUNDS", "6")),
        bridge_api_url=os.environ.get("BRIDGE_API_URL", "http://127.0.0.1:8080/api"),
        bridge_token=_resolve_bridge_token(
            os.environ.get(
                "BRIDGE_TOKEN_FILE",
                "../whatsapp-mcp/whatsapp-bridge/store/.bridge-token",
            )
        ),
        messages_db_path=os.environ.get(
            "MESSAGES_DB_PATH", "../whatsapp-mcp/whatsapp-bridge/store/messages.db"
        ),
        whatsapp_store_dir=os.environ.get(
            "WHATSAPP_STORE_DIR", "../whatsapp-mcp/whatsapp-bridge/store"
        ),
        agent_db_path=agent_db_path,
        service_token="",
        pending_action_ttl_seconds=float(os.environ.get("PENDING_ACTION_TTL_SECONDS", "300")),
        latency_warn_ms=float(os.environ.get("LATENCY_WARN_MS", "2000")),
        supervisor_enabled=os.environ.get("SUPERVISOR_ENABLED", "true").lower()
        in ("1", "true", "yes"),
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", "").strip(),
        elevenlabs_voice_id=os.environ.get(
            "ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"
        ).strip(),
        elevenlabs_model=os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5").strip(),
        voice_wake_greeting=os.environ.get(
            "VOICE_WAKE_GREETING", "Uhu at your service, boss."
        ).strip(),
        voice_wake_word=os.environ.get("VOICE_WAKE_WORD", "uhu").strip(),
    )
    # Resolve after dataclass construction so the token file can live next to the db.
    object.__setattr__(
        settings,
        "service_token",
        _resolve_service_token(Path(agent_db_path).parent),
    )
    return settings


def load_settings_for_test(**overrides) -> Settings:  # pragma: no cover - test helper
    """Build Settings directly without touching env or disk."""
    defaults = dict(
        llm_base_url="http://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_user_agent="opencode/1.0.0",
        llm_timeout_seconds=5,
        llm_connect_timeout_seconds=1,
        llm_max_tool_rounds=2,
        bridge_api_url="http://bridge.test/api",
        bridge_token="bridge-token",
        messages_db_path=":memory:",
        agent_db_path=":memory:",
        whatsapp_store_dir=":memory:",
        service_token="svc-token",
        pending_action_ttl_seconds=300,
        latency_warn_ms=2000,
        supervisor_enabled=False,
        elevenlabs_api_key="test-11-key",
        elevenlabs_voice_id="JBFqnCBsd6RMkjVDRZzb",
        elevenlabs_model="eleven_turbo_v2_5",
        voice_wake_greeting="Uhu at your service, boss.",
        voice_wake_word="uhu",
    )
    defaults.update(overrides)
    return Settings(**defaults)
