"""Ручка автозаполнения формы регистрации ООПТ по ИНН."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.registry.providers import RegistryUnavailable
from app.registry.service import InvalidInn, lookup_company
from app.schemas import CompanyInfoOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/registry", tags=["registry"])


@router.get(
    "/company",
    response_model=CompanyInfoOut,
    summary="Сведения об организации по ИНН (для автозаполнения формы)",
)
async def company_by_inn(
    inn: str = Query(min_length=10, max_length=14, description="ИНН, 10 или 12 цифр"),
    session: AsyncSession = Depends(get_session),
):
    # Намеренно без авторизации: ручка нужна на форме регистрации, где токена
    # ещё нет. Отдаёт только сведения из открытого реестра ЕГРЮЛ — ничего,
    # чего нельзя было бы получить на сайте ФНС.
    try:
        info = await lookup_company(session, inn)
    except InvalidInn as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    except RegistryUnavailable as error:
        # 503, а не 500: это внешний сбой, и фронт по нему показывает
        # «заполните вручную», а не «что-то пошло не так».
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Реестр организаций сейчас недоступен, заполните поля вручную.",
        ) from error

    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Организация с таким ИНН в ЕГРЮЛ не найдена.",
        )

    return CompanyInfoOut(**info.__dict__)
