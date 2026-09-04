"""
FastAPI application entry point.

Run locally:
    alembic upgrade head
    uvicorn app.main:app --reload

Schema is owned by Alembic — the app never calls create_all().
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.auth import router as auth_router
from app.config import settings
from app.database import engine
from app.hypotheses import router as hypotheses_router
from app.monitoring import router as monitoring_router

app = FastAPI(
    title="Eco-Project MVP",
    description="Backend API for the ecological volunteer / ООПТ staff platform.",
    version="0.3.0",
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


@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "eco-project-mvp", "env": settings.ENV}


@app.get("/health", tags=["health"])
async def health():
    """Readiness probe — verifies the database round-trips."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
