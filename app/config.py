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
    LOG_LEVEL: str = "INFO"

    # --- Auth ---------------------------------------------------------------
    SECRET_KEY: str = "change-me-to-a-random-secret-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # --- Refresh-токены (P1-7) ----------------------------------------------
    # Access-токен — подписанный JWT: сервер не спрашивает о нём базу и
    # потому не может его отозвать. Отзыв висит на refresh-токене, который
    # лежит в БД. Отсюда и разделение: access живёт минуты, refresh — недели.
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    REFRESH_COOKIE_NAME: str = "refresh_token"
    #: Кука уходит только на /auth/*. Остальному API refresh не нужен, а чем
    #: меньше запросов её несут, тем меньше шансов утечки.
    REFRESH_COOKIE_PATH: str = "/auth"
    #: False допустим только для http://localhost. В развёрнутом окружении —
    #: True, иначе кука ходит по сети в открытом виде.
    REFRESH_COOKIE_SECURE: bool = True
    REFRESH_COOKIE_SAMESITE: str = "lax"
    #: Пусто — host-only кука; это то, что нужно, когда фронт и API на одном
    #: домене.
    REFRESH_COOKIE_DOMAIN: str = ""

    # --- Инвайты сотрудников (P1-6) -----------------------------------------
    # Трое суток: код передают человеку в мессенджере или голосом, он должен
    # пережить выходные, но не жить в переписке месяцами.
    STAFF_INVITE_TTL_HOURS: int = 72
    #: Код регистрации координатора. Пусто — ручка выключена: координатор
    #: иначе регистрировался бы как волонтёр с правами на все согласия.
    COORDINATOR_INVITE_CODE: str = ""

    # --- Загрузки (P0-2) ----------------------------------------------------
    UPLOAD_PRESIGN_EXPIRE_SECONDS: int = 600

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
    #: Куда библиотеке разрешено писать свои временные файлы. Пусто —
    #: подкаталог системного temp. Рабочая директория не годится: в контейнере
    #: это /app, и он принадлежит root, а процесс работает под appuser.
    ROSREESTR_TMP_DIR: str = ""

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

    # --- ML-сервис (детекция мусора) ----------------------------------------
    # Отдельный хост (обычно ml.{DOMAIN} на машине с GPU). Пустой BASE_URL
    # или ML_ENABLED=false — ручки сканов отвечают 503, остальной API живёт.
    ML_ENABLED: bool = True
    ML_BASE_URL: str = ""
    ML_API_KEY: str = ""
    #: Инференс на CPU может идти минуты; GPU — единицы секунд.
    ML_TIMEOUT_S: float = 180.0

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept both a JSON list and a plain comma-separated string."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


settings = Settings()
