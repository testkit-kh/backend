"""
Оценка объёма, массы и стоимости уборки по данным, которые заполняет человек.

Все коэффициенты ниже — **явные допущения**, а не измеренные величины. Задача
хакатона прямо требует фиксировать допущения, а не выдавать их за факты, поэтому
каждое число здесь названо, объяснено и вынесено в одно место: когда у Фонда
появится своя смета, правится только этот файл.

Почему стоимость вообще считается на бэке, а не «на глаз»:
география проекта — Командорские острова, Земля Франца-Иосифа, Кроноцкий
заповедник. Там стоимость уборки определяется не объёмом мусора, а способом
заброски: один вертолёто-час дороже, чем вывоз всей свалки с Куршской косы.
Поэтому доступность — главный множитель, а не второстепенный.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class TrashCategory(str, enum.Enum):
    """Состав мусора. Список — по методике учёта пляжного мусора проекта."""

    plastic = "plastic"                # пластик, тара, плёнка
    fishing_gear = "fishing_gear"      # сети, тросы, буи, ярусы
    glass = "glass"
    metal = "metal"                    # бочки, металлолом
    wood = "wood"                      # обработанная древесина, поддоны
    rubber = "rubber"                  # покрышки, резина
    hazardous = "hazardous"            # нефтепродукты, ГСМ, химия
    household = "household"            # бытовой мусор
    construction = "construction"      # строительный мусор, грунт
    other = "other"


class TrashFraction(str, enum.Enum):
    """Фракция по общепринятой классификации морского мусора."""

    mega = "mega"      # > 1 м
    macro = "macro"    # 2.5 см – 1 м
    meso = "meso"      # 0.5 – 2.5 см
    micro = "micro"    # < 0.5 см


class AccessType(str, enum.Enum):
    """Как физически попасть на точку. Главный драйвер стоимости."""

    on_foot = "on_foot"          # пешком от дороги
    vehicle = "vehicle"          # подъезд транспорта
    boat = "boat"                # только с воды
    helicopter = "helicopter"    # только авиацией


# ---------------------------------------------------------------------------
# Допущения
# ---------------------------------------------------------------------------

#: Насыпная плотность, кг/м³. Не плотность материала: мусор на берегу лежит
#: рыхло, пустоты занимают большую часть объёма.
BULK_DENSITY_KG_M3: dict[TrashCategory, float] = {
    TrashCategory.plastic: 60,
    TrashCategory.fishing_gear: 150,
    TrashCategory.glass: 400,
    TrashCategory.metal: 250,
    TrashCategory.wood: 300,
    TrashCategory.rubber: 350,
    TrashCategory.hazardous: 900,
    TrashCategory.household: 120,
    TrashCategory.construction: 800,
    TrashCategory.other: 150,
}

#: Сбор, сортировка и затаривание одного м³ на месте, ₽. Без логистики.
HANDLING_RUB_PER_M3 = 3_500.0

#: Множитель к работам по способу доступа: пешая переноска мешков по камням
#: дороже, чем погрузка в машину у дороги.
ACCESS_WORK_MULTIPLIER: dict[AccessType, float] = {
    AccessType.vehicle: 0.8,
    AccessType.on_foot: 1.0,
    AccessType.boat: 2.5,
    AccessType.helicopter: 8.0,
}

#: Разовая мобилизация на выезд, ₽ — не зависит от объёма. Именно она делает
#: экономику удалённых территорий: одна точка на ЗФИ или сто — вертолёт летит
#: один раз, поэтому точки выгодно группировать в один выезд.
MOBILISATION_RUB: dict[AccessType, float] = {
    AccessType.on_foot: 0.0,
    AccessType.vehicle: 15_000.0,
    AccessType.boat: 120_000.0,
    AccessType.helicopter: 900_000.0,
}

#: Средняя толщина слоя мусора, м — если человек указал площадь, но не объём.
DEFAULT_LAYER_DEPTH_M = 0.15


@dataclass(frozen=True)
class CleanupEstimate:
    """Результат оценки. Все поля — производные, ничего не хранится «на веру»."""

    volume_m3: float
    mass_kg: float
    handling_rub: float
    mobilisation_rub: float
    total_rub: float
    assumptions: dict[str, float | str]


def estimate_volume_m3(
    *,
    volume_m3: float | None,
    area_m2: float | None,
    depth_m: float | None = None,
) -> float | None:
    """Объём: как указал человек, иначе площадь × толщина слоя."""
    if volume_m3 is not None:
        return volume_m3
    if area_m2 is not None:
        return area_m2 * (depth_m or DEFAULT_LAYER_DEPTH_M)
    return None


def estimate_mass_kg(volume_m3: float, dominant: TrashCategory) -> float:
    return volume_m3 * BULK_DENSITY_KG_M3[dominant]


def estimate_cleanup(
    *,
    volume_m3: float | None,
    area_m2: float | None,
    dominant: TrashCategory,
    access: AccessType,
    depth_m: float | None = None,
) -> CleanupEstimate | None:
    """Полная оценка. Возвращает None, если объём определить не из чего —
    молча подставлять ноль нельзя: это исказит и смету, и KPI по объёмам."""
    volume = estimate_volume_m3(volume_m3=volume_m3, area_m2=area_m2, depth_m=depth_m)
    if volume is None or volume <= 0:
        return None

    mass = estimate_mass_kg(volume, dominant)
    handling = volume * HANDLING_RUB_PER_M3 * ACCESS_WORK_MULTIPLIER[access]
    mobilisation = MOBILISATION_RUB[access]

    return CleanupEstimate(
        volume_m3=round(volume, 3),
        mass_kg=round(mass, 1),
        handling_rub=round(handling, 2),
        mobilisation_rub=mobilisation,
        total_rub=round(handling + mobilisation, 2),
        assumptions={
            "bulk_density_kg_m3": BULK_DENSITY_KG_M3[dominant],
            "handling_rub_per_m3": HANDLING_RUB_PER_M3,
            "access_work_multiplier": ACCESS_WORK_MULTIPLIER[access],
            "mobilisation_rub": MOBILISATION_RUB[access],
            "depth_m": depth_m or DEFAULT_LAYER_DEPTH_M,
            "source": "проектные допущения, не фактическая смета",
        },
    )
