"""Append-only журнал модерации.

Пишется в той же транзакции, что и вердикт: если статус не сохранился,
в журнале не появится «отклонено». Обновить или удалить запись нельзя —
это и есть смысл журнала.
"""

from __future__ import annotations

import uuid

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModerationLog


class ModerationLogImmutable(RuntimeError):
    """Попытка изменить или удалить строку журнала."""


@event.listens_for(ModerationLog, "before_update")
def _forbid_update(mapper, connection, target) -> None:  # noqa: ARG001
    raise ModerationLogImmutable("moderation_log is append-only")


@event.listens_for(ModerationLog, "before_delete")
def _forbid_delete(mapper, connection, target) -> None:  # noqa: ARG001
    raise ModerationLogImmutable("moderation_log is append-only")


def log_moderation(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    entity_id: uuid.UUID,
    action: str,
    reason: str | None = None,
) -> None:
    """Добавить строку. Других операций у журнала нет."""
    session.add(
        ModerationLog(
            actor_id=actor_id,
            entity_id=entity_id,
            action=action,
            reason=reason,
        )
    )
