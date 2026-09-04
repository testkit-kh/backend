"""
FastAPI application entry point.

Run:
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth import router as auth_router
from app.hypotheses import router as hypotheses_router
from app.database import engine, Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables on startup (dev convenience). Use Alembic in production."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Eco-Project MVP",
    description="Backend API for the ecological volunteer / ООПТ staff platform.",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(auth_router)
app.include_router(hypotheses_router)



@app.get("/", tags=["health"])
async def root():
    return {"status": "ok", "service": "eco-project-mvp"}
