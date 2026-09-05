"""
Напоминания: «вы не начали курс», «курс не закончен», «нужно согласие».

Зачем это вообще есть. Между уходом на внешнюю платформу обучения и
возвращением мы человека не видим — в KPI-документе это названо «слепой зоной»
и признано главным риском воронки, особенно для четырнадцатилетних, у которых
16-часовой курс легко откладывается на «потом». Пассивно ждать нельзя.

Что здесь важнее самого текста писем:

* **Каждое напоминание — это событие.** `reminder_sent` при отправке и
  `reminder_clicked` при переходе по ссылке с `?nid=`. Без этой пары нельзя
  отличить того, кто вернулся сам, от того, кого вернуло напоминание, а
  значит нельзя ответить, работают ли напоминания вообще.
* **A/B зашит в механику, а не приделан сбоку.** Люди делятся на две ветки по
  хэшу идентификатора, ветки отличаются задержкой первого касания. Ветка
  пишется в payload, витрина `kpi.reminder_effectiveness` группирует по ней.
  Это превращает «мы считаем, что напоминания помогают» в проверяемое
  утверждение.
* **Отправка идемпотентна.** Ключ дедупликации не даёт послать одно и то же
  дважды, даже если планировщик отработал на двух репликах одновременно.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import EventType, emit
from app.models import (
    CertificateStatus,
    ConsentStatus,
    Notification,
    NotificationKind,
    User,
    Volunteer,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ветки A/B
# ---------------------------------------------------------------------------

#: Задержки первого напоминания о незаконченном курсе, дни.
#: Гипотеза: раннее касание ловит тех, кто ещё помнит о регистрации; позднее
#: не выглядит навязчивым. Какая ветка лучше — покажет витрина, а не спор.
VARIANTS: dict[str, tuple[int, ...]] = {
    "early": (3, 7, 14),
    "late": (7, 14, 21),
}


def variant_for(user_id: uuid.UUID) -> str:
    """Ветка эксперимента — устойчиво к перезапускам.

    По хэшу идентификатора, а не случайно: человек должен всегда попадать в ту
    же ветку, иначе замер бессмыслен. И не по чётности UUID — там нет гарантии
    равномерности.
    """
    digest = hashlib.sha256(user_id.bytes).digest()
    return "early" if digest[0] % 2 == 0 else "late"


# ---------------------------------------------------------------------------
# Правила
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Reminder:
    """Одно готовое к отправке напоминание."""

    user_id: uuid.UUID
    kind: NotificationKind
    stage: str
    title: str
    body: str
    action_url: str
    variant: str | None = None

    @property
    def dedupe_key(self) -> str:
        return f"{self.user_id}:{self.kind.value}:{self.stage}"


#: Через сколько дней после ухода на курс спросить, начал ли человек вообще.
COURSE_NOT_STARTED_DAYS = 1
#: Через сколько дней после регистрации напомнить о согласии представителя.
CONSENT_DAYS = 2


async def collect_due(session: AsyncSession, now: datetime | None = None) -> list[Reminder]:
    """Кому сегодня надо написать.

    Только чтение: разделено с отправкой намеренно, чтобы правила можно было
    проверить тестом и показать список на демонстрации, ничего не рассылая.
    """
    now = now or datetime.now(UTC)
    due: list[Reminder] = []

    rows = (
        await session.execute(
            select(Volunteer, User)
            .join(User, User.id == Volunteer.user_id)
            .where(Volunteer.certificate_status != CertificateStatus.approved)
        )
    ).unique().all()

    for volunteer, user in rows:
        due.extend(_course_reminders(volunteer, user, now))
        due.extend(_consent_reminders(volunteer, user, now))

    return due


def _course_reminders(
    volunteer: Volunteer, user: User, now: datetime
) -> list[Reminder]:
    # На курс ещё не уходили — напоминать про «допройди» нечего. Такой человек
    # застрял раньше, и это другая проблема (и другое письмо).
    if volunteer.course_redirect_at is None:
        return []
    # Сертификат уже на проверке: человек своё сделал, ждёт нас, а не мы его.
    if volunteer.certificate_status == CertificateStatus.pending:
        return []

    days_since = (now - volunteer.course_redirect_at).days
    variant = variant_for(user.id)
    reminders: list[Reminder] = []

    if days_since >= COURSE_NOT_STARTED_DAYS:
        reminders.append(
            Reminder(
                user_id=user.id,
                kind=NotificationKind.course_not_started,
                stage="day1",
                title="Как продвигается обучение?",
                body="Курс «Школы Защитников Природы» занимает 16 часов и его можно "
                     "проходить частями. Карта откроется сразу после проверки сертификата.",
                action_url="/api/v1/course/redirect",
                variant=variant,
            )
        )

    # Отклонённый сертификат — отдельный случай: человек дошёл до конца, но
    # документ не приняли, и текст должен звать исправить, а не «начать».
    rejected = volunteer.certificate_status == CertificateStatus.rejected
    for day in VARIANTS[variant]:
        if days_since >= day:
            reminders.append(
                Reminder(
                    user_id=user.id,
                    kind=NotificationKind.course_not_finished,
                    stage=f"day{day}",
                    title=(
                        "Сертификат нужно отправить заново"
                        if rejected
                        else "Остался шаг до карты"
                    ),
                    body=(
                        volunteer.certificate_reject_reason
                        or "Загрузите сертификат — и вы сможете отмечать загрязнения "
                           "на карте своей территории."
                    ),
                    action_url="/course",
                    variant=variant,
                )
            )

    return reminders


def _consent_reminders(
    volunteer: Volunteer, user: User, now: datetime
) -> list[Reminder]:
    if volunteer.consent_status != ConsentStatus.awaiting:
        return []
    if (now - user.created_at).days < CONSENT_DAYS:
        return []

    return [
        Reminder(
            user_id=user.id,
            kind=NotificationKind.consent_required,
            stage="day2",
            title="Нужно согласие законного представителя",
            body="Участникам до 18 лет для выхода на карту и участия в выездах нужно "
                 "согласие родителя или опекуна. Курс при этом доступен уже сейчас.",
            action_url="/consent",
        )
    ]


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

async def dispatch(session: AsyncSession, now: datetime | None = None) -> int:
    """Разослать всё, что назрело. Возвращает число фактически отправленных."""
    due = await collect_due(session, now)
    if not due:
        return 0

    # Один запрос вместо проверки по каждому: адресатов может быть много, а
    # уже отправленных среди них — почти все.
    keys = [reminder.dedupe_key for reminder in due]
    already = set(
        (
            await session.execute(
                select(Notification.dedupe_key).where(Notification.dedupe_key.in_(keys))
            )
        ).scalars()
    )

    sent = 0
    for reminder in due:
        if reminder.dedupe_key in already:
            continue

        try:
            # Savepoint, а не общий откат: конфликт по одному напоминанию не
            # должен отменять те, что уже вставлены в этом же прогоне.
            # `session.rollback()` откатил бы всю транзакцию целиком.
            async with session.begin_nested():
                session.add(
                    Notification(
                        user_id=reminder.user_id,
                        kind=reminder.kind,
                        title=reminder.title,
                        body=reminder.body,
                        action_url=reminder.action_url,
                        dedupe_key=reminder.dedupe_key,
                        payload={"stage": reminder.stage, "variant": reminder.variant},
                    )
                )
                # Событие внутри того же savepoint: если вставка не прошла,
                # `reminder_sent` не должен остаться — иначе KPI посчитает
                # отправку, которой не было.
                await emit(
                    session,
                    EventType.reminder_sent,
                    user_id=reminder.user_id,
                    payload={
                        "kind": reminder.kind.value,
                        "stage": reminder.stage,
                        "variant": reminder.variant,
                        # Канал фиксируем с самого начала: когда добавится
                        # почта, сравнение доходимости уже будет на чём считать.
                        "channel": "in_app",
                    },
                )
        except IntegrityError:
            # Уникальный индекс сработал: другая реплика успела раньше.
            # Штатный исход гонки, а не ошибка.
            logger.info("Напоминание %s уже отправлено другой репликой", reminder.dedupe_key)
            continue

        sent += 1

    return sent
