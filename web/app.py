"""FastAPI application factory: routers, health, metrics, SPA statics."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from config.version import VERSION
from utils.login_throttle import LoginThrottle
from utils.public_guard import PublicEndpointGuard
from utils.sessions import SessionStore
from web.routers import admin, auth, deals, feed, market
from web.schemas import HealthResponse

BASE_DIR = Path(__file__).resolve().parent.parent
SPA_DIR = BASE_DIR / "web" / "static" / "spa"


def resolve_spa_file(root: Path, relative: str) -> Path | None:
    """Return an existing file strictly inside `root`, else None (blocks `../` traversal)."""
    if not relative:
        return None
    base = root.resolve()
    candidate = (base / relative).resolve()
    if candidate.is_relative_to(base) and candidate.is_file():
        return candidate
    return None


def create_app(container) -> FastAPI:  # type: ignore[no-untyped-def]
    app = FastAPI(
        title="Stalzone Bot",
        version=VERSION,
        description="STALCRAFT: X auction monitor — scanner, analytics, trader cabinet",
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:  # P2
        """Baseline security headers on every response."""
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'",
        )
        return resp
    app.state.container = container
    # DB-backed sessions survive restarts; TTL shared with the cookie lifetime
    import asyncio

    app.state.register_lock = asyncio.Lock()
    store = getattr(container, "session_store", None)
    app.state.session_store = store or SessionStore(
        container.db, ttl_hours=container.settings.session_ttl_hours
    )

    if not getattr(container.settings, "cookie_secure", False) and getattr(
        container.settings, "web_host", "127.0.0.1"
    ) not in ("127.0.0.1", "localhost"):
        import logging

        logging.getLogger(__name__).warning(
            "cookie_secure is False while WEB_HOST is external — session cookies travel unencrypted"
        )

    app.state.login_throttle = LoginThrottle()
    # P2: sliding-window limiter + short cache for the unauthenticated
    # market/daily-prices/forecast endpoints (see utils/public_guard.py).
    app.state.public_guard = PublicEndpointGuard()

    app.include_router(auth.router)
    app.include_router(deals.router)
    app.include_router(market.router)
    app.include_router(feed.router)
    app.include_router(admin.router)

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        """F-5/F-9: public meta — one version everywhere, fee, registration mode."""
        return {
            "version": VERSION,
            "fee_pct": container.settings.fee_pct,
            "registration_open": container.settings.registration_mode in ("bootstrap", "open"),
        }

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/health/live", response_model=HealthResponse)
    async def health_live() -> HealthResponse:
        return HealthResponse(status="alive")

    @app.get("/health/ready", response_model=HealthResponse)
    async def health_ready(response: Response) -> HealthResponse:
        """Readiness: every worker heartbeat fresh + a successful scan < 5 min ago."""
        registry = getattr(container, "heartbeat", None)
        if registry is None:
            return HealthResponse(status="ready")  # web-only / test mode
        dead = registry.stale()
        scan_age = registry.age("scan")
        scan_stale = scan_age is not None and scan_age > 300.0
        if dead or scan_stale:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status=f"not_ready:{','.join(dead) or 'scan'}")
        return HealthResponse(status="ready")

    @app.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        """P2: scraper endpoint — open by default, requires ?token= when set."""
        expected = getattr(container.settings, "metrics_token", "") or ""
        if expected and request.query_params.get("token") != expected:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden")
        return PlainTextResponse(container.metrics.render().decode())

    if SPA_DIR.exists():
        app.mount(
            "/assets", StaticFiles(directory=SPA_DIR / "assets", check_dir=False), name="spa-assets"
        )

        @app.middleware("http")
        async def static_cache_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
            """Hashed assets: immutable long cache; html: no-cache (revalidate always)."""
            response = await call_next(request)
            path = request.url.path
            if path.startswith("/assets/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif path == "/" or path.endswith(".html"):
                response.headers["Cache-Control"] = "no-cache"
            return response

        @app.get("/", include_in_schema=False)
        async def spa_index() -> FileResponse:
            return FileResponse(SPA_DIR / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            # client-side routing fallback (API routes are registered above)
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
            static_file = resolve_spa_file(SPA_DIR, full_path)
            if static_file is None:
                # a path with a file extension that doesn't exist is a real miss,
                # not a client-route: return 404 instead of serving index.html
                if PurePosixPath(full_path).suffix:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
                static_file = SPA_DIR / "index.html"
            return FileResponse(static_file)

    return app
