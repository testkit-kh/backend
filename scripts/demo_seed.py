#!/usr/bin/env python
"""
Демо-данные для KPI-дашбордов Metabase: организации, волонтёры и события
аналитики, из которых считаются все 11 витрин в схеме `kpi` (см. миграцию
0006_kpi_views.py).

Пишет только organizations / users / analytics_events. Ни один из
kpi.*-вью не читает hypotheses, volunteers или staff напрямую — вся
воронка, операционка и экология считаются из событий и таблицы
организаций, поэтому «тяжёлые» таблицы (с геометрией, уникальными
ограничениями и т.п.) этот скрипт не трогает.

Идемпотентен: если демо-пользователи (домен @demo.testkit.invalid) уже
есть — ничего не делает, пока не передан --reset.

Запуск (внутри контейнера backend, где уже настроен DATABASE_URL):
    python scripts/demo_seed.py [--reset]
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from random import Random

from sqlalchemy import delete, select

from app.analytics.events import EventType
from app.auth import hash_password
from app.database import async_session_factory
from app.models import AnalyticsEvent, Organization, OrgVerificationStatus, Staff, User, UserRole

DEMO_EMAIL_DOMAIN = "demo.testkit.invalid"
RNG = Random(42)
NOW = datetime.now(UTC)
WEEKS = 10
N_VOLUNTEERS = 140

ORGS = [
    {"name": "Кроноцкий государственный заповедник", "inn": "4101099991"},
    {"name": "Командорский государственный заповедник", "inn": "4101088882"},
    {"name": "Национальный парк «Земля Франца-Иосифа»", "inn": "8901077773"},
    {"name": "Куршская коса, национальный парк", "inn": "3906066664"},
]

SOURCES = ["direct", "vk", "instagram", "telegram", "school"]
SOURCE_WEIGHTS = [35, 25, 20, 15, 5]
TRASH_CATEGORIES = [
    "plastic", "fishing_gear", "glass", "metal", "wood",
    "rubber", "hazardous", "household", "construction", "other",
]
FRACTIONS = ["mega", "macro", "meso", "micro"]
ACCESS_TYPES = ["on_foot", "vehicle", "boat", "helicopter"]
ACCESS_WEIGHTS = [45, 35, 15, 5]


async def already_seeded(session) -> bool:
    result = await session.execute(
        select(User.id).where(User.email.like(f"%@{DEMO_EMAIL_DOMAIN}")).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def reset(session) -> None:
    demo_user_ids = (
        (await session.execute(select(User.id).where(User.email.like(f"%@{DEMO_EMAIL_DOMAIN}"))))
        .scalars()
        .all()
    )
    if demo_user_ids:
        await session.execute(
            delete(AnalyticsEvent).where(AnalyticsEvent.user_id.in_(demo_user_ids))
        )
        await session.execute(delete(User).where(User.id.in_(demo_user_ids)))
    demo_inns = [o["inn"] for o in ORGS]
    await session.execute(delete(Organization).where(Organization.inn.in_(demo_inns)))
    await session.commit()


async def seed() -> dict[str, int]:
    async with async_session_factory() as session:
        orgs: list[Organization] = []
        for spec in ORGS:
            org = Organization(
                name=spec["name"],
                inn=spec["inn"],
                verification_status=OrgVerificationStatus.verified,
                contact_email=f"office@{spec['inn']}.{DEMO_EMAIL_DOMAIN}",
            )
            session.add(org)
            orgs.append(org)
        await session.flush()

        shared_password = hash_password("demo-not-a-real-password")

        staff_by_org: dict[uuid.UUID, User] = {}
        for org in orgs:
            staff_user = User(
                email=f"staff+{org.inn}@{DEMO_EMAIL_DOMAIN}",
                full_name=f"Инспектор, {org.name[:40]}",
                password_hash=shared_password,
                role=UserRole.staff,
            )
            session.add(staff_user)
            await session.flush()
            # Обязателен: /auth/me падает с 500 ("Staff profile missing for
            # user"), если у роли staff нет связанной записи Staff.
            session.add(Staff(user_id=staff_user.id, organization_id=org.id))
            staff_by_org[org.id] = staff_user
        await session.flush()

        events: list[AnalyticsEvent] = []

        def add_event(event_type: EventType, user_id, created_at, payload) -> None:
            events.append(
                AnalyticsEvent(
                    user_id=user_id,
                    event_type=event_type.value,
                    payload=payload,
                    created_at=created_at,
                )
            )

        certified_active: list[tuple[uuid.UUID, datetime]] = []

        for i in range(N_VOLUNTEERS):
            registered_at = NOW - timedelta(
                weeks=RNG.uniform(0, WEEKS), days=RNG.uniform(0, 6), hours=RNG.uniform(0, 23)
            )
            source = RNG.choices(SOURCES, weights=SOURCE_WEIGHTS)[0]
            user = User(
                email=f"volunteer{i}@{DEMO_EMAIL_DOMAIN}",
                full_name=f"Волонтёр {i}",
                password_hash=shared_password,
                role=UserRole.volunteer,
                created_at=registered_at,
            )
            session.add(user)
            await session.flush()

            add_event(
                EventType.user_registered, user.id, registered_at,
                {"role": "volunteer", "source": source, "referred_by": None},
            )

            if RNG.random() > 0.70:
                continue  # так и не дошёл до курса

            redirect_at = registered_at + timedelta(hours=RNG.uniform(0.2, 72))
            add_event(
                EventType.course_redirect_click, user.id, redirect_at,
                {"first_time": True, "from_notification": None},
            )

            returned = RNG.random() < 0.55
            if not returned and RNG.random() < 0.6:
                sent_at = redirect_at + timedelta(days=RNG.uniform(3, 10))
                variant = RNG.choice(["control", "warm"])
                add_event(
                    EventType.reminder_sent,
                    user.id,
                    sent_at,
                    {
                        "kind": "course_not_finished",
                        "stage": 1,
                        "variant": variant,
                        "channel": "email",
                    },
                )
                if RNG.random() < (0.45 if variant == "warm" else 0.25):
                    click_at = sent_at + timedelta(hours=RNG.uniform(0.5, 30))
                    add_event(
                        EventType.reminder_clicked, user.id, click_at,
                        {"notification_id": str(uuid.uuid4())},
                    )
                    return_at = click_at + timedelta(hours=RNG.uniform(0.1, 5))
                    add_event(
                        EventType.app_reopened_post_redirect, user.id, return_at,
                        {"days_since_redirect": (return_at - redirect_at).days},
                    )
                    returned = True
            elif returned:
                return_at = redirect_at + timedelta(days=RNG.uniform(0.1, 20))
                add_event(
                    EventType.app_reopened_post_redirect, user.id, return_at,
                    {"days_since_redirect": (return_at - redirect_at).days},
                )

            if not returned:
                continue

            uploaded_at = return_at + timedelta(hours=RNG.uniform(0.5, 48))
            add_event(EventType.certificate_uploaded, user.id, uploaded_at, {"kind": "url"})

            if RNG.random() >= 0.82:
                continue  # висит в очереди на проверку

            approved = RNG.random() < 0.78
            review_at = uploaded_at + timedelta(hours=RNG.uniform(1, 96))
            add_event(
                EventType.certificate_verified, user.id, review_at,
                {
                    "method": "manual",
                    "status": "approved" if approved else "rejected",
                    "reviewer_id": None,
                    "reason": None if approved else "не хватает практики",
                    "time_to_review": (review_at - uploaded_at).total_seconds(),
                },
            )
            if not approved:
                continue

            certified_active.append((user.id, review_at))

            if RNG.random() > 0.65:
                continue  # сертифицирован, но пока не активирован

            point_at = review_at
            for _ in range(RNG.randint(1, 4)):
                point_at = point_at + timedelta(days=RNG.uniform(0.2, 12))
                category = RNG.choice(TRASH_CATEGORIES)
                access = RNG.choices(ACCESS_TYPES, weights=ACCESS_WEIGHTS)[0]
                volume = round(RNG.uniform(0.3, 12.0), 1)
                mass = round(volume * RNG.uniform(60, 400), 1)
                cost = round(volume * RNG.uniform(3500, 15000), 0)
                org = RNG.choice(orgs) if RNG.random() < 0.75 else None
                add_event(
                    EventType.point_created, user.id, point_at,
                    {
                        "hypothesis_id": str(uuid.uuid4()),
                        "organization_id": str(org.id) if org else None,
                        "has_photo": RNG.random() < 0.85,
                        "trash_categories": [category],
                        "dominant_category": category,
                        "fraction": RNG.choice(FRACTIONS),
                        "access_type": access,
                        "volume_m3": volume,
                        "mass_kg": mass,
                        "cleanup_cost_rub": cost,
                        "monitoring_site_id": None,
                    },
                )
                if org is None:
                    continue

                add_event(
                    EventType.point_received_in_zone, user.id, point_at,
                    {"hypothesis_id": str(uuid.uuid4()), "organization_id": str(org.id)},
                )
                if RNG.random() >= 0.8:
                    continue  # осталось в очереди на верификацию

                validate_at = point_at + timedelta(hours=RNG.uniform(1, 240))
                status = RNG.choices(
                    ["approved", "rejected", "drone_requested"], weights=[70, 20, 10]
                )[0]
                add_event(
                    EventType.point_validated, staff_by_org[org.id].id, validate_at,
                    {
                        "hypothesis_id": str(uuid.uuid4()),
                        "organization_id": str(org.id),
                        "author_id": str(user.id),
                        "status": status,
                        "time_to_validate": (validate_at - point_at).total_seconds(),
                    },
                )
                if status != "approved" or RNG.random() >= 0.5:
                    continue

                event_id = str(uuid.uuid4())
                join_at = validate_at + timedelta(days=RNG.uniform(1, 20))
                add_event(EventType.cleanup_event_joined, user.id, join_at, {"event_id": event_id})
                if RNG.random() < 0.7:
                    add_event(
                        EventType.cleanup_event_completed, user.id,
                        join_at + timedelta(days=RNG.uniform(1, 10)),
                        {"event_id": event_id},
                    )

        # Всплеск для антифрод-витрины: один волонтёр, много точек за час.
        if certified_active:
            burst_user_id, base_at = RNG.choice(certified_active)
            burst_hour = base_at.replace(minute=0, second=0, microsecond=0)
            for j in range(12):
                add_event(
                    EventType.point_created, burst_user_id,
                    burst_hour + timedelta(minutes=RNG.uniform(0, 55)),
                    {
                        "hypothesis_id": str(uuid.uuid4()),
                        "organization_id": None,
                        "has_photo": j % 3 != 0,
                        "trash_categories": ["plastic"],
                        "dominant_category": "plastic",
                        "fraction": "macro",
                        "access_type": "on_foot",
                        "volume_m3": round(RNG.uniform(0.1, 1.0), 1),
                        "mass_kg": round(RNG.uniform(10, 60), 1),
                        "cleanup_cost_rub": round(RNG.uniform(500, 3000), 0),
                        "monitoring_site_id": None,
                    },
                )

        session.add_all(events)
        await session.commit()

        return {
            "organizations": len(orgs),
            "staff": len(staff_by_org),
            "volunteers": N_VOLUNTEERS,
            "events": len(events),
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="удалить прежний прогон и пересеять")
    args = parser.parse_args()

    async with async_session_factory() as session:
        seeded = await already_seeded(session)
        if seeded and not args.reset:
            print(
                f"Демо-данные уже засеяны (есть пользователи @{DEMO_EMAIL_DOMAIN}). "
                "Передайте --reset, чтобы пересеять."
            )
            return
        if seeded:
            await reset(session)

    stats = await seed()
    print("Готово:", stats)


if __name__ == "__main__":
    asyncio.run(main())
