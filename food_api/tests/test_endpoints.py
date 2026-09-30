"""Контракты эндпоинтов: авторизация, валидация, форма ответа."""

import io
import json
import urllib.request

import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient


def test_health_needs_no_auth(api):
    with TestClient(api.app) as anon:
        resp = anon.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_missing_credentials_is_401(api):
    """Изменено в Фазе 2 (было 422).

    422 был артефактом обязательного Header(...), а не решением: FastAPI
    отвергал запрос как невалидный до того, как дело доходило до проверки
    ключа. Теперь заголовок опционален (есть альтернатива — кука сессии),
    и отсутствие учётных данных — это ровно 401. Shortcut и garmin-sync
    заголовок всегда шлют, их это не касается.
    """
    with TestClient(api.app) as anon:
        assert anon.get("/day").status_code == 401


def test_wrong_api_key_is_401(api):
    with TestClient(api.app) as anon:
        resp = anon.get("/day", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


def test_valid_api_key_passes(client):
    assert client.get("/day").status_code == 200


def test_log_returns_updated_day(client):
    resp = client.post(
        "/logs",
        json={
            "items": [
                {
                    "name": "гречка",
                    "quantity": 180,
                    "unit": "g",
                    "calories_kcal": 617,
                    "protein_g": 23,
                    "fat_total_g": 6,
                    "carbs_g": 125,
                }
            ],
            "meal_type": "lunch",
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["items_saved"] == 1
    assert body["day"]["totals"]["calories_kcal"] == 617, "ответ уже содержит день"


@pytest.mark.parametrize("missing", ["calories_kcal", "protein_g", "fat_total_g", "carbs_g"])
def test_macros_are_required(client, missing):
    """Макросы обязательны намеренно: иначе модель их пропустит."""
    item = {
        "name": "еда",
        "quantity": 100,
        "unit": "g",
        "calories_kcal": 100,
        "protein_g": 1,
        "fat_total_g": 1,
        "carbs_g": 1,
    }
    del item[missing]
    resp = client.post("/logs", json={"items": [item]})
    assert resp.status_code == 422


def test_empty_items_rejected(client):
    assert client.post("/logs", json={"items": []}).status_code == 422


def test_log_can_be_deleted(client, log_food):
    log_id = log_food(calories_kcal=500)["log_id"]
    assert client.delete(f"/logs/{log_id}").status_code == 200
    assert client.get("/day").json()["totals"]["calories_kcal"] == 0


def test_days_range_rejects_reversed_bounds(client):
    resp = client.get("/days", params={"start": "2026-09-19", "end": "2026-09-01"})
    assert resp.status_code == 400


def test_export_queue_hands_out_then_acks(client, log_food):
    """Очередь Apple Health: запись выдаётся до ack и не выдаётся после."""
    log_food(calories_kcal=500)

    pending = client.get("/export/pending").json()
    assert pending["count"] == 1
    assert pending["items"][0]["calories_kcal"] == 500
    # Shortcut разбирает только локальное время без смещения
    assert pending["items"][0]["consumed_at_local"].count(":") == 2

    assert client.post("/export/ack", params={"token": pending["ack_token"]}).json()["acked"] == 1

    after = client.get("/export/pending").json()
    assert after["count"] == 0 and after["items"] == []


def test_products_maps_off_response(client, monkeypatch):
    """/products переводит ответ OFF в нашу схему (натрий г -> мг)."""
    payload = {
        "products": [
            {
                "product_name": "High Protein Pudding",
                "brands": "Ehrmann",
                "nutriments": {
                    "energy-kcal_100g": 78,
                    "proteins_100g": 10.0,
                    "fat_100g": 1.4,
                    "carbohydrates_100g": 6.2,
                    "sodium_100g": 0.08,
                },
            },
            {"product_name": "без калорий", "nutriments": {}},
        ]
    }
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(payload).encode())
    )

    products = client.get("/products", params={"query": "ehrmann pudding"}).json()["products"]
    assert len(products) == 1, "позиция без калорий отбрасывается"
    assert products[0]["per_100g"]["sodium_mg"] == 80.0
    assert products[0]["per_100g"]["protein_g"] == 10.0


def test_products_reports_upstream_failure_as_502(client, monkeypatch):
    def _explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _explode)
    assert client.get("/products", params={"query": "что-нибудь"}).status_code == 502


def test_api_key_header_still_works_for_shortcut_and_garmin(client):
    """Регрессия: интеграции ходят по X-API-Key, его ломать нельзя."""
    assert client.get("/export/pending", headers={"X-API-Key": API_KEY}).status_code == 200
