"""
Контрольная сумма ИНН — проверка без единого сетевого запроса.

Первая линия обороны при регистрации ООПТ: опечатка в цифре отсеивается
мгновенно, до похода во внешний реестр. Алгоритм официальный (приказ ФНС),
повторяет тот, что уже реализован на фронте в `src/lib/registry.ts` — обе
стороны обязаны давать одинаковый ответ, иначе форма будет спорить с API.

Валидная контрольная сумма НЕ означает, что организация существует: она лишь
означает, что число похоже на ИНН. Существование проверяет `providers.py`.
"""

from __future__ import annotations

_WEIGHTS_10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_WEIGHTS_12_1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_WEIGHTS_12_2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _check_digit(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(w * d for w, d in zip(weights, digits)) % 11 % 10


def normalize_inn(raw: str) -> str:
    """Оставляет только цифры: люди вставляют ИНН с пробелами и из Excel."""
    return "".join(ch for ch in raw if ch.isdigit())


def is_valid_inn(raw: str) -> bool:
    """True, если контрольная сумма сходится (10 цифр — ЮЛ, 12 — ИП/физлицо)."""
    inn = normalize_inn(raw)
    digits = [int(ch) for ch in inn]

    if len(inn) == 10:
        return _check_digit(digits[:9], _WEIGHTS_10) == digits[9]
    if len(inn) == 12:
        return (
            _check_digit(digits[:10], _WEIGHTS_12_1) == digits[10]
            and _check_digit(digits[:11], _WEIGHTS_12_2) == digits[11]
        )
    return False
