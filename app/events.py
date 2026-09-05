"""
P1-4 — мероприятия по уборке.

Мероприятие не создаётся здесь: оно появляется автоматически при одобрении
гипотезы (см. ``hypotheses.validate_hypothesis``). Этот роутер отвечает за
всё, что происходит с ним потом — планирование сотрудником ООПТ, запись
волонтёров и закрытие с итогами.

Все эндпоинты живут под ``/api/v1/events`` и требуют JWT.
Ролевой контроль — внутри каждого обработчика.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.age import has_field_access
from app.analytics import EventType, emit
from app.auth import get_current_user
from app.database import get_session
from app.hypotheses import _require_staff, _require_volunteer, notify_point_status_changed
from app.models import (
    CertificateStatus,
    Event,
    EventParticipant,
    EventStatus,
    Hypothesis,
    HypothesisStatus,
    Staff,
    User,
    UserRole,
)
from app.moderation import log_moderation
from app.schemas import (
    EventBeforeAfterOut,
    EventBeforeAfterRequest,
    EventCompleteRequest,
    EventCompleteResponse,
    EventJoinOut,
    EventListOut,
    EventOut,
    EventUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])


# ═══════════════════════════════════════════════════════════
# Хелперы
# ═══════════════════════════════════════════════════════════


def _participants_count_subq():
    """Число записавшихся одним скалярным подзапросом.

    Считаем в SQL, а не через relationship: на списке мероприятий обход
    participants дал бы запрос на каждую строку.
    """
    return (
        select(func.count(EventParticipant.id))
        .where(EventParticipant.event_id == Event.id)
        .correlate(Event)
        .scalar_subquery()
    )


async def _get_event_for_staff(
    session: AsyncSession,
    event_id: uuid.UUID,
    staff: Staff,
) -> Event:
    """Мероприятие своей ООПТ или 404/403.

    404 раньше 403: сначала «существует ли», потом «ваше ли». Обратный
    порядок невозможен — у несуществующего мероприятия нет владельца.
    """
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Мероприятие не найдено.",
        )
    if event.organization_id != staff.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Мероприятие относится к другой ООПТ — изменять его может только её сотрудник."
            ),
        )
    return event


async def _to_event_out(
    session: AsyncSession,
    event: Event,
    *,
    is_joined: bool = False,
) -> EventOut:
    """Собрать EventOut для одного мероприятия."""
    count = await session.scalar(
        select(func.count(EventParticipant.id)).where(EventParticipant.event_id == event.id)
    )
    out = EventOut.model_validate(event)
    out.participants_count = count or 0
    out.is_joined = is_joined
    return out


# ═══════════════════════════════════════════════════════════
# 1. GET /api/v1/events
# ═══════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=EventListOut,
    summary="Список мероприятий (своя ООПТ для сотрудника, planned для волонтёра)",
)
async def list_events(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    status_filter: EventStatus | None = Query(
        default=None,
        alias="status",
        description="Фильтр по статусу; для волонтёра игнорируется",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Одна ручка на две роли, но с разной выборкой.

    Сотрудник ведёт свои мероприятия — видит все статусы своей ООПТ.
    Волонтёр выбирает, куда поехать — видит только запланированные и по
    всем территориям: человек из Петропавловска может поехать и в
    Кроноцкий, и в Южно-Камчатский.
    """
    count_subq = _participants_count_subq().label("participants_count")

    if user.role == UserRole.staff:
        staff = _require_staff(user)
        query = select(Event, count_subq, literal(False).label("is_joined")).where(
            Event.organization_id == staff.organization_id
        )
        total_query = (
            select(func.count())
            .select_from(Event)
            .where(Event.organization_id == staff.organization_id)
        )
        if status_filter is not None:
            query = query.where(Event.status == status_filter)
            total_query = total_query.where(Event.status == status_filter)
    else:
        # Волонтёр. _require_volunteer здесь и играет роль guard'а: у
        # координатора нет ни своей ООПТ, ни повода записываться на уборку.
        _require_volunteer(user)
        joined_subq = (
            select(EventParticipant.id)
            .where(
                EventParticipant.event_id == Event.id,
                EventParticipant.user_id == user.id,
            )
            .exists()
            .label("is_joined")
        )
        query = select(Event, count_subq, joined_subq).where(Event.status == EventStatus.planned)
        total_query = (
            select(func.count()).select_from(Event).where(Event.status == EventStatus.planned)
        )

    # Мероприятия без даты — в конец: у них дата ещё не назначена, и в
    # ленте волонтёра они менее полезны, чем те, куда можно записаться.
    result = await session.execute(
        query.order_by(
            Event.scheduled_at.is_(None),
            Event.scheduled_at.asc(),
            Event.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )

    items: list[EventOut] = []
    for event, participants_count, is_joined in result.all():
        out = EventOut.model_validate(event)
        out.participants_count = participants_count or 0
        out.is_joined = bool(is_joined)
        items.append(out)

    total = await session.scalar(total_query)

    return EventListOut(
        items=items,
        total=total or 0,
        limit=limit,
        offset=offset,
    )


# ═══════════════════════════════════════════════════════════
# 2. POST /api/v1/events/{id}/join
# ═══════════════════════════════════════════════════════════


@router.post(
    "/{event_id}/join",
    response_model=EventJoinOut,
    status_code=status.HTTP_201_CREATED,
    summary="Записаться на мероприятие (волонтёр, идемпотентно)",
)
async def join_event(
    event_id: uuid.UUID,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    vol = _require_volunteer(user)

    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Мероприятие не найдено.",
        )

    # ---- Проверка допуска к полевой работе ----
    # Те же правила, что и для создания точки: выезд на уборку — работа в
    # поле, и пускать туда несовершеннолетнего без согласия представителя
    # или человека без обучения нельзя.
    if not has_field_access(vol):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Нужно согласие законного представителя:"
                " участникам до 18 лет выезды открываются"
                " после его подтверждения."
            ),
        )
    if vol.certificate_status != CertificateStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Запись на уборку открывается после проверки"
                " сертификата о прохождении курса."
                f" Текущий статус: {vol.certificate_status.value}."
            ),
        )

    # Записываться можно только в запланированное. На завершённое или
    # отменённое — бессмысленно, и это не идемпотентный повтор, а ошибка.
    if event.status != EventStatus.planned:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Записаться можно только на запланированное"
                f" мероприятие. Текущий статус: {event.status.value}."
            ),
        )

    # ---- Идемпотентность ----
    existing = await session.scalar(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user.id,
        )
    )
    if existing is not None:
        # 200, а не 201 и не 409: повторное нажатие «Пойду» на слабой связи —
        # норма, а не ошибка клиента.
        response.status_code = status.HTTP_200_OK
        out = EventJoinOut.model_validate(existing)
        out.already_joined = True
        return out

    participant = EventParticipant(
        event_id=event_id,
        user_id=user.id,
    )
    session.add(participant)
    await session.flush()

    await emit(
        session,
        EventType.cleanup_event_joined,
        user_id=user.id,
        payload={
            "event_id": str(event_id),
            "hypothesis_id": str(event.hypothesis_id),
            "organization_id": str(event.organization_id),
            "scheduled_at": (event.scheduled_at.isoformat() if event.scheduled_at else None),
        },
    )

    return EventJoinOut.model_validate(participant)


# ═══════════════════════════════════════════════════════════
# 3. DELETE /api/v1/events/{id}/join
# ═══════════════════════════════════════════════════════════


@router.delete(
    "/{event_id}/join",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отменить запись на мероприятие (волонтёр)",
)
async def leave_event(
    event_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Отмена записи.

    Идемпотентна так же, как и запись: 204 и в случае, когда записи не
    было. Клиент хочет получить состояние «я не участвую», и оно
    достигнуто.
    """
    _require_volunteer(user)

    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Мероприятие не найдено.",
        )

    await session.execute(
        delete(EventParticipant).where(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user.id,
        )
    )


# ═══════════════════════════════════════════════════════════
# 4. PATCH /api/v1/events/{id}
# ═══════════════════════════════════════════════════════════


@router.patch(
    "/{event_id}",
    response_model=EventOut,
    summary="Обновить дату, место и описание мероприятия (сотрудник ООПТ)",
)
async def update_event(
    event_id: uuid.UUID,
    body: EventUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    staff = _require_staff(user)
    event = await _get_event_for_staff(session, event_id, staff)

    # Закрытое мероприятие — уже история. Переписать дату прошедшего
    # выезда значит переписать отчётность по нему.
    if event.status == EventStatus.completed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Завершённое мероприятие изменить нельзя.",
        )

    # Только явно переданные поля: PATCH с одним place не должен обнулить
    # уже назначенную дату.
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(event, field, value)
    await session.flush()

    return await _to_event_out(session, event)


# ═══════════════════════════════════════════════════════════
# 5. POST /api/v1/events/{id}/complete
# ═══════════════════════════════════════════════════════════


@router.post(
    "/{event_id}/complete",
    response_model=EventCompleteResponse,
    summary="Закрыть мероприятие с итогами уборки (сотрудник ООПТ)",
)
async def complete_event(
    event_id: uuid.UUID,
    body: EventCompleteRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Закрытие мероприятия — момент, когда точка становится убранной.

    Гипотеза переводится в ``cleaned`` здесь, а не отдельным вызовом: два
    запроса означали бы состояние «мероприятие закрыто, а точка на карте
    всё ещё грязная», и рано или поздно система в нём бы и застряла.
    """
    staff = _require_staff(user)
    event = await _get_event_for_staff(session, event_id, staff)

    # Повторное закрытие — не идемпотентный повтор, а перезапись итогов:
    # объём мусора вторым вызовом молча заменился бы. Пусть клиент явно
    # разбирается.
    if event.status == EventStatus.completed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Мероприятие уже завершено.",
        )
    if event.status == EventStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Отменённое мероприятие нельзя завершить.",
        )

    now = datetime.now(UTC)
    event.status = EventStatus.completed
    event.completed_at = body.completed_at or now
    event.actual_participants = body.actual_participants
    event.waste_volume_m3 = body.waste_volume_m3
    event.waste_mass_kg = body.waste_mass_kg
    if body.result_notes is not None:
        event.result_notes = body.result_notes

    # ---- Отметка явки ----
    attendance_marked = 0
    if body.attended_user_ids:
        wanted = set(body.attended_user_ids)
        result = await session.execute(
            select(EventParticipant).where(
                EventParticipant.event_id == event.id,
                EventParticipant.user_id.in_(wanted),
            )
        )
        found = result.scalars().all()
        # Отмечать явку тому, кто не записывался, нельзя: участие
        # оформляется записью, иначе в данных появятся участники без
        # согласия и без обучения.
        missing = wanted - {p.user_id for p in found}
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Эти пользователи не записаны на мероприятие: "
                    + ", ".join(str(m) for m in sorted(missing, key=str))
                ),
            )
        for participant in found:
            participant.attended = True
        attendance_marked = len(found)

    # ---- Гипотеза → cleaned ----
    # Явный select по hypothesis_id, а не event.hypothesis: связь ленивая,
    # и обращение к ней в async-сессии упало бы в MissingGreenlet.
    hypothesis = await session.scalar(
        select(Hypothesis).where(Hypothesis.id == event.hypothesis_id)
    )
    if hypothesis is None:
        # FK с ON DELETE CASCADE делает это невозможным, но если целостность
        # всё же нарушена — падать нужно здесь, а не отдавать «убрано» по
        # точке, которой нет.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Гипотеза, привязанная к мероприятию, не найдена.",
        )
    previous_status = hypothesis.status
    hypothesis.status = HypothesisStatus.cleaned

    await session.flush()

    if previous_status != HypothesisStatus.cleaned:
        notify_point_status_changed(
            session,
            author_id=hypothesis.author_id,
            hypothesis_id=hypothesis.id,
            new_status=HypothesisStatus.cleaned,
        )

    log_moderation(
        session,
        actor_id=user.id,
        entity_id=event.id,
        action="completed",
        reason=body.result_notes,
    )

    registered = await session.scalar(
        select(func.count(EventParticipant.id)).where(EventParticipant.event_id == event.id)
    )

    await emit(
        session,
        EventType.cleanup_event_completed,
        user_id=user.id,
        lat=hypothesis.lat,
        lon=hypothesis.lon,
        payload={
            "event_id": str(event.id),
            "hypothesis_id": str(hypothesis.id),
            "organization_id": str(event.organization_id),
            "author_id": str(hypothesis.author_id),
            "actual_participants": body.actual_participants,
            "registered_participants": registered or 0,
            "waste_volume_m3": body.waste_volume_m3,
            "waste_mass_kg": body.waste_mass_kg,
            # Разница «оценили на глаз» и «вывезли на самом деле» — то, чем
            # калибруются будущие сметы.
            "estimated_volume_m3": hypothesis.computed_volume_m3,
            "estimated_mass_kg": hypothesis.computed_mass_kg,
            "days_from_report": (
                (event.completed_at - hypothesis.created_at).days if event.completed_at else None
            ),
        },
    )

    out = EventOut.model_validate(event)
    out.participants_count = registered or 0

    return EventCompleteResponse(
        event=out,
        hypothesis_id=hypothesis.id,
        hypothesis_status=hypothesis.status,
        attendance_marked=attendance_marked,
    )


# ═══════════════════════════════════════════════════════════
# 6. POST /api/v1/events/{id}/before-after
# ═══════════════════════════════════════════════════════════


@router.post(
    "/{event_id}/before-after",
    response_model=EventBeforeAfterOut,
    status_code=status.HTTP_201_CREATED,
    summary="Принять фото «до/после» уборки (сотрудник ООПТ)",
)
async def accept_before_after(
    event_id: uuid.UUID,
    body: EventBeforeAfterRequest,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Приёмка доказательств уборки.

    Отдельная ручка, а не поле в complete: цифры «сколько вывезли» и фото
    «было/стало» приходят в разное время. Повтор не переписывает уже
    принятую пару — иначе отчётность по доказательствам плыла бы.
    """
    staff = _require_staff(user)
    event = await _get_event_for_staff(session, event_id, staff)

    if event.status == EventStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Отменённое мероприятие нельзя принимать.",
        )
    if event.status != EventStatus.completed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Приёмка «до/после» возможна после закрытия мероприятия.",
        )

    if event.before_after_accepted_at is not None:
        response.status_code = status.HTTP_200_OK
        return EventBeforeAfterOut(
            event=await _to_event_out(session, event),
            already_accepted=True,
        )

    before = list(body.photo_before_urls)
    if not before:
        hypothesis = await session.scalar(
            select(Hypothesis).where(Hypothesis.id == event.hypothesis_id)
        )
        if hypothesis is not None and hypothesis.photo_url:
            before = [hypothesis.photo_url]
    if not before:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Нужна хотя бы одна фотография «до»: приложите снимок "
                "или у точки должен быть photo_url."
            ),
        )

    now = datetime.now(UTC)
    event.photo_before_urls = before
    event.photo_after_urls = list(body.photo_after_urls)
    event.before_after_accepted_at = now
    await session.flush()

    log_moderation(
        session,
        actor_id=user.id,
        entity_id=event.id,
        action="before_after_accepted",
    )

    await emit(
        session,
        EventType.cleanup_event_before_after,
        user_id=user.id,
        payload={
            "event_id": str(event.id),
            "hypothesis_id": str(event.hypothesis_id),
            "organization_id": str(event.organization_id),
            "before_count": len(before),
            "after_count": len(event.photo_after_urls or []),
        },
    )

    return EventBeforeAfterOut(event=await _to_event_out(session, event))
