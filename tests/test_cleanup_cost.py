"""Оценка объёма, массы и стоимости уборки."""

import pytest

from app.cleanup_cost import (
    DEFAULT_LAYER_DEPTH_M,
    AccessType,
    TrashCategory,
    estimate_cleanup,
    estimate_volume_m3,
)


def test_volume_taken_as_given_when_provided():
    assert estimate_volume_m3(volume_m3=12.0, area_m2=999.0) == 12.0


def test_volume_derived_from_area_when_missing():
    assert estimate_volume_m3(volume_m3=None, area_m2=100.0) == 100.0 * DEFAULT_LAYER_DEPTH_M


def test_volume_unknown_when_nothing_given():
    """None, а не ноль: нулевой объём попал бы в суммы по ООПТ как факт."""
    assert estimate_volume_m3(volume_m3=None, area_m2=None) is None


def test_estimate_returns_none_without_volume():
    assert (
        estimate_cleanup(
            volume_m3=None,
            area_m2=None,
            dominant=TrashCategory.plastic,
            access=AccessType.on_foot,
        )
        is None
    )


@pytest.mark.parametrize("volume", [0.0, -5.0])
def test_estimate_rejects_non_positive_volume(volume):
    assert (
        estimate_cleanup(
            volume_m3=volume,
            area_m2=None,
            dominant=TrashCategory.plastic,
            access=AccessType.on_foot,
        )
        is None
    )


def test_access_dominates_the_bill():
    """Способ заброски важнее объёма — на этом стоит вся экономика проекта:
    Командорские острова и Куршская коса при одинаковой свалке стоят разного."""
    common = dict(
        volume_m3=10.0, area_m2=None, dominant=TrashCategory.fishing_gear
    )
    on_foot = estimate_cleanup(**common, access=AccessType.on_foot)
    helicopter = estimate_cleanup(**common, access=AccessType.helicopter)

    assert on_foot is not None and helicopter is not None
    assert helicopter.total_rub > on_foot.total_rub * 10


def test_mobilisation_is_independent_of_volume():
    """Вертолёт летит один раз — поэтому точки выгодно группировать в выезд."""
    small = estimate_cleanup(
        volume_m3=1.0, area_m2=None,
        dominant=TrashCategory.plastic, access=AccessType.helicopter,
    )
    large = estimate_cleanup(
        volume_m3=50.0, area_m2=None,
        dominant=TrashCategory.plastic, access=AccessType.helicopter,
    )

    assert small is not None and large is not None
    assert small.mobilisation_rub == large.mobilisation_rub


def test_mass_follows_bulk_density_of_dominant_category():
    """Пластик лежит рыхло, строительный мусор — плотно: при равном объёме
    масса отличается на порядок, и от неё зависит транспорт."""
    plastic = estimate_cleanup(
        volume_m3=10.0, area_m2=None,
        dominant=TrashCategory.plastic, access=AccessType.on_foot,
    )
    construction = estimate_cleanup(
        volume_m3=10.0, area_m2=None,
        dominant=TrashCategory.construction, access=AccessType.on_foot,
    )

    assert plastic is not None and construction is not None
    assert construction.mass_kg > plastic.mass_kg * 10


def test_assumptions_are_exposed():
    """Коэффициенты отдаются наружу: смета без объяснения — это не смета."""
    estimate = estimate_cleanup(
        volume_m3=5.0, area_m2=None,
        dominant=TrashCategory.glass, access=AccessType.boat,
    )

    assert estimate is not None
    assert estimate.assumptions["bulk_density_kg_m3"] > 0
    assert "допущения" in str(estimate.assumptions["source"])
