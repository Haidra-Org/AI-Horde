# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AI Power Grid coordinator and public API.

This is the sole production coordinator runtime. Provides:
  - POST /v1/chat/completions  (OpenAI-compatible, streaming)
  - POST /v1/messages          (Anthropic-compatible, streaming)
  - POST /v1/images/generations (OpenAI-compatible image gen)
  - GET  /v1/models            (available models from connected workers)
  - WS   /v1/workers/ws        (WebSocket for text generation workers)
  - GET  /health               (health check)
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from .database import close_database, init_database
from .ratelimit import limiter
from .redis_client import close_redis, init_redis
from .routers import (
    accounts,
    anthropic,
    audio,
    health,
    images,
    metrics,
    openai,
    responses,
    stats,
    styles,
    threed,
    validator,
    videos,
    worker_enrollment,
    worker_ws,
)
from .services.p2p import close_p2p, init_p2p

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("grid_api")

# Rate limiter is the shared, Redis-backed, per-API-key limiter from
# .ratelimit (imported above). All routers use the same instance.


async def _stale_job_reclaimer():
    """Background task: periodically reclaim abandoned jobs."""
    from .services.job_queue import claim_stale_jobs
    while True:
        try:
            reclaimed = await claim_stale_jobs()
            if reclaimed:
                logger.info(f"Reclaimed {reclaimed} stale jobs")
        except Exception as e:
            logger.error(f"Stale job reclaimer error: {e}")
        await asyncio.sleep(60)  # Check every minute


async def _reservation_sweeper():
    """Background task: release reservations stuck in 'held' past a generous
    deadline — the safety net for a process crash between reserve and the
    worker-WS terminal. No-op while charging is dark. Interval/threshold via
    RESERVATION_SWEEP_SECONDS / RESERVATION_STALE_SECONDS."""
    import os
    from .services.credits import sweep_stale_reservations
    from .services.promotions import sweep_stale_spends
    interval = int(os.getenv("RESERVATION_SWEEP_SECONDS", "300") or 300)
    stale = int(os.getenv("RESERVATION_STALE_SECONDS", "3600") or 3600)
    while True:
        try:
            await sweep_stale_reservations(older_than_seconds=stale)
            await sweep_stale_spends(older_than_seconds=stale)
        except Exception as e:
            logger.error(f"Reservation sweeper error: {e}")
        await asyncio.sleep(interval)


async def _recipe_sync_loop():
    """Background task: refresh approved recipes from on-chain RecipeVault.
    No-ops until RECIPEVAULT_ADDRESS/BASE_RPC_URL are set; curated local recipes
    loaded at startup remain servable regardless. Interval via RECIPE_SYNC_SECONDS."""
    import os
    from .services.recipes import sync_from_recipevault
    interval = int(os.getenv("RECIPE_SYNC_SECONDS", "600") or 600)
    while True:
        try:
            await sync_from_recipevault()
        except Exception as e:
            logger.error(f"Recipe sync loop error: {e}")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Starting Grid Streaming API...")
    await init_database()
    await init_redis()
    await init_p2p()  # Initialize P2P (no-op if disabled)
    reclaimer = asyncio.create_task(_stale_job_reclaimer())
    # Media recipes: load curated local recipes now (servable immediately), then
    # refresh from RecipeVault on an interval (no-op until BASE_RPC/addr configured).
    import os
    from .services import recipes as _recipes
    from .services import loras as _loras
    from .services import styles as _styles
    _base = os.path.dirname(os.path.dirname(__file__))
    _recipes.load_local_recipes(os.path.join(_base, "recipes"))
    _loras.load_blacklist(os.path.join(_base, "lora_blacklist.json"))
    # Styles: curated creative presets that compose over recipes. Served at
    # /v1/styles and applied server-side when a request carries `style`.
    _styles.load_local_styles(os.path.join(_base, "styles"))
    recipe_sync = asyncio.create_task(_recipe_sync_loop())
    sweeper = asyncio.create_task(_reservation_sweeper())
    # Verification probes ("validator zero") — dormant unless GRID_PROBE_ENABLED;
    # even ON it only records evidence (no reward/slash). See VERIFICATION_PROBES.md.
    from .services import probe as _probe
    prober = asyncio.create_task(_probe.probe_loop())
    logger.info("Grid Streaming API ready.")
    yield
    logger.info("Shutting down Grid Streaming API...")
    reclaimer.cancel()
    recipe_sync.cancel()
    sweeper.cancel()
    prober.cancel()
    await close_p2p()  # Shutdown P2P
    await close_redis()
    await close_database()


app = FastAPI(
    title="AI Power Grid — Streaming API",
    description="OpenAI and Anthropic compatible endpoints with real token streaming.",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": {"message": "Rate limit exceeded. Please slow down.", "type": "rate_limit_error"}},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Auth is via the Authorization/apikey HEADER, never cookies — so credentials must be
    # FALSE. "*" + allow_credentials=True is a footgun: Starlette then reflects the caller's
    # Origin AND sets Allow-Credentials:true, making every origin a trusted credentialed one.
    # With credentials off, the public API stays callable cross-origin (header auth works)
    # without that reflection. Authorization is allowed via allow_headers below.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(openai.router)
app.include_router(anthropic.router)
app.include_router(responses.router)
app.include_router(images.router)
app.include_router(videos.router)
app.include_router(audio.router)
app.include_router(threed.router)
app.include_router(worker_ws.router)
app.include_router(worker_enrollment.router)
app.include_router(stats.router)
app.include_router(styles.router)
app.include_router(validator.router)
app.include_router(accounts.router)
app.include_router(health.router)
app.include_router(metrics.router)


@app.get("/")
async def root():
    from .services.p2p import get_p2p_node, get_p2p_config

    p2p_config = get_p2p_config()
    p2p_node = get_p2p_node()

    return {
        "name": "AI Power Grid — Streaming API",
        "version": "1.0.0",
        "endpoints": {
            "openai": "POST /v1/chat/completions",
            "anthropic": "POST /v1/messages",
            "images": "POST /v1/images/generations",
            "audio": "POST /v1/audio/generations",
            "models": "GET /v1/models",
            "worker_ws": "WS /v1/workers/ws",
            "health": "GET /health",
        },
        "p2p": {
            "enabled": p2p_config.enabled,
            "peer_id": p2p_node.peer_id if p2p_node else None,
            "status": "running" if p2p_node and p2p_node.running else "disabled",
        },
    }
