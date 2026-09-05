"""
Поиск организации по ИНН: кэш в своей БД + цепочка источников.

Кэш здесь не про скорость, а про устойчивость. ЕГРЮЛ — не наш сервис: он
может лечь, поменять формат или начать душить запросы. Один раз получив
сведения, мы больше не зависим от его доступности при повторных обращениях,
а при регистрации новой ООПТ падение источника уводит заявку в ручную
модерацию вместо отказа пользователю.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CompanyRegistryCache
from app.registry.checksum import is_valid_inn, normalize_inn
from app.registry.providers import (
    CompanyInfo,
    CompanyProvider,
    RegistryUnavailable,
    default_providers,
)

logger = logging.getLogger(__name__)


class InvalidInn(ValueError):
    """Контрольная сумма не сходится — во внешний реестр идти незачем."""


async def lookup_company(
    session: AsyncSession,
    raw_inn: str,
    *,
    providers: list[CompanyProvider] | None = None,
    use_cache: bool = True,
) -> CompanyInfo | None:
    """Сведения об организации.

    Возвращает None, если ИНН корректен, но организации нет.
    Бросает InvalidInn при неверной контрольной сумме и RegistryUnavailable,
    если ни один источник не ответил.
    """
    inn = normalize_inn(raw_inn)
    if not is_valid_inn(inn):
        raise InvalidInn(f"Контрольная сумма ИНН {inn or '<пусто>'} не сходится")

    if use_cache:
        cached = await _read_cache(session, inn)
        if cached is not None:
            return cached

    chain = providers if providers is not None else default_providers()
    if not chain:
        raise RegistryUnavailable("Не настроен ни один источник сведений об организациях")

    failures: list[str] = []
    for provider in chain:
        try:
            info = await provider.lookup(inn)
        except RegistryUnavailable as error:
            failures.append(f"{provider.name}: {error}")
            continue

        # Организации нет — это ответ, а не сбой: следующий источник
        # спрашивать бессмысленно, ЕГРЮЛ первоисточник.
        if info is None:
            await _write_cache(session, inn, None, provider.name)
            return None

        await _write_cache(session, inn, info, provider.name)
        return info

    raise RegistryUnavailable("; ".join(failures))


async def _read_cache(session: AsyncSession, inn: str) -> CompanyInfo | None:
    row = await session.scalar(select(CompanyRegistryCache).where(CompanyRegistryCache.inn == inn))
    if row is None:
        return None

    age = datetime.now(UTC) - row.fetched_at
    if age > timedelta(days=settings.REGISTRY_CACHE_TTL_DAYS):
        return None

    # Отрицательный результат тоже кэшируется: перебор несуществующих ИНН
    # иначе превращается в поток запросов к ЕГРЮЛ от нашего имени.
    if not row.payload:
        return None

    return CompanyInfo(**row.payload)


async def _write_cache(
    session: AsyncSession, inn: str, info: CompanyInfo | None, source: str
) -> None:
    row = await session.scalar(select(CompanyRegistryCache).where(CompanyRegistryCache.inn == inn))
    payload = asdict(info) if info is not None else {}

    if row is None:
        session.add(
            CompanyRegistryCache(
                inn=inn, payload=payload, source=source, fetched_at=datetime.now(UTC)
            )
        )
    else:
        row.payload = payload
        row.source = source
        row.fetched_at = datetime.now(UTC)
