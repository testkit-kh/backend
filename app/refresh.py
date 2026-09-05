"""
P1-7 — refresh-токены: выдача, ротация, отзыв.

Схема сессии: короткий access-токен в JSON (клиент держит его в памяти) и
долгий refresh-токен в httpOnly-куке. Разделение не косметическое:
access-токен — подписанный JWT, который сервер не проверяет по базе и потому
не может отозвать; отзыв висит на refresh-токене, который лежит в БД.

Ротация: каждый refresh обменивает токен на новый и отзывает предъявленный.
Оттуда же берётся обнаружение кражи — см. ``rotate``.

Здесь только работа с токенами и кукой; сами ручки ``/auth/refresh`` и
``/auth/logout`` — в ``app/auth.py``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import RefreshToken, User

logger = logging.getLogger(__name__)

#: 48 байт из CSPRNG — 64 символа base64url. Столько энтропии, что перебор
#: не рассматривается как угроза; это и позволяет хранить быстрый SHA-256
#: вместо bcrypt (подробнее — в докстринге модели RefreshToken).
_TOKEN_BYTES = 48

#: Сколько символов user-agent записываем. Строка нужна человеку, чтобы
#: узнать своё устройство в списке сессий, а не для точного разбора.
_UA_MAX_LEN = 512


def hash_token(raw_token: str) -> str:
    """SHA-256 в hex — единственный вид, в котором токен попадает в БД."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def issue(
    session: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
) -> tuple[str, RefreshToken]:
    """Выдать новый refresh-токен.

    Возвращает (сырой токен, запись). Сырой токен существует только здесь и
    в куке — в базу уходит хэш, и восстановить его оттуда нельзя.
    """
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    token = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=(user_agent or "").strip()[:_UA_MAX_LEN] or None,
    )
    session.add(token)
    await session.flush()
    return raw, token


async def find_by_raw(
    session: AsyncSession,
    raw_token: str,
    *,
    lock: bool = False,
) -> RefreshToken | None:
    """Найти запись по сырому токену из куки.

    Ищем по хэшу — индексный поиск по равенству. Именно поэтому хэш быстрый
    и без соли: по bcrypt-хэшу такой поиск невозможен.

    `lock` берёт строку под SELECT ... FOR UPDATE. Нужен при ротации: без
    блокировки два одновременных refresh с одной кукой оба увидели бы
    revoked_at = NULL и обменяли бы один токен на два.
    """
    query = select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token))
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)


async def revoke(session: AsyncSession, token: RefreshToken) -> None:
    """Отозвать один токен. Повторный отзыв ничего не меняет."""
    if token.revoked_at is None:
        token.revoked_at = datetime.now(UTC)
        await session.flush()


async def revoke_all_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    reason: str,
) -> int:
    """Отозвать все активные токены пользователя. Возвращает их число.

    Массовый отзыв — реакция на кражу: если refresh-токен утёк, мы не знаем,
    сколько ещё сессий успел завести злоумышленник, поэтому закрываем все и
    требуем войти паролем. Один UPDATE, а не выборка с обходом: между
    чтением и записью злоумышленник успел бы обновиться ещё раз.
    """
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    count = result.rowcount or 0
    logger.warning(
        "Отозваны все refresh-токены пользователя %s (%d шт.); причина: %s",
        user_id,
        count,
        reason,
    )
    return count


class RefreshError(Exception):
    """Refresh не удался.

    `theft_detected` отличает обычную неудачу (истёк, не найден) от прихода
    по уже отозванному токену: на это ручка отвечает другим кодом, чтобы
    клиент понял, что нужно не молча перелогиниться, а сказать человеку о
    входе с чужого устройства.
    """

    def __init__(self, detail: str, *, theft_detected: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.theft_detected = theft_detected


async def rotate(
    session: AsyncSession,
    raw_token: str,
    *,
    user_agent: str | None = None,
) -> tuple[str, User]:
    """Обменять предъявленный refresh-токен на новый.

    Возвращает (новый сырой токен, пользователь) либо бросает RefreshError.

    Обнаружение кражи. Токен одноразовый: успешная ротация сразу его
    отзывает. Поэтому приход по уже отозванному токену означает, что токен
    существует в двух местах — у владельца и у того, кто его скопировал. Кто
    из них пришёл вторым, выяснить нельзя, поэтому отзываем все сессии
    пользователя: владелец переживёт повторный вход паролем, а вор потеряет
    весь доступ.
    """
    token = await find_by_raw(session, raw_token, lock=True)
    if token is None:
        raise RefreshError("Refresh token is not valid.")

    if token.revoked_at is not None:
        await revoke_all_for_user(
            session,
            token.user_id,
            reason=f"повторное использование отозванного токена {token.id}",
        )
        raise RefreshError(
            "This token has already been used. All sessions have been revoked"
            " for security reasons — please sign in again.",
            theft_detected=True,
        )

    # Сравнение aware-времён: колонка timestamptz, asyncpg отдаёт с зоной.
    if token.expires_at <= datetime.now(UTC):
        await revoke(session, token)
        raise RefreshError("Refresh token has expired.")

    user = await session.get(User, token.user_id)
    if user is None:
        # Пользователя удалили, а кука осталась.
        await revoke(session, token)
        raise RefreshError("User not found.")

    # Ротация: отзываем старый, выдаём новый. Обе операции в одной
    # транзакции — при ошибке выдачи откатится всё, и старый токен не
    # останется живым рядом с новым.
    await revoke(session, token)
    raw_new, _ = await issue(session, user, user_agent=user_agent)
    return raw_new, user


# ---------------------------------------------------------------------------
# Кука
# ---------------------------------------------------------------------------


def set_cookie(response: Response, raw_token: str) -> None:
    """Положить refresh-токен в httpOnly-куку.

    httpOnly — чтобы XSS не смог её прочитать; в этом весь смысл держать
    refresh в куке, а не в localStorage. SameSite защищает от CSRF: кука не
    уйдёт с чужого сайта. max_age совпадает с TTL токена в БД, чтобы браузер
    не носил заведомо мёртвую куку.
    """
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=raw_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
        path=settings.REFRESH_COOKIE_PATH,
        domain=settings.REFRESH_COOKIE_DOMAIN or None,
    )


def clear_cookie(response: Response) -> None:
    """Удалить куку.

    path и domain обязаны совпадать с теми, что были при выдаче — иначе
    браузер сочтёт это другой кукой и оставит исходную на месте.
    """
    response.delete_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        path=settings.REFRESH_COOKIE_PATH,
        domain=settings.REFRESH_COOKIE_DOMAIN or None,
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
    )
