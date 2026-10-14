"""Configuration, read once from the environment.

Secrets (database password, signing and encryption keys, the LLM key, the first
admin's password) arrive as env vars populated from Kubernetes Secrets. None of
them has a default, so a missing Secret stops the pod at startup instead of
running with a known value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _database_url() -> str:
    if url := os.environ.get("DATABASE_URL", "").strip():
        return url
    # Built here from its parts so the password never has to be templated into
    # a connection string inside a manifest.
    return "postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}".format(
        user=quote(_required("DB_USER"), safe=""),
        password=quote(_required("DB_PASSWORD"), safe=""),
        host=_required("DB_HOST"),
        port=os.environ.get("DB_PORT", "5432"),
        name=_required("DB_NAME"),
    )


@dataclass(frozen=True)
class Settings:
    database_url: str
    # Signs dashboard session cookies.
    session_secret: str
    # Encrypts upstream credentials before they are written to the database.
    encryption_key: str
    # Created on first start if no user exists yet.
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # The address people use to reach the gateway, e.g. http://gateway.localtest.me
    public_url: str = "http://localhost:8080"
    cookie_secure: bool = False
    session_ttl_hours: int = 12
    invite_ttl_hours: int = 72

    # Servers registered automatically at startup (JSON file from a Secret).
    seed_servers_file: str = ""

    # --- Upstream behaviour ---
    # How long one tool call may take before the caller gets a timeout error.
    call_timeout: float = 30.0
    # How long connecting to an upstream and listing its tools may take.
    connect_timeout: float = 8.0
    # How often each upstream is re-checked and its tool list refreshed.
    refresh_interval: float = 5.0
    # How often the registry in the database is compared with what is running.
    reconcile_interval: float = 5.0
    # Longest wait between reconnect attempts to a dead upstream.
    max_backoff: float = 20.0

    # --- Adding upstreams by URL (SSRF protection) ---
    # Hostname suffixes that may resolve to private addresses, for upstreams
    # that live inside the cluster. Everything else must be a public address.
    internal_host_suffixes: tuple[str, ...] = ()
    allow_plain_http_public: bool = False

    # --- Rate limits (0 turns a limit off) ---
    # Requests per minute per API key on the MCP endpoint.
    mcp_rate_limit_per_minute: int = 240
    # Chat messages per minute per user.
    chat_rate_limit_per_minute: int = 12

    # --- Chat agent ---
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_api_key: str = ""
    llm_timeout: float = 60.0
    # Longest reply the model may write in one step.
    llm_max_tokens: int = 1024
    # Sent as "reasoning_effort" when set. "low" suits gpt-oss models; leave
    # empty for models that do not accept the parameter.
    llm_reasoning_effort: str = ""
    chat_max_steps: int = 8
    # Tool results longer than this are cut before they go back to the model.
    chat_tool_result_chars: int = 5000

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        suffixes = tuple(s.strip().lower() for s in env("INTERNAL_HOST_SUFFIXES", "").split(",") if s.strip())
        return cls(
            database_url=_database_url(),
            session_secret=_required("SESSION_SECRET"),
            encryption_key=_required("ENCRYPTION_KEY"),
            bootstrap_admin_email=env("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower(),
            bootstrap_admin_password=env("BOOTSTRAP_ADMIN_PASSWORD", ""),
            public_url=env("PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            cookie_secure=_flag("COOKIE_SECURE", False),
            session_ttl_hours=int(env("SESSION_TTL_HOURS", "12")),
            invite_ttl_hours=int(env("INVITE_TTL_HOURS", "72")),
            seed_servers_file=env("SEED_SERVERS_FILE", ""),
            call_timeout=float(env("UPSTREAM_CALL_TIMEOUT", "30")),
            connect_timeout=float(env("UPSTREAM_CONNECT_TIMEOUT", "8")),
            refresh_interval=float(env("UPSTREAM_REFRESH_INTERVAL", "5")),
            reconcile_interval=float(env("UPSTREAM_RECONCILE_INTERVAL", "5")),
            max_backoff=float(env("UPSTREAM_MAX_BACKOFF", "20")),
            internal_host_suffixes=suffixes,
            allow_plain_http_public=_flag("ALLOW_PLAIN_HTTP_PUBLIC", False),
            mcp_rate_limit_per_minute=int(env("MCP_RATE_LIMIT_PER_MINUTE", "240")),
            chat_rate_limit_per_minute=int(env("CHAT_RATE_LIMIT_PER_MINUTE", "12")),
            llm_base_url=env("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            llm_model=env("LLM_MODEL", "openai/gpt-oss-120b"),
            llm_api_key=env("LLM_API_KEY", "").strip(),
            llm_timeout=float(env("LLM_TIMEOUT", "60")),
            llm_max_tokens=int(env("LLM_MAX_TOKENS", "1024")),
            llm_reasoning_effort=env("LLM_REASONING_EFFORT", "").strip(),
            chat_max_steps=int(env("CHAT_MAX_STEPS", "8")),
            chat_tool_result_chars=int(env("CHAT_TOOL_RESULT_CHARS", "5000")),
        )
