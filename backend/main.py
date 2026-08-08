"""FastAPI entrypoint. Render web service - the frontend is a separate Netlify
deploy, so nothing here serves a built frontend bundle by default.

Run from the repository root:
    uvicorn backend.main:app --host 0.0.0.0 --port $PORT
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from backend import __version__
from backend.api import router as api_router
from backend.config import settings

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger("aco")

# Frontend build output, served for the single-origin deployment option.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit_public])

app = FastAPI(
    title="Agent Council Orchestrator",
    version=__version__,
    # Interactive docs are a dev affordance, not something to expose in prod.
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
# FRONTEND_ORIGIN is the production Netlify (or custom) domain, exact match.
# The regex additionally covers Netlify's preview subdomains, which use two
# different shapes (deploy-preview-12--site.netlify.app,
# branch-name--site.netlify.app) - matching on the .netlify.app suffix rather
# than either shape specifically is what makes both work. This is a specific
# suffix pattern, not the literal "*" wildcard, so it is safe to combine with
# allow_credentials=True (Starlette reflects the matched origin, never "*").
_cors_origins = list(settings.cors_origin_list)
if settings.frontend_origin and settings.frontend_origin not in _cors_origins:
    _cors_origins.append(settings.frontend_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"^https://[a-zA-Z0-9-]+\.netlify\.app$",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Full detail to server logs, generic message to the client.

    Stack traces, file paths and raw DB errors must never reach a response body.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": "An internal error occurred."},
    )


@app.get("/health", tags=["ops"])
@limiter.exempt
async def health(request: Request) -> dict[str, str]:
    """Liveness probe. Deliberately leaks no configuration."""
    return {"status": "ok", "version": __version__}


app.include_router(api_router)


# Mounted last so it cannot shadow /health or future /api routes.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    log.warning("frontend build not found at %s - serving API only", FRONTEND_DIST)
