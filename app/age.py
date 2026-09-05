"""
Возраст и требование согласия законного представителя.

Отдельный модуль без зависимостей: нужен и в регистрации (`auth.py`), и в
модерации согласий (`consent.py`), а те друг друга уже импортируют — держать
эти функции в одном из них означало бы цикл.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.models import ConsentStatus, Volunteer

#: Нижняя граница самостоятельного участия в проекте.
MIN_AGE = 14
ADULT_AGE = 18


def age_at(birth_date: date, on: date | None = None) -> int:
    """Полных лет.

    Отдельной функцией, потому что «текущий год минус год рождения» ошибается
    на год у всех, кто ещё не отметил день рождения в этом году, — а у нас от
    этого зависит, нужен ли документ.
    """
    today = on or datetime.now(UTC).date()
    return (
        today.year
        - birth_date.year
        - ((today.month, today.day) < (birth_date.month, birth_date.day))
    )


def required_consent_status(birth_date: date | None) -> ConsentStatus:
    """Нужно ли согласие представителя при регистрации.

    Без даты рождения — «не требуется»: записи заводились без неё, и
    ретроспективно блокировать уже работающих волонтёров нельзя.
    """
    if birth_date is None:
        return ConsentStatus.not_required
    return (
        ConsentStatus.awaiting
        if age_at(birth_date) < ADULT_AGE
        else ConsentStatus.not_required
    )


def has_field_access(volunteer: Volunteer) -> bool:
    """Пускать ли на карту и в выезды.

    Согласие, полученное в 15 лет, действует и в 17 — статус остаётся
    approved. А тому, кому исполнилось 18 с незакрытым согласием, документ
    больше не нужен: требование снимается по возрасту само, без вмешательства
    координатора.
    """
    if volunteer.consent_status in (
        ConsentStatus.approved,
        ConsentStatus.not_required,
    ):
        return True
    return volunteer.birth_date is not None and age_at(volunteer.birth_date) >= ADULT_AGE
