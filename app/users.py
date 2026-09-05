"""Удаление аккаунта — 152-ФЗ.

Точки и аналитика остаются: FK на users у гипотез, участников мероприятий
и analytics_events — ON DELETE SET NULL. Профиль, сессии и уведомления
уходят вместе с пользователем (CASCADE).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import refresh as refresh_tokens
from app.auth import get_current_user
from app.database import get_session
from app.models import User

router = APIRouter(prefix="/users", tags=["users"])


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить свой аккаунт и обезличить связанные данные",
)
async def delete_me(
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Удаляет строку users. Остальное делает база:

    * hypotheses.author_id → NULL — точка на карте остаётся;
    * analytics_events.user_id → NULL — KPI не ломается;
    * event_participants.user_id → NULL — явка мероприятия остаётся;
    * volunteers / staff / refresh_tokens / notifications — CASCADE.
    """
    await session.delete(user)
    # Кука живёт в браузере и после удаления строки. Без очистки клиент
    # носил бы мёртвый refresh и ловил 401 на каждом /auth/refresh.
    refresh_tokens.clear_cookie(response)
