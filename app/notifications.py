"""
Уведомления: колокольчик и напоминания «допройди курс».

Само создание напоминаний по расписанию — отдельная задача (планировщик).
Здесь то, без чего планировщик бессмысленен: чтение, отметка о прочтении и
учёт клика, который замыкает KPI «эффективность напоминаний».
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_session
from app.models import Notification, User, UserRole
from app.reminders import collect_due, dispatch
from app.schemas import NotificationListOut, NotificationOut, ReminderDispatchOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get(
    "",
    response_model=NotificationListOut,
    summary="Мои уведомления",
)
async def list_notifications(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
):
    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))

    result = await session.execute(
        query.order_by(Notification.created_at.desc()).limit(limit)
    )
    items = result.scalars().all()

    # Счётчик считаем запросом, а не по выборке: с limit её длина ничего не
    # говорит о реальном числе непрочитанных.
    unread = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )

    return NotificationListOut(
        items=[NotificationOut.model_validate(n) for n in items],
        unread_count=unread or 0,
    )


@router.post(
    "/{notification_id}/read",
    response_model=NotificationOut,
    summary="Отметить прочитанным",
)
async def mark_read(
    notification_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    notification = await session.get(Notification, notification_id)
    # 404, а не 403: чужое уведомление для этого пользователя не существует —
    # отвечать «оно есть, но не ваше» значит подтверждать факт его наличия.
    if notification is None or notification.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found."
        )

    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
    await session.flush()

    return NotificationOut.model_validate(notification)


@router.post(
    "/read-all",
    response_model=NotificationListOut,
    summary="Отметить всё прочитанным",
)
async def mark_all_read(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Notification).where(
            Notification.user_id == user.id, Notification.read_at.is_(None)
        )
    )
    now = datetime.now(UTC)
    for notification in result.scalars().all():
        notification.read_at = now
    await session.flush()

    return NotificationListOut(items=[], unread_count=0)


@router.post(
    "/dispatch-reminders",
    response_model=ReminderDispatchOut,
    summary="Разослать назревшие напоминания прямо сейчас (координатор)",
)
async def dispatch_reminders_now(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    dry_run: bool = Query(
        default=False,
        description="Только показать, кому и что уйдёт, ничего не отправляя",
    ),
):
    """Ручной запуск рассылки.

    Планировщик и так тикает раз в час, но на демонстрации ждать час нельзя, а
    подкручивать часы на сервере — плохая идея. `dry_run` показывает список
    адресатов, не трогая ни базу, ни события.
    """
    if user.role != UserRole.coordinator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Рассылку запускает координатор программы.",
        )

    if dry_run:
        due = await collect_due(session)
        return ReminderDispatchOut(
            sent=0,
            due=len(due),
            preview=[f"{r.kind.value}/{r.stage} → {r.user_id}" for r in due[:20]],
        )

    sent = await dispatch(session)
    return ReminderDispatchOut(sent=sent, due=sent, preview=[])
