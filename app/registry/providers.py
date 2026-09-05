"""
Источники сведений об организации по ИНН.

Почему не DaData: она фримиум — нужен ключ, есть суточный лимит, и при его
исчерпании регистрация ООПТ встаёт. Для проекта, который разворачивают в 15
регионах и, возможно, на изолированном контуре, зависимость от чужого ключа —
это лишняя точка отказа.

Порядок предпочтения:

1. **ЕГРЮЛ ФНС** (`egrul.nalog.ru`) — первоисточник, без ключа и без лимита.
   Оговорка честная: это тот же endpoint, что использует поисковая форма на
   сайте, отдельного публичного контракта у него нет. Формат может измениться
   без предупреждения — поэтому парсер терпимый, а падение источника уводит
   организацию в ручную модерацию, а не роняет регистрацию.
2. **DaData** — только если ключ задан в конфиге. Не обязателен.
3. **Офлайн-выгрузка ЕГРЮЛ** — план на прод: ФНС публикует полный набор
   открытых данных, его можно загрузить в свой PostgreSQL и не ходить наружу
   вообще. Не реализовано; см. PLAN.md.

Адреса в ответе поиска ЕГРЮЛ нет — есть регион. Полный адрес требует заказа
выписки; для регистрации это избыточно, адрес запрашиваем у пользователя.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompanyInfo:
    """Нормализованные сведения об организации — одинаковые для всех источников."""

    inn: str
    name: str
    short_name: str | None = None
    ogrn: str | None = None
    kpp: str | None = None
    address: str | None = None
    region: str | None = None
    management: str | None = None
    registered_at: str | None = None
    entity_type: str | None = None  # ul / ip
    is_active: bool = True
    source: str = "unknown"


class RegistryUnavailable(RuntimeError):
    """Источник не ответил. Отличается от «не нашли»: не найдено — это ответ,
    а недоступность — повод для ручной модерации, а не для отказа."""


class CompanyProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    async def lookup(self, inn: str) -> CompanyInfo | None: ...


class EgrulNalogProvider:
    """ЕГРЮЛ ФНС. Без ключа.

    Двухшаговый протокол: POST с запросом отдаёт токен, GET по токену — строки.
    """

    name = "egrul.nalog.ru"

    BASE = "https://egrul.nalog.ru"
    HEADERS = {
        "User-Agent": "chistyi-bereg/1.0 (+https://xn--80aihfaa6bgjbrt2e.xn--p1ai)",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    def is_configured(self) -> bool:
        return True

    async def lookup(self, inn: str) -> CompanyInfo | None:
        timeout = httpx.Timeout(settings.REGISTRY_TIMEOUT_SECONDS)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, headers=self.HEADERS, follow_redirects=True
            ) as client:
                token_response = await client.post(
                    f"{self.BASE}/", data={"query": inn, "region": ""}
                )
                token_response.raise_for_status()
                token = token_response.json().get("t")
                if not token:
                    raise RegistryUnavailable("ЕГРЮЛ не выдал токен поиска")

                result = await client.get(f"{self.BASE}/search-result/{token}")
                result.raise_for_status()
                rows = result.json().get("rows") or []
        except RegistryUnavailable:
            raise
        except Exception as error:  # сеть, таймаут, смена формата
            logger.warning("ЕГРЮЛ недоступен для ИНН=%s: %s", inn, error)
            raise RegistryUnavailable(str(error)) from error

        if not rows:
            return None

        row = rows[0]
        return CompanyInfo(
            inn=row.get("i") or inn,
            name=row.get("n") or row.get("c") or "",
            short_name=row.get("c"),
            ogrn=row.get("o"),
            kpp=row.get("p"),
            address=row.get("a"),
            region=row.get("rn"),
            management=row.get("g"),
            registered_at=row.get("r"),
            entity_type=row.get("k"),
            # В строке поиска ЕГРЮЛ признак ликвидации приходит в поле "e"
            # (дата прекращения). Есть дата — организация недействующая.
            is_active=not row.get("e"),
            source=self.name,
        )


class DadataProvider:
    """DaData. Подключается, только если задан ключ — иначе пропускается."""

    name = "dadata"

    URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"

    def is_configured(self) -> bool:
        return bool(settings.DADATA_API_KEY)

    async def lookup(self, inn: str) -> CompanyInfo | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Token {settings.DADATA_API_KEY}",
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.REGISTRY_TIMEOUT_SECONDS)
            ) as client:
                response = await client.post(
                    self.URL, json={"query": inn}, headers=headers
                )
                response.raise_for_status()
                suggestions = response.json().get("suggestions") or []
        except Exception as error:
            logger.warning("DaData недоступна для ИНН=%s: %s", inn, error)
            raise RegistryUnavailable(str(error)) from error

        if not suggestions:
            return None

        data = suggestions[0].get("data", {})
        management = data.get("management") or {}
        return CompanyInfo(
            inn=data.get("inn") or inn,
            name=(data.get("name") or {}).get("full_with_opf") or "",
            short_name=(data.get("name") or {}).get("short_with_opf"),
            ogrn=data.get("ogrn"),
            kpp=data.get("kpp"),
            address=(data.get("address") or {}).get("unrestricted_value"),
            region=((data.get("address") or {}).get("data") or {}).get("region_with_type"),
            management=(
                f"{management.get('post')}: {management.get('name')}"
                if management.get("name")
                else None
            ),
            registered_at=None,
            entity_type=data.get("type", "").lower() or None,
            is_active=data.get("state", {}).get("status") == "ACTIVE",
            source=self.name,
        )


def default_providers() -> list[CompanyProvider]:
    """Цепочка в порядке предпочтения; неподключённые отсеиваются."""
    chain: list[CompanyProvider] = [EgrulNalogProvider(), DadataProvider()]
    return [provider for provider in chain if provider.is_configured()]
