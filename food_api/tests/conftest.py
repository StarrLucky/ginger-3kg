"""Общие фикстуры.

Важно: DB_PATH, API_KEY и USER_TZ в api.py — модульные константы, прочитанные
при импорте. monkeypatch.setenv на них уже не влияет, поэтому патчим атрибуты
модуля: функции читают эти глобалы в момент вызова.
"""

import urllib.request
from zoneinfo import ZoneInfo

import api as api_module
import pytest
from fastapi.testclient import TestClient

API_KEY = "test-key"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Ронять тест при попытке реального сетевого запроса.

    Без этого CI однажды начнёт ходить в Open Food Facts и падать по чужой
    недоступности. Тесты, которым нужен ответ OFF, мокают urlopen сами.
    """

    def _boom(*args, **kwargs):
        raise AssertionError("тест попытался сходить в сеть — замокай запрос")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Модуль api с изолированной БД и известным ключом."""
    monkeypatch.setattr(api_module, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(api_module, "API_KEY", API_KEY)
    monkeypatch.setattr(api_module, "USER_TZ", ZoneInfo("Europe/Moscow"))
    return api_module


@pytest.fixture
def client(api):
    """TestClient с валидным ключом по умолчанию."""
    with TestClient(api.app) as c:
        c.headers.update({"X-API-Key": API_KEY})
        yield c


@pytest.fixture
def log_food(client):
    """Записать один приём пищи. Обязательные макросы по умолчанию — нули."""

    def _log(*, meal_type="lunch", consumed_at=None, **nutrients):
        item = {
            "name": nutrients.pop("name", "тестовая еда"),
            "quantity": nutrients.pop("quantity", 100),
            "unit": nutrients.pop("unit", "g"),
            "calories_kcal": nutrients.pop("calories_kcal", 0),
            "protein_g": nutrients.pop("protein_g", 0),
            "fat_total_g": nutrients.pop("fat_total_g", 0),
            "carbs_g": nutrients.pop("carbs_g", 0),
            **nutrients,
        }
        payload = {"items": [item], "meal_type": meal_type}
        if consumed_at:
            payload["consumed_at"] = consumed_at
        resp = client.post("/logs", json=payload)
        assert resp.status_code == 200, resp.text
        return resp.json()

    return _log
