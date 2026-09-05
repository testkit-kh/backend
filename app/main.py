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
from app.certificates import router as certificates_router
from app.config import settings
from app.consent import router as consent_router
from app.course import router as course_router
from app.database import engine
from app.events import router as events_router
from app.hypotheses import router as hypotheses_router
from app.logging_config import configure_logging
from app.ml import router as ml_router
from app.monitoring import router as monitoring_router
from app.notifications import router as notifications_router
from app.organizations import router as organizations_router
from app.parcels import router as parcels_router
from app.public import router as public_router
from app.registry.router import router as registry_router
from app.satellite.router import router as satellite_router
from app.scheduler import shutdown_scheduler, start_scheduler
from app.uploads import router as uploads_router
from app.users import router as users_router
from app.volunteers import router as volunteers_router

# До первого обращения к rosreestr2coord: иначе корневой логгер заберёт она.
# Подробности — в app/logging_config.py.
configure_logging()


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
app.include_router(events_router)
app.include_router(monitoring_router)
app.include_router(course_router)
app.include_router(certificates_router)
app.include_router(notifications_router)
app.include_router(consent_router)
app.include_router(parcels_router)
app.include_router(registry_router)
app.include_router(analytics_router)
app.include_router(organizations_router)
app.include_router(public_router)
app.include_router(users_router)
app.include_router(volunteers_router)
app.include_router(uploads_router)
app.include_router(ml_router)
app.include_router(satellite_router)


@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "eco-project-mvp", "env": settings.ENV}


@app.get("/health", tags=["health"])
async def health():
    """Readiness probe — verifies the database round-trips."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
