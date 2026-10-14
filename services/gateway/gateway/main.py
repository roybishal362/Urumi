"""The gateway application: REST API for the dashboard, plus the MCP endpoint.

Run with:  uvicorn gateway.main:create_app --factory --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.routing import Route

from . import __version__
from .aggregator import Aggregator
from .api import activity, auth, keys, members, servers
from .api.deps import AppState
from .chat import LLMClient
from .config import Settings
from .db import Database, SeedRecord, UpstreamServer, User, bump_registry_version
from .mcp_endpoint import McpEndpoint
from .ratelimit import RateLimiter
from .security import CredentialCipher, hash_password
from .upstreams import UpstreamManager, UrlCheck

logger = logging.getLogger("gateway")


async def bootstrap_admin(db: Database, settings: Settings) -> None:
    """Create the first admin from the Secret, once, when there are no users at all."""
    if not (settings.bootstrap_admin_email and settings.bootstrap_admin_password):
        return
    async with db.session() as session:
        if (await session.execute(select(func.count()).select_from(User))).scalar_one() > 0:
            return
        session.add(
            User(
                email=settings.bootstrap_admin_email,
                name="Admin",
                role="admin",
                password_hash=hash_password(settings.bootstrap_admin_password),
            )
        )
        try:
            await session.commit()
            logger.info("created the first admin: %s", settings.bootstrap_admin_email)
        except IntegrityError:
            # Another replica starting at the same moment got there first.
            await session.rollback()


async def apply_seeds(db: Database, cipher: CredentialCipher, settings: Settings) -> None:
    """Register the servers listed in the seed file (mounted from a Secret).

    This is how the WooCommerce MCP server is already there on first login.
    Each slug is seeded once; from then on the dashboard is the source of
    truth, so an admin's edits and removals survive restarts.
    """
    path = Path(settings.seed_servers_file) if settings.seed_servers_file else None
    if path is None or not path.is_file():
        return
    try:
        seeds = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        logger.error("seed file %s is not valid JSON; skipping", path)
        return

    async with db.session() as session:
        for seed in seeds:
            slug = seed["slug"]
            if await session.get(SeedRecord, slug) is not None:
                continue
            credential = seed.get("credential") or None
            if seed.get("requires_credential") and not credential:
                # Not recorded as applied, so a later start that does have the
                # credential (for example after the store chart is installed) seeds it.
                logger.warning("seed '%s' has no credential yet; it will be retried on the next start", slug)
                continue
            exists = (await session.execute(select(UpstreamServer.id).where(UpstreamServer.slug == slug))).first()
            if not exists:
                auth_type = seed.get("auth_type", "none") if credential else "none"
                session.add(
                    UpstreamServer(
                        slug=slug,
                        name=seed.get("name", slug),
                        url=seed["url"],
                        auth_type=auth_type,
                        auth_header=seed.get("auth_header") if auth_type == "header" else None,
                        credential_enc=cipher.encrypt(credential) if credential else None,
                        enabled=seed.get("enabled", True),
                    )
                )
                logger.info("seeded upstream server '%s'", slug)
            session.add(SeedRecord(slug=slug))
        await bump_registry_version(session)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()


def create_app(
    settings: Settings | None = None,
    *,
    url_check: UrlCheck | None = None,
    llm_transport: httpx2.AsyncBaseTransport | None = None,
) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # These log every request at INFO, which the periodic upstream checks turn into noise.
    for noisy in ("httpx2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = settings or Settings.from_env()
    db = Database(settings.database_url)
    cipher = CredentialCipher(settings.encryption_key)
    manager = UpstreamManager(settings, db, cipher, url_check)
    aggregator = Aggregator(manager, db, settings)
    llm = LLMClient(settings, llm_transport)
    mcp = McpEndpoint(aggregator, db, RateLimiter(settings.mcp_rate_limit_per_minute))
    state = AppState(
        settings=settings,
        db=db,
        cipher=cipher,
        manager=manager,
        aggregator=aggregator,
        llm=llm,
        chat_limiter=RateLimiter(settings.chat_rate_limit_per_minute),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with db.startup_lock():
            await db.create_tables()
            await bootstrap_admin(db, settings)
            await apply_seeds(db, cipher, settings)
        await manager.start()
        async with mcp.sessions.run():
            # Give every upstream one connection attempt before reporting
            # ready, so the first tool listing after a rollout is not empty.
            await manager.wait_until_settled(settings.connect_timeout + 2)
            state.ready = True
            logger.info("gateway %s ready", __version__)
            yield
            state.ready = False
        await manager.stop()
        await llm.aclose()
        await db.dispose()

    app = FastAPI(
        title="Urumi team MCP gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.gateway = state

    for module in (auth, servers, members, keys, activity):
        app.include_router(module.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        # Ready means started up and able to reach its database. An upstream
        # being down does not make the gateway unready.
        ok = state.ready and await db.ping()
        return JSONResponse({"status": "ready" if ok else "not ready"}, status_code=200 if ok else 503)

    @app.get("/.well-known/{path:path}", include_in_schema=False)
    async def no_oauth_metadata(path: str) -> JSONResponse:
        # MCP clients that receive a 401 look for OAuth metadata here. This
        # gateway authenticates with API keys, so the honest answer is "none":
        # the client then reports the 401 instead of starting a login flow.
        return JSONResponse(
            {"error": "not_found", "error_description": "This gateway uses API keys. Create one in the dashboard."},
            status_code=404,
        )

    # The MCP endpoint is a raw ASGI app, so the SDK handles the protocol itself.
    app.router.routes.append(Route("/mcp", endpoint=mcp))
    return app
