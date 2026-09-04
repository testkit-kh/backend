"""
Application configuration via pydantic-settings.
Reads from environment variables / .env file.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Core ---------------------------------------------------------------
    ENV: str = "dev"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/eco_project"

    # --- Auth ---------------------------------------------------------------
    SECRET_KEY: str = "change-me-to-a-random-secret-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # --- CORS ---------------------------------------------------------------
    # Comma-separated list in the environment, e.g.
    # CORS_ORIGINS=http://localhost:5173,https://chistyi-bereg.ru
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # --- Course (iSpring) ---------------------------------------------------
    COURSE_SIGNUP_URL: str = (
        "https://zaprirodu.ispring.ru/signup/LvEGzxy0_owor3LFnvf7L2qVk5I"
    )

    # --- Object storage (MinIO / S3) ---------------------------------------
    S3_ENDPOINT_URL: str = "http://minio:9000"
    S3_PUBLIC_URL: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET: str = "chistyi-bereg"

    # --- Metabase embedding -------------------------------------------------
    METABASE_SITE_URL: str = "http://localhost:3000"
    METABASE_EMBEDDING_SECRET_KEY: str = ""
    METABASE_EMBED_TOKEN_TTL_MINUTES: int = 10

    # --- External registries ------------------------------------------------
    DADATA_API_KEY: str = ""
    DADATA_SECRET_KEY: str = ""

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept both a JSON list and a plain comma-separated string."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


settings = Settings()
