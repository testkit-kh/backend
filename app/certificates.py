"""Выдача и проверка сертификатов «Чистого берега» (PLAN.md 5.6)."""

from __future__ import annotations

import io
import logging
import secrets
import string
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import EventType, emit
from app.auth import get_current_user
from app.database import get_session
from app.models import (
    Hypothesis,
    HypothesisStatus,
    IssuedCertificate,
    User,
    UserRole,
    Volunteer,
)
from app.schemas import CertificateVerificationOut, IssuedCertificateOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/certificates", tags=["certificates"])

COURSE_TITLE = "Школа Защитников Природы"
_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))


def _pdf_url(code: str) -> str:
    return f"/api/v1/certificates/{code}/pdf"


async def _points_confirmed(session: AsyncSession, volunteer: Volunteer) -> int:
    statuses = [
        HypothesisStatus.approved,
        HypothesisStatus.cleaned,
        HypothesisStatus.drone_requested,
    ]
    count = await session.scalar(
        select(func.count(Hypothesis.id)).where(
            Hypothesis.author_id == volunteer.user_id,
            Hypothesis.status.in_(statuses),
        )
    )
    return int(count or 0)


async def issue_for_volunteer(
    session: AsyncSession,
    volunteer: Volunteer,
    *,
    full_name: str,
) -> IssuedCertificate:
    """Создать или вернуть уже выданный сертификат после approve."""
    existing = await session.scalar(
        select(IssuedCertificate).where(IssuedCertificate.volunteer_id == volunteer.id)
    )
    if existing is not None and existing.revoked_at is None:
        return existing

    points = await _points_confirmed(session, volunteer)
    # Часы — грубая оценка: 2 ч курс + 0.5 ч на подтверждённую точку.
    hours = round(2.0 + points * 0.5, 1)

    if existing is not None:
        # Был отозван — перевыпускаем с новым кодом.
        existing.code = _new_code()
        existing.full_name = full_name
        existing.course = COURSE_TITLE
        existing.points_confirmed = points
        existing.hours = hours
        existing.issued_at = datetime.now(UTC)
        existing.revoked_at = None
        await session.flush()
        return existing

    cert = IssuedCertificate(
        code=_new_code(),
        volunteer_id=volunteer.id,
        full_name=full_name,
        course=COURSE_TITLE,
        points_confirmed=points,
        hours=hours,
    )
    session.add(cert)
    await session.flush()
    return cert


def _find_font() -> Path | None:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/DejaVuSans.ttf"),
        Path(__file__).resolve().parent / "assets" / "DejaVuSans.ttf",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def render_pdf(cert: IssuedCertificate) -> bytes:
    """PDF сертификата. Кириллица через системный TTF, иначе латиница."""
    try:
        from fpdf import FPDF
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("fpdf2 is required to render certificates") from exc

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    font = _find_font()
    if font:
        pdf.add_font("Cert", "", str(font))
        pdf.set_font("Cert", size=28)
    else:
        pdf.set_font("Helvetica", size=28)
        logger.warning("No TTF for certificate PDF — falling back to Helvetica")

    pdf.set_xy(20, 40)
    title = "Сертификат «Чистый берег»" if font else "Certificate — Chistyi bereg"
    pdf.cell(0, 14, title, align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font(pdf.font_family, size=16)
    pdf.ln(10)
    name = cert.full_name if font else cert.full_name.encode("ascii", "replace").decode()
    pdf.cell(0, 10, name, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font(pdf.font_family, size=12)
    course = cert.course if font else "School of Nature Defenders"
    pdf.cell(0, 8, course, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    issued = cert.issued_at.astimezone(UTC).strftime("%d.%m.%Y")
    if font:
        line = (
            f"Выдан {issued} · точек подтверждено: "
            f"{cert.points_confirmed} · часов: {cert.hours}"
        )
    else:
        line = f"Issued {issued} · points {cert.points_confirmed} · hours {cert.hours}"
    pdf.cell(0, 8, line, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font(pdf.font_family, size=11)
    pdf.cell(0, 8, f"№ {cert.code}", align="C", new_x="LMARGIN", new_y="NEXT")

    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()


@router.get(
    "/me",
    response_model=IssuedCertificateOut,
    summary="Мой выданный сертификат",
)
async def my_certificate(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if user.role != UserRole.volunteer or user.volunteer is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Volunteers only.")
    volunteer = user.volunteer
    cert = await session.scalar(
        select(IssuedCertificate).where(
            IssuedCertificate.volunteer_id == volunteer.id,
            IssuedCertificate.revoked_at.is_(None),
        )
    )
    # Approve мог пройти до появления таблицы issued_certificates —
    # догоняем выдачу при первом запросе, иначе /course и /verify пустые.
    if cert is None and volunteer.is_trained:
        cert = await issue_for_volunteer(session, volunteer, full_name=user.full_name)
    if cert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Certificate not issued yet.",
        )
    return IssuedCertificateOut(
        code=cert.code,
        pdf_url=_pdf_url(cert.code),
        issued_at=cert.issued_at,
    )


@router.get(
    "/verify/{code}",
    response_model=CertificateVerificationOut,
    summary="Публичная проверка сертификата",
)
async def verify_certificate(
    code: str,
    session: AsyncSession = Depends(get_session),
):
    cert = await session.scalar(
        select(IssuedCertificate).where(IssuedCertificate.code == code.upper().strip())
    )
    if cert is None:
        return CertificateVerificationOut(valid=False)
    if cert.revoked_at is not None:
        return CertificateVerificationOut(
            valid=True,
            revoked=True,
            revoked_at=cert.revoked_at,
        )
    return CertificateVerificationOut(
        valid=True,
        revoked=False,
        full_name=cert.full_name,
        course=cert.course,
        issued_at=cert.issued_at,
        points_confirmed=cert.points_confirmed,
        hours=cert.hours,
    )


@router.get(
    "/{code}/pdf",
    summary="PDF сертификата",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def certificate_pdf(
    code: str,
    session: AsyncSession = Depends(get_session),
):
    cert = await session.scalar(
        select(IssuedCertificate).where(IssuedCertificate.code == code.upper().strip())
    )
    if cert is None or cert.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certificate not found.")
    data = render_pdf(cert)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="certificate-{cert.code}.pdf"'},
    )


@router.post(
    "/{code}/share",
    summary="Отметить шеринг сертификата (аналитика)",
)
async def share_certificate(
    code: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    cert = await session.scalar(
        select(IssuedCertificate).where(IssuedCertificate.code == code.upper().strip())
    )
    if cert is None or cert.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Certificate not found.")
    if user.role == UserRole.volunteer and user.volunteer is not None:
        if cert.volunteer_id != user.volunteer.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not your certificate.",
            )
    await emit(
        session,
        EventType.certificate_shared,
        user_id=user.id,
        payload={"code": cert.code},
    )
    return {"ok": True}
