"""Возраст, согласие представителя и контрольная сумма ИНН."""

from datetime import date

import pytest

from app.age import ADULT_AGE, MIN_AGE, age_at, required_consent_status
from app.models import ConsentStatus
from app.registry.checksum import is_valid_inn, normalize_inn

# ---------------------------------------------------------------------------
# Возраст
# ---------------------------------------------------------------------------


def test_age_counts_full_years_before_birthday():
    """Классическая ошибка «текущий год минус год рождения» даёт здесь 18,
    хотя человеку ещё 17 — и от этого зависит, нужен ли документ."""
    assert age_at(date(2008, 12, 31), on=date(2026, 6, 1)) == 17


def test_age_increments_on_the_birthday_itself():
    assert age_at(date(2008, 6, 1), on=date(2026, 6, 1)) == 18


@pytest.mark.parametrize(
    ("birth_year", "expected"),
    [
        (2011, ConsentStatus.awaiting),  # 15 лет
        (2009, ConsentStatus.awaiting),  # 17 лет
        (2008, ConsentStatus.not_required),  # 18 лет
        (1990, ConsentStatus.not_required),
    ],
)
def test_consent_required_only_under_eighteen(birth_year, expected):
    assert required_consent_status(date(birth_year, 1, 1)) == expected


def test_consent_not_required_without_birth_date():
    """Записи заводились без даты рождения — ретроспективно блокировать
    уже работающих волонтёров нельзя."""
    assert required_consent_status(None) == ConsentStatus.not_required


def test_age_boundaries_are_the_documented_ones():
    assert MIN_AGE == 14
    assert ADULT_AGE == 18


# ---------------------------------------------------------------------------
# ИНН
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inn",
    [
        "7707083893",  # ПАО Сбербанк, 10 знаков
        "500100732259",  # 12 знаков
    ],
)
def test_valid_inn_checksums(inn):
    assert is_valid_inn(inn) is True


@pytest.mark.parametrize(
    "inn",
    [
        "7707083894",  # последняя цифра испорчена
        "500100732258",
        "123456789",  # короткий
        "12345678901",  # 11 знаков — такой длины ИНН не бывает
        "",
        "abcdefghij",
    ],
)
def test_invalid_inn_rejected(inn):
    assert is_valid_inn(inn) is False


def test_inn_normalised_before_check():
    """Люди вставляют ИНН из Excel и с пробелами — это не повод отказывать."""
    assert normalize_inn(" 7707 083 893 ") == "7707083893"
    assert is_valid_inn(" 7707 083 893 ") is True
