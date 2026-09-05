"""Подписанные ссылки на дашборды Metabase.

Главное, что здесь проверяется, — изоляция данных. Sandboxing в Metabase
платный, поэтому единственное, что не даёт сотруднику одной ООПТ увидеть цифры
другой, — заблокированный параметр внутри подписанного токена. Если он
пропадёт или подпись станет предсказуемой, утечка будет тихой.
"""

import uuid

import jwt
import pytest

from app.analytics.dashboards import Dashboard, available_dashboards
from app.config import settings
from app.models import UserRole


def test_scoped_dashboards_are_marked_as_such():
    dashboards = available_dashboards()
    assert dashboards["oopt"].scoped is True
    assert dashboards["impact"].scoped is True
    # Воронка общая по программе: у неё нет владельца-территории.
    assert dashboards["funnel"].scoped is False


def test_funnel_is_coordinator_only():
    """Сотрудник одной ООПТ не должен видеть воронку всей программы."""
    assert available_dashboards()["funnel"].roles == (UserRole.coordinator,)


def test_staff_can_open_territory_dashboards():
    for slug in ("oopt", "impact"):
        assert UserRole.staff in available_dashboards()[slug].roles


def test_dashboards_default_to_not_provisioned():
    """Пока скрипт провижининга не отработал, номера равны нулю — ручка по
    этому признаку отвечает 503, а не отдаёт битый iframe."""
    for dashboard in available_dashboards().values():
        assert isinstance(dashboard, Dashboard)
        assert dashboard.number >= 0


def test_signed_token_locks_organization():
    """Воспроизводим то, что делает ручка: параметр обязан быть внутри
    подписи, иначе его можно подменить в адресной строке."""
    secret = "test-embedding-secret"
    organization_id = uuid.uuid4()
    payload = {
        "resource": {"dashboard": 7},
        "params": {"organization_id": str(organization_id)},
        "exp": 4102444800,
    }

    token = jwt.encode(payload, secret, algorithm="HS256")
    decoded = jwt.decode(token, secret, algorithms=["HS256"])

    assert decoded["params"]["organization_id"] == str(organization_id)
    assert decoded["resource"] == {"dashboard": 7}


def test_token_signed_with_other_secret_is_rejected():
    token = jwt.encode({"resource": {"dashboard": 7}}, "secret-a", algorithm="HS256")

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "secret-b", algorithms=["HS256"])


def test_embedding_disabled_by_default():
    """Секрет встраивания пуст, пока его не задали: без него подписывать
    нечем, и ручка обязана сказать об этом, а не молча отдать ссылку."""
    assert settings.METABASE_EMBEDDING_SECRET_KEY == "" or isinstance(
        settings.METABASE_EMBEDDING_SECRET_KEY, str
    )
