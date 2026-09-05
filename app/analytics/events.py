"""
Event taxonomy and the single write path into `analytics_events`.

Every KPI in the project passport is computed from this table — there is no
separate analytics service. The names below are the contract: they must match
the KPI document exactly, because the `kpi_*` views select on them by string.

Naming note: the course platform is iSpring («Школа Защитников Природы»), but
the event names stay platform-neutral (`course_*`) so that swapping the LMS
does not invalidate historical data.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from geoalchemy2.functions import ST_MakePoint, ST_SetSRID
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AnalyticsEvent


class EventType(str, enum.Enum):
    # ---- Volunteer journey ------------------------------------------------
    user_registered = "user_registered"
    course_redirect_click = "course_redirect_click"
    reminder_sent = "reminder_sent"
    reminder_clicked = "reminder_clicked"
    app_reopened_post_redirect = "app_reopened_post_redirect"
    certificate_uploaded = "certificate_uploaded"
    certificate_verified = "certificate_verified"
    certificate_shared = "certificate_shared"
    map_access_granted = "map_access_granted"
    point_created = "point_created"
    cleanup_event_joined = "cleanup_event_joined"
    cleanup_event_completed = "cleanup_event_completed"
    #: Приёмка фото «до/после»: доказательство, что уборка состоялась.
    #: Третье содержательное событие для kpi.retention_30d.
    cleanup_event_before_after = "cleanup_event_before_after"

    # ---- ООПТ journey -----------------------------------------------------
    oopt_registered = "oopt_registered"
    inn_verification = "inn_verification"
    geo_zone_created = "geo_zone_created"
    point_received_in_zone = "point_received_in_zone"
    point_validated = "point_validated"
    cleanup_event_created = "cleanup_event_created"
    aerial_survey_requested = "aerial_survey_requested"


async def emit(
    session: AsyncSession,
    event_type: EventType,
    *,
    user_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> None:
    """Append one event.

    Does not commit — the request-scoped session commits once in
    `get_session`, so an event is written if and only if the business
    transaction that produced it succeeded.
    """
    geo = None
    if lat is not None and lon is not None:
        # PostGIS point order is (x=lon, y=lat).
        geo = ST_SetSRID(ST_MakePoint(lon, lat), 4326)

    session.add(
        AnalyticsEvent(
            user_id=user_id,
            event_type=event_type.value,
            payload=payload or {},
            geo=geo,
        )
    )
