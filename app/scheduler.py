"""
Планировщик фоновых задач.

Живёт внутри процесса API, а не отдельным воркером: на объёмах пилота
поднимать Celery или arq ради одной задачи раз в час — это лишний контейнер,
лишний брокер и лишняя точка отказа. Когда задач станет больше, вынести их
будет несложно: `dispatch` из `app.reminders` ничего не знает о том, кто его
вызвал.

Единственная неочевидная часть — блокировка. API разворачивается несколькими
репликами, и планировщик стартует в каждой. Без взаимного исключения
напоминания рассылались бы по числу реплик. Уникальный ключ в БД от дублей
защитит, но лишняя работа и лишние конфликты никому не нужны, поэтому реплики
договариваются через advisory-lock самого PostgreSQL: он не требует ни
дополнительной таблицы, ни очистки — снимается вместе с соединением, даже если
реплику убили.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.config import settings
from app.database import async_session_factory
from app.reminders import dispatch

logger = logging.getLogger(__name__)

#: Произвольное, но постоянное число: имя блокировки в пространстве
#: advisory-локов PostgreSQL. Менять нельзя — иначе старые и новые реплики
#: перестанут видеть друг друга.
REMINDERS_LOCK_ID = 831_204_771

_scheduler: AsyncIOScheduler | None = None


async def dispatch_reminders_job() -> None:
    """Тик планировщика. Никогда не бросает исключений наружу.

    Упавшая джоба не должна ронять планировщик: следующий тик через час, и
    к нему проблема может пройти сама (например, если БД была недоступна).
    """
    try:
        async with async_session_factory() as session:
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": REMINDERS_LOCK_ID}
            )
            if not acquired:
                logger.debug("Рассылку напоминаний уже выполняет другая реплика")
                return

            try:
                sent = await dispatch(session)
                await session.commit()
                if sent:
                    logger.info("Отправлено напоминаний: %s", sent)
            finally:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": REMINDERS_LOCK_ID}
                )
                await session.commit()
    except Exception:
        logger.exception("Не удалось разослать напоминания")


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler

    if not settings.REMINDERS_ENABLED:
        logger.info("Планировщик напоминаний выключен (REMINDERS_ENABLED=false)")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        dispatch_reminders_job,
        trigger=IntervalTrigger(minutes=settings.REMINDERS_INTERVAL_MINUTES),
        id="dispatch_reminders",
        # Если процесс стоял, не наверстываем пропущенные тики: человек всё равно
        # получит письмо один раз, а всплеск отправок сразу после деплоя
        # выглядит как сбой.
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    _scheduler.start()
    logger.info(
        "Планировщик напоминаний запущен, интервал %s мин",
        settings.REMINDERS_INTERVAL_MINUTES,
    )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
