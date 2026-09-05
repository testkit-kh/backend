"""
Настройка логирования процесса.

Нужна не для красоты вывода. `rosreestr2coord` на импорте своего модуля
`logger` выполняет

    logging.basicConfig(filename="debug.log", level=logging.DEBUG)

то есть на правах библиотеки забирает корневой логгер всего процесса и
открывает файл в текущей рабочей директории. Последствий два. В контейнере
рабочая директория — /app, принадлежащий root, а процесс работает под appuser:
импорт падает с PermissionError, и участок остаётся без границ. Там же, где
писать можно, вывод всего приложения молча уезжает в debug.log на уровне DEBUG.

`basicConfig` ничего не делает, если у корневого логгера уже есть обработчики,
поэтому надёжный способ обезвредить чужой вызов — настроить логирование до
него. Отсюда требование: `configure_logging()` вызывается раньше любого
обращения к библиотеке.
"""

from __future__ import annotations

import logging

from app.config import settings


def configure_logging() -> None:
    """Идемпотентно настраивает корневой логгер на stderr.

    Сама сделана через basicConfig: повторный вызов, как и чужой, не изменит
    уже настроенное логирование.
    """
    logging.basicConfig(
        level=settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Библиотека пишет на DEBUG каждый свой HTTP-запрос. Нам от неё нужны
    # только жалобы.
    logging.getLogger("rosreestr2coord").setLevel(logging.WARNING)
