"""
HTTP-клиент к ML-микросервису детекции мусора.

Сервис stateless и живёт на отдельном хосте (ml.{DOMAIN}). Бэкенд — единственный
потребитель с API-ключом; фронт ML напрямую не зовёт.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class MlUnavailable(Exception):
    """ML выключен, не сконфигурирован или не отвечает."""

    def __init__(self, detail: str, *, status_code: int = 503) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _base_url() -> str:
    url = (settings.ML_BASE_URL or "").rstrip("/")
    if not settings.ML_ENABLED or not url:
        raise MlUnavailable("ML-сервис не сконфигурирован (ML_BASE_URL / ML_ENABLED).")
    return url


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if settings.ML_API_KEY:
        headers["X-API-Key"] = settings.ML_API_KEY
    return headers


async def health() -> dict[str, Any]:
    """Прокси GET /health. Не требует API-ключа на стороне ML."""
    base = _base_url()
    try:
        async with httpx.AsyncClient(timeout=min(30.0, settings.ML_TIMEOUT_S)) as client:
            response = await client.get(f"{base}/health", headers=_headers())
    except httpx.RequestError as exc:
        log.warning("ML health unreachable: %s", exc)
        raise MlUnavailable(f"ML-сервис недоступен: {exc}") from exc

    if response.status_code >= 500:
        raise MlUnavailable(
            f"ML-сервис ответил {response.status_code}",
            status_code=502,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MlUnavailable("ML /health вернул не-JSON", status_code=502) from exc


async def detect_area(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/v1/imagery/detect/area — bbox → детекции + кандидаты."""
    base = _base_url()
    try:
        async with httpx.AsyncClient(timeout=settings.ML_TIMEOUT_S) as client:
            response = await client.post(
                f"{base}/api/v1/imagery/detect/area",
                json=payload,
                headers=_headers(),
            )
    except httpx.TimeoutException as exc:
        log.warning("ML detect/area timeout after %ss", settings.ML_TIMEOUT_S)
        raise MlUnavailable(
            "ML-сервис не успел ответить (таймаут инференса).",
            status_code=504,
        ) from exc
    except httpx.RequestError as exc:
        log.warning("ML detect/area unreachable: %s", exc)
        raise MlUnavailable(f"ML-сервис недоступен: {exc}") from exc

    if response.status_code == 401:
        raise MlUnavailable("Неверный ML API-ключ.", status_code=502)
    if response.status_code == 422:
        detail = _extract_detail(response)
        raise MlUnavailable(detail or "Некорректный участок для ML.", status_code=422)
    if response.status_code >= 400:
        detail = _extract_detail(response)
        raise MlUnavailable(
            detail or f"ML ответил {response.status_code}",
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    try:
        return response.json()
    except ValueError as exc:
        raise MlUnavailable("ML detect/area вернул не-JSON", status_code=502) from exc


def _extract_detail(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:500] if text else None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            return "; ".join(str(item) for item in detail)[:500]
    return None
