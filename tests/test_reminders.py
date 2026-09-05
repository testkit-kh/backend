"""Правила напоминаний.

Тесты без базы: правила — чистые функции над двумя объектами, и проверять их
через БД значит проверять SQLAlchemy, а не собственную логику.
"""

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    CertificateStatus,
    ConsentStatus,
    NotificationKind,
    User,
    UserRole,
    Volunteer,
)
from app.reminders import (
    VARIANTS,
    Reminder,
    _consent_reminders,
    _course_reminders,
    variant_for,
)

NOW = datetime(2026, 9, 5, tzinfo=UTC)


def make_user(user_id: uuid.UUID | None = None, days_ago: int = 30) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        email="v@example.ru",
        full_name="Волонтёр В.",
        password_hash="x",
        role=UserRole.volunteer,
        created_at=NOW - timedelta(days=days_ago),
    )


def make_volunteer(**kwargs) -> Volunteer:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_over_14=True,
        is_trained=False,
        certificate_status=CertificateStatus.none,
        consent_status=ConsentStatus.not_required,
        course_redirect_at=None,
    )
    return Volunteer(**{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# Ветки эксперимента
# ---------------------------------------------------------------------------


def test_variant_is_stable_for_the_same_user():
    """Человек обязан всегда попадать в одну ветку, иначе замер бессмыслен."""
    user_id = uuid.uuid4()
    assert variant_for(user_id) == variant_for(user_id)


def test_variants_split_roughly_evenly():
    """Перекошенный сплит обесценил бы сравнение веток."""
    counts = Counter(variant_for(uuid.uuid4()) for _ in range(2000))

    assert set(counts) == set(VARIANTS)
    assert 0.4 < counts["early"] / 2000 < 0.6


# ---------------------------------------------------------------------------
# Курс
# ---------------------------------------------------------------------------


def test_no_course_reminders_before_leaving_to_the_course():
    """Кто не уходил на курс, застрял раньше — это другая проблема."""
    volunteer = make_volunteer(course_redirect_at=None)

    assert _course_reminders(volunteer, make_user(), NOW) == []


def test_certificate_pending_is_left_alone():
    """Человек своё сделал и ждёт нас — торопить его нечестно."""
    volunteer = make_volunteer(
        course_redirect_at=NOW - timedelta(days=30),
        certificate_status=CertificateStatus.pending,
    )

    assert _course_reminders(volunteer, make_user(), NOW) == []


def test_first_nudge_after_a_day():
    volunteer = make_volunteer(course_redirect_at=NOW - timedelta(days=1))

    kinds = {r.kind for r in _course_reminders(volunteer, make_user(), NOW)}

    assert NotificationKind.course_not_started in kinds


def test_nothing_on_the_same_day():
    volunteer = make_volunteer(course_redirect_at=NOW - timedelta(hours=3))

    assert _course_reminders(volunteer, make_user(), NOW) == []


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_stages_follow_the_variant_schedule(variant):
    """Ветка задаёт расписание касаний, а не только метку в payload."""
    user = next(
        u for u in (make_user(uuid.uuid4()) for _ in range(200)) if variant_for(u.id) == variant
    )
    days = VARIANTS[variant]
    volunteer = make_volunteer(course_redirect_at=NOW - timedelta(days=days[0]))

    stages = {
        r.stage
        for r in _course_reminders(volunteer, user, NOW)
        if r.kind == NotificationKind.course_not_finished
    }

    assert stages == {f"day{days[0]}"}


def test_rejected_certificate_gets_its_own_wording():
    """«Начните курс» человеку, который его прошёл, — это оскорбительно."""
    volunteer = make_volunteer(
        course_redirect_at=NOW - timedelta(days=30),
        certificate_status=CertificateStatus.rejected,
        certificate_reject_reason="На скане не видно ФИО",
    )

    unfinished = [
        r
        for r in _course_reminders(volunteer, make_user(), NOW)
        if r.kind == NotificationKind.course_not_finished
    ]

    assert unfinished
    assert all("заново" in r.title for r in unfinished)
    assert all(r.body == "На скане не видно ФИО" for r in unfinished)


# ---------------------------------------------------------------------------
# Согласие
# ---------------------------------------------------------------------------


def test_consent_reminder_only_when_awaiting():
    for status in (ConsentStatus.not_required, ConsentStatus.approved, ConsentStatus.rejected):
        volunteer = make_volunteer(consent_status=status)
        assert _consent_reminders(volunteer, make_user(days_ago=10), NOW) == []


def test_consent_reminder_waits_a_couple_of_days():
    """Сразу после регистрации родитель ещё физически не успел подписать."""
    volunteer = make_volunteer(consent_status=ConsentStatus.awaiting)

    assert _consent_reminders(volunteer, make_user(days_ago=0), NOW) == []
    assert _consent_reminders(volunteer, make_user(days_ago=3), NOW) != []


def test_consent_reminder_says_the_course_is_still_open():
    """Смысл текста: документ блокирует карту, но не обучение."""
    volunteer = make_volunteer(consent_status=ConsentStatus.awaiting)

    reminder = _consent_reminders(volunteer, make_user(days_ago=3), NOW)[0]

    assert "Курс при этом доступен" in reminder.body


# ---------------------------------------------------------------------------
# Дедупликация
# ---------------------------------------------------------------------------


def test_dedupe_key_separates_stages_and_kinds():
    user_id = uuid.uuid4()
    base = dict(user_id=user_id, title="t", body="b", action_url="/")

    day3 = Reminder(kind=NotificationKind.course_not_finished, stage="day3", **base)
    day7 = Reminder(kind=NotificationKind.course_not_finished, stage="day7", **base)
    other = Reminder(kind=NotificationKind.course_not_started, stage="day3", **base)

    assert len({day3.dedupe_key, day7.dedupe_key, other.dedupe_key}) == 3


def test_dedupe_key_is_stable():
    reminder = Reminder(
        user_id=uuid.uuid4(),
        kind=NotificationKind.consent_required,
        stage="day2",
        title="t",
        body="b",
        action_url="/",
    )

    assert reminder.dedupe_key == reminder.dedupe_key
