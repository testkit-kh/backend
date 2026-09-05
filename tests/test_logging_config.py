"""Рабочая директория и чужой basicConfig — регрессия на резолвинг участков.

Резолвинг падал с `[Errno 13] Permission denied: '/app/debug.log'`:
rosreestr2coord на импорте своего модуля logger выполняет
logging.basicConfig(filename="debug.log") и тем самым открывает файл в рабочей
директории. В контейнере это /app, принадлежащий root, а процесс работает под
appuser.

Фикс держится на одном свойстве: basicConfig ничего не делает, если у корневого
логгера уже есть обработчики. Проверять это внутри pytest нельзя — его плагин
логирования сам вешает обработчик на корневой логгер, и чужой basicConfig
оказался бы обезврежен и без нашего кода. Поэтому каждая проверка идёт в
отдельном процессе с чистым логированием.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_in(workdir: Path, code: str) -> None:
    """Выполнить код в отдельном процессе с рабочей директорией workdir."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=workdir,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_library_writes_log_into_working_directory_on_import(tmp_path):
    """Фиксирует поведение зависимости, ради которого существует
    app/logging_config.py.

    Если тест покраснел — библиотеку починили наверху, и обходной путь можно
    убирать.
    """
    _run_in(tmp_path, "import rosreestr2coord.parser")

    assert (tmp_path / "debug.log").exists()


def test_configure_logging_keeps_library_from_opening_the_file(tmp_path):
    """Файл не просто пишется в другое место — он не открывается вовсе,
    поэтому права на рабочую директорию перестают что-либо значить."""
    _run_in(
        tmp_path,
        "from app.logging_config import configure_logging;"
        "configure_logging();"
        "import rosreestr2coord.parser",
    )

    assert not (tmp_path / "debug.log").exists()


def test_media_path_is_outside_the_working_directory(tmp_path, monkeypatch):
    """Второе место, где библиотека пишет в рабочую директорию: Area в
    конструкторе делает makedirs(media_path/tmp), а media_path по умолчанию —
    os.getcwd()."""
    from app.rosreestr import _media_path

    monkeypatch.chdir(tmp_path)
    path = Path(_media_path())

    assert path.is_absolute()
    assert path != tmp_path and tmp_path not in path.parents
    assert os.access(path, os.W_OK)
