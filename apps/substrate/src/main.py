import logging
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .crypto import validate_encryption_config
from .routes import router
from .cache import get_redis
from .vector_listener import ListenerHandle

# No composition-root import of finance's own module lives here: routes.py
# is what needs finance's router and integrity-error mapper registered
# eagerly (see its own comment), so importing it above already pulls
# finance_schemas in — this module never names a finance symbol.

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Bead Substrate", version="0.1.4")

# CORS: browser clients (lifeops-console) hit Substrate directly with
# X-API-Key. Origins are pinned via SUBSTRATE_CORS_ORIGINS (comma-separated)
# so we never fall back to "*" — wildcards would be a credential-leak risk
# if the API key ever ended up in a misconfigured browser context.
_cors_env = os.environ.get("SUBSTRATE_CORS_ORIGINS", "http://localhost:5173")
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type"],
)
logger.info("CORS: allowed origins = %s", _cors_origins)

# Lifecycle managers
_vector_listener = ListenerHandle()

@app.on_event("startup")
async def startup_event():
    # 1. Validate Encryption (Phase 5 ALE)
    try:
        validate_encryption_config()
        logger.info("ALE: Encryption configuration validated.")
    except Exception as e:
        logger.error(f"ALE: Configuration validation failed: {e}")
        # In a real production app, we might want to exit here
        # but let's allow the app to start so we can see logs if needed.
        # However, Phase 5 design says "fail fast".
        raise e

    # 2. Start Vector Listener (Phase 4.3)
    if os.environ.get("DISABLE_VECTOR_LISTENER", "").lower() in {"1", "true", "yes"}:
        logger.info("Vector listener disabled by DISABLE_VECTOR_LISTENER.")
    else:
        await _vector_listener.start()
        logger.info("Vector listener task started.")

    # 3. Initialize Redis Cache (Phase 8 Architect)
    await get_redis()

@app.on_event("shutdown")
async def shutdown_event():
    await _vector_listener.stop()
    logger.info("Substrate shutdown complete.")

@app.get("/health")
async def health():
    return {"status": "healthy"}

# Include modular routes. Namespace-registered routers (finance's summary
# endpoint, the rules dry-run router) are nested inside `router` itself --
# see the bottom of routes.py -- so this one call mounts everything.
app.include_router(router)
