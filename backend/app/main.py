"""
PredictOps — AI Predictive Maintenance & OEE Command Center
FastAPI Application Entry Point
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.database import init_db
from app.routes import machines, alerts, workorders, oee, erp, ingest, impact, investigate, auth, model, stream, datasets, report
from app.schemas import HealthOut
from app.security import verify_startup_configuration

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    logger.info("PredictOps starting up...")

    # Refuse to serve an unsafe configuration: a placeholder signing key, or a
    # non-development environment with authentication switched off. Raised before
    # anything binds, so the failure is loud rather than latent.
    verify_startup_configuration()

    # Create DB tables
    await init_db()
    logger.info("Database initialized")

    # Demo operator accounts. Idempotent, and only ever creates accounts that do
    # not exist, so a real deployment that manages its own users is unaffected.
    from app.services.user_seed import seed_demo_users
    await seed_demo_users()

    # A deployment starts against an empty database, so the fleet has to be created
    # before anything can be derived from it. Seeding inserts ~43,000 sensor
    # readings and is therefore run as a background task: blocking startup on it
    # would trip the platform health check before the port ever opened.
    #
    # Baseline backfill is chained behind seeding rather than run beside it, because
    # on a fresh database there would be no machines to derive baselines for yet. On
    # an already-seeded database the seed step returns immediately and this behaves
    # as it did before.
    async def _prepare_data() -> None:
        try:
            from app.services.demo_seed import seed_demo_fleet_if_empty
            await seed_demo_fleet_if_empty()
        except Exception as exc:
            logger.warning(f"Demo seed step failed: {exc}")

        # Derive observed baselines for any machine lacking one, demo fleet included.
        # The same code populates the same fields regardless of data origin, so the
        # built-in data is not treated differently from an upload.
        try:
            from app.database import AsyncSessionLocal
            from app.services.baseline_service import backfill_all_baselines
            async with AsyncSessionLocal() as db:
                await backfill_all_baselines(db)
        except Exception as exc:
            logger.warning(f"Baseline backfill skipped: {exc}")

    prepare_task = asyncio.create_task(_prepare_data())

    # Open the Cortex connection now. The handshake measured ~18s cold against ~2.5s
    # warm, and that cost would otherwise land on whoever asked the first question.
    try:
        from app.llm.llm_client import warm_up
        asyncio.create_task(warm_up())
    except Exception as exc:
        logger.info(f"LLM warm-up not started: {exc}")

    # Start live feed background task
    from app.services.live_feed import live_feed_loop
    live_feed_task = asyncio.create_task(
        live_feed_loop(interval_seconds=settings.live_feed_interval_seconds)
    )
    logger.info(f"Live feed started ({settings.live_feed_interval_seconds}s interval)")

    # Modbus TCP ingestion, only when explicitly enabled. Starting it by default
    # would make a fresh clone dial out to MODBUS_HOST on a loop.
    background_tasks = [live_feed_task, prepare_task]
    if settings.modbus_enabled:
        try:
            from app.services.modbus_source import modbus_poll_loop
            modbus_task = asyncio.create_task(modbus_poll_loop())
            background_tasks.append(modbus_task)
            logger.info(
                "Modbus poller started (%s:%s every %ss)",
                settings.modbus_host, settings.modbus_port,
                settings.modbus_poll_seconds,
            )
        except ImportError as exc:
            # pymodbus is a real dependency but an optional capability. A missing
            # install should disable the feature, not stop the API from serving.
            logger.warning("Modbus enabled but unavailable: %s", exc)

    yield

    # Shutdown
    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("PredictOps shutdown complete")


app = FastAPI(
    title="PredictOps API",
    description="AI Predictive Maintenance & OEE Command Center",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — an explicit allowlist, not a wildcard.
#
# `allow_origins=["*"]` with `allow_credentials=True` is invalid per the Fetch
# standard (a browser rejects `Access-Control-Allow-Origin: *` on a credentialed
# request), so the previous setting did not do what it looked like it did. Worse,
# it let any page a developer visited issue cross-origin calls to this API and
# read the responses — including mutations, which are unauthenticated by default.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline response hardening.

    `/api/report/html` returns a document built from user-supplied machine names
    and CSV headers. Those values are escaped at the point of interpolation, but a
    single missed `_esc` would otherwise be script execution on this origin, so the
    policy denies scripts outright as a second line of defence. Routes may set
    their own Content-Security-Policy and it is left intact.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    )
    return response

# Register routes
app.include_router(machines.router)
app.include_router(alerts.router)
app.include_router(workorders.router)
app.include_router(oee.router)
app.include_router(erp.router)
app.include_router(ingest.router)
app.include_router(impact.router)
app.include_router(investigate.router)
app.include_router(auth.router)
app.include_router(model.router)
app.include_router(stream.router)
app.include_router(datasets.router)
app.include_router(report.router)


# ─── Error Handling (spec §47) ────────────────────────────────────────────────
# Every error is returned as a structured {code, message} envelope. Internal
# failures are logged server-side in full but never leak details, connection
# strings, or credentials to the client.

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "REQUEST_FAILED"
    # Codes like MACHINE_NOT_FOUND are already machine-readable; otherwise
    # derive a generic code from the status.
    code = detail if detail.isupper() and " " not in detail else f"HTTP_{exc.status_code}"
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": code, "message": detail, "detail": detail},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "errors": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred. Please retry.",
        },
    )


@app.get("/api/health", response_model=HealthOut)
async def health_check():
    """Health check endpoint — always returns 200 if app is running."""
    try:
        from app.database import engine
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "error"

    # Report whether the ML model is loadable. If it is not, scoring falls back
    # to the rules-based estimate and the system is running degraded.
    #
    # `get_model` and not `load_model`: the latter re-reads and deserialises the
    # artefact on every call. A platform health check runs every few seconds, so
    # that reloaded a 240 KB pickle thousands of times a day and filled the log
    # with "Model loaded from ..." on a free instance with little CPU to spare.
    try:
        from app.ml.predict import get_model
        get_model()
        ml_status = "loaded"
    except Exception:
        ml_status = "unavailable"

    # Seeding state and fleet size. A deployment whose fleet is empty because
    # seeding failed looks identical to a working one on every other field.
    try:
        from app.services.demo_seed import seed_status
        from app.database import AsyncSessionLocal
        from app.models.machine import Machine
        from sqlalchemy import func, select

        seed = seed_status()
        async with AsyncSessionLocal() as db:
            machine_count = (
                await db.execute(select(func.count()).select_from(Machine))
            ).scalar() or 0
    except Exception:
        seed = {"status": "unknown", "detail": None}
        machine_count = 0

    return HealthOut(
        status="ok",
        demo_mode=settings.demo_mode,
        database=db_status,
        ml_model=ml_status,
        degraded_mode=(ml_status != "loaded"),
        seed=seed.get("status", "unknown"),
        seed_detail=seed.get("detail"),
        machines=machine_count,
    )


if __name__ == "__main__":
    import uvicorn
    # Loopback by default. Binding 0.0.0.0 published an API whose mutating
    # endpoints are unauthenticated out of the box to the whole local network;
    # set HOST explicitly to opt into that.
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
    )
