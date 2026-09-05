"""
Application configuration via pydantic-settings.
Reads from environment variables / .env file.
"""

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # --- Course (iSpring) ---------------------------------------------------
    COURSE_SIGNUP_URL: str = "https://zaprirodu.ispring.ru/signup/LvEGzxy0_owor3LFnvf7L2qVk5I"

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
    # Номера дашбордов появляются только после провижининга
    # (scripts/metabase_seed.py) и потому задаются переменными окружения,
    # а не зашиваются в код. 0 означает «ещё не создан» — ручка встраивания
    # честно отвечает 503 вместо неработающего iframe.
    METABASE_DASHBOARD_FUNNEL: int = 0
    METABASE_DASHBOARD_OOPT: int = 0
    METABASE_DASHBOARD_IMPACT: int = 0

    # --- External registries ------------------------------------------------
    # Основной источник сведений об организациях — ЕГРЮЛ ФНС: без ключа, без
    # лимита. DaData подключается только если ключ задан, и остаётся
    # запасным вариантом, а не обязательной зависимостью.
    REGISTRY_TIMEOUT_SECONDS: float = 12.0
    #: Сколько дней ответу реестра считаться свежим. Сведения об организации
    #: меняются раз в годы, а вот доступность ЕГРЮЛ — каждый день.
    REGISTRY_CACHE_TTL_DAYS: int = 30
    DADATA_API_KEY: str = ""
    DADATA_SECRET_KEY: str = ""

    # --- Росреестр (ФГИС ЕГРН) ----------------------------------------------
    # WAF ФГИС режет запросы по IP: из-за рубежа и из части корпоративных
    # сетей приходит 403. Где доступа нет — выключаем, чтобы не ждать
    # таймаута на каждом участке; границы тогда вводятся вручную.
    ROSREESTR_ENABLED: bool = True

    # --- Напоминания ---------------------------------------------------------
    # Планировщик крутится внутри процесса API. На тестах и в CI не нужен:
    # там нет ни адресатов, ни смысла что-то рассылать.
    REMINDERS_ENABLED: bool = True
    REMINDERS_INTERVAL_MINUTES: int = 60

    # --- Прибрежная буферная зона -------------------------------------------
    # «Прибрежная», а не «морская»: в географии проекта есть Байкал, Ладога и
    # Каспий. Точка волонтёра относится к ООПТ, если лежит в её границах или
    # в пределах этого буфера от них.
    COASTAL_BUFFER_KM: float = 20.0

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept both a JSON list and a plain comma-separated string."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


settings = Settings()
