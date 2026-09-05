"""
FastAPI application entry point.

Run locally:
    alembic upgrade head
    uvicorn app.main:app --reload

Schema is owned by Alembic — the app never calls create_all().
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.analytics.dashboards import router as analytics_router
from app.auth import router as auth_router
from app.config import settings
from app.consent import router as consent_router
from app.course import router as course_router
from app.database import engine
from app.hypotheses import router as hypotheses_router
from app.monitoring import router as monitoring_router
from app.notifications import router as notifications_router
from app.parcels import router as parcels_router
from app.registry.router import router as registry_router
from app.scheduler import shutdown_scheduler, start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Планировщик напоминаний живёт столько же, сколько приложение.

    Схемой БД он не занимается — она принадлежит Alembic; здесь только
    фоновые задачи.
    """
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(
    title="Eco-Project MVP",
    description="Backend API for the ecological volunteer / ООПТ staff platform.",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(hypotheses_router)
app.include_router(monitoring_router)
app.include_router(course_router)
app.include_router(notifications_router)
app.include_router(consent_router)
app.include_router(parcels_router)
app.include_router(registry_router)
app.include_router(analytics_router)


@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "eco-project-mvp", "env": settings.ENV}


@app.get("/health", tags=["health"])
async def health():
    """Readiness probe — verifies the database round-trips."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
