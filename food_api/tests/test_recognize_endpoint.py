"""POST /recognize — склейка шага 1 (модель) и шага 2 (справочники).

Сеть не трогаем: распознаватель подменён заглушкой, httpx-клиент — MockTransport.
Главное, что здесь проверяется, — контракт ответа: черновик должен без правок
приниматься в POST /logs, иначе вся связка бесполезна.
"""

import httpx
import pytest
import recognize as rec

FULL_PANEL = {1008: ("kcal", 343.0), 1003: ("g", 13.25), 1004: ("g", 3.4), 1005: ("g", 71.5)}


class FakeRecognizer:
    """Заглушка шага 1: отдаёт заранее заданный разбор или падает."""

    name = "fake"

    def __init__(self, meal=None, error=None):
        self.meal = meal
        self.error = error
        self.calls = []
        self.closed = False

    async def recognize(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.meal

    async def aclose(self):
        self.closed = True


def meal(items=None, meal_type="lunch", notes="", confidence=0.8) -> rec.RecognizedMeal:
    if items is None:
        items = [
            rec.RecognizedItemOut(
                name="гречка",
                quantity=180,
                unit="g",
                kind="generic",
                lookup_query="buckwheat, cooked",
                brand=None,
            )
        ]
    return rec.RecognizedMeal(items=items, meal_type=meal_type, notes=notes, confidence=confidence)


def usda_ok(request: httpx.Request) -> httpx.Response:
    """Мок USDA: поиск отдаёт одного кандидата, карточка — полную панель."""
    if "foods/search" in request.url.path:
        return httpx.Response(200, json={"foods": [{"fdcId": 170286, "dataType": "SR Legacy"}]})
    return httpx.Response(
        200,
        json={
            "description": "Buckwheat",
            "foodNutrients": [
                {"nutrient": {"id": nid, "unitName": unit}, "amount": amount}
                for nid, (unit, amount) in FULL_PANEL.items()
            ],
        },
    )


@pytest.fixture
def wire(client, api, monkeypatch):
    """Подменить распознаватель и сетевой клиент у уже поднятого приложения."""
    monkeypatch.setattr(api, "USDA_API_KEY", "test-usda-key")

    def _wire(*, meal=None, error=None, handler=usda_ok):
        fake = FakeRecognizer(meal, error)
        api.app.state.recognizer = fake
        # Клиент из lifespan ни одного запроса не сделал — просто отпускаем его.
        api.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return fake

    return _wire


# --- вход --------------------------------------------------------------------


def test_empty_request_is_rejected(client, wire):
    fake = wire(meal=meal())
    resp = client.post("/recognize", json={})
    assert resp.status_code == 400
    assert fake.calls == [], "до модели такой запрос доходить не должен"


def test_oversized_image_is_rejected_before_the_model(client, wire):
    """Отказ по размеру — на границе API, а не после оплаченного вызова."""
    fake = wire(meal=meal())
    resp = client.post("/recognize", json={"image_b64": "A" * (rec.MAX_IMAGE_B64_CHARS + 1)})
    assert resp.status_code == 413
    assert str(rec.MAX_IMAGE_B64_CHARS) in resp.json()["detail"]
    assert fake.calls == [], "картинку сверх лимита в модель слать не надо"


def test_key_is_required(client, wire):
    wire(meal=meal())
    resp = client.post("/recognize", json={"text": "гречка"}, headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


# --- контракт ответа ---------------------------------------------------------


def test_draft_is_accepted_by_logs_unchanged(client, wire):
    """Ключевой тест: черновик должен постить­ся в /logs как есть."""
    wire(meal=meal())
    draft = client.post("/recognize", json={"text": "180 г гречки"}).json()["draft"]

    saved = client.post("/logs", json=draft)
    assert saved.status_code == 200, saved.text
    assert saved.json()["items_saved"] == 1


def test_macros_come_from_the_reference_not_the_model(client, wire):
    wire(meal=meal())
    [item] = client.post("/recognize", json={"text": "180 г гречки"}).json()["draft"]["items"]

    assert item["calories_kcal"] == pytest.approx(617.4)  # 343 * 1.8
    assert item["source_ref"].startswith("USDA 170286")
    assert item["needs_manual"] is False


def test_recognize_saves_nothing(client, wire, api):
    wire(meal=meal())
    client.post("/recognize", json={"text": "180 г гречки"})

    day = client.get("/day").json()
    assert day["totals"]["calories_kcal"] == 0, "распознавание — это ещё не запись"


def test_user_meal_type_wins_over_the_model(client, wire):
    wire(meal=meal(meal_type="lunch"))
    body = client.post("/recognize", json={"text": "гречка", "meal_type": "dinner"}).json()
    assert body["draft"]["meal_type"] == "dinner"


def test_model_meal_type_is_used_when_user_is_silent(client, wire):
    wire(meal=meal(meal_type="breakfast"))
    body = client.post("/recognize", json={"text": "овсянка"}).json()
    assert body["draft"]["meal_type"] == "breakfast"


def test_source_reflects_the_input(client, wire):
    wire(meal=meal())
    text = client.post("/recognize", json={"text": "гречка"}).json()
    photo = client.post("/recognize", json={"image_b64": "QUJD"}).json()
    assert text["draft"]["source"] == "text"
    assert photo["draft"]["source"] == "photo"


def test_local_time_is_passed_to_the_model(client, wire):
    """meal_type модель выводит из времени — без него она гадает вслепую."""
    fake = wire(meal=meal())
    client.post("/recognize", json={"text": "гречка", "consumed_at": "2026-03-01T08:30:00"})
    assert fake.calls[0]["now"].startswith("2026-03-01T08:30"), fake.calls[0]["now"]


def test_consumed_at_is_echoed_in_utc(client, wire):
    wire(meal=meal())
    body = client.post(
        "/recognize", json={"text": "гречка", "consumed_at": "2026-03-01T08:30:00"}
    ).json()
    # Europe/Moscow в фикстуре: 08:30 местного — это 05:30 UTC
    assert body["draft"]["consumed_at"].startswith("2026-03-01T05:30")


def test_bad_consumed_at_is_a_400(client, wire):
    wire(meal=meal())
    resp = client.post("/recognize", json={"text": "гречка", "consumed_at": "вчера"})
    assert resp.status_code == 400


# --- дырки и отказы ----------------------------------------------------------


def test_unresolved_item_is_flagged_not_zeroed_silently(client, wire):
    """Штуки в граммы не переводятся — позиция уходит в ручной ввод."""
    wire(
        meal=meal(
            items=[
                rec.RecognizedItemOut(
                    name="сырник",
                    quantity=2,
                    unit="шт",
                    kind="generic",
                    lookup_query="cheese pancake",
                    brand=None,
                )
            ]
        )
    )
    body = client.post("/recognize", json={"text": "два сырника"}).json()

    assert body["needs_manual"] is True
    [item] = body["draft"]["items"]
    assert item["needs_manual"] is True
    assert "шт" in item["source_ref"]


def test_partial_failure_is_visible_at_the_top_level(client, wire):
    """Одна нерешённая позиция из двух — флаг на весь черновик."""
    wire(
        meal=meal(
            items=[
                rec.RecognizedItemOut(
                    name="гречка",
                    quantity=180,
                    unit="g",
                    kind="generic",
                    lookup_query="buckwheat",
                    brand=None,
                ),
                rec.RecognizedItemOut(
                    name="сырник",
                    quantity=2,
                    unit="шт",
                    kind="generic",
                    lookup_query="cheese pancake",
                    brand=None,
                ),
            ]
        )
    )
    body = client.post("/recognize", json={"text": "гречка и два сырника"}).json()

    assert body["needs_manual"] is True
    assert [i["needs_manual"] for i in body["draft"]["items"]] == [False, True]


def test_model_failure_is_a_502_not_a_500(client, wire):
    """Отказал апстрим, а не мы: клиенту стоит повторить."""
    wire(error=rec.RecognizeError("все провайдеры отказали"))
    resp = client.post("/recognize", json={"text": "гречка"})
    assert resp.status_code == 502
    assert "все провайдеры отказали" in resp.json()["detail"]


def test_misconfigured_provider_is_a_503_and_does_not_break_logs(client, api, monkeypatch):
    """Кривой RECOGNIZE_PROVIDER не должен ронять остальной API."""
    monkeypatch.setenv("RECOGNIZE_PROVIDER", "нетакого")
    api.app.state.recognizer = None

    assert client.post("/recognize", json={"text": "гречка"}).status_code == 503
    assert client.get("/day").status_code == 200, "остальные эндпоинты должны работать"


def test_provider_name_is_reported(client, wire):
    wire(meal=meal())
    assert client.post("/recognize", json={"text": "гречка"}).json()["provider"] == "fake"


# --- ресурсы -----------------------------------------------------------------


def test_recognizer_is_closed_on_shutdown(api):
    from fastapi.testclient import TestClient

    fake = FakeRecognizer(meal())
    with TestClient(api.app) as c:
        api.app.state.recognizer = fake
        c.get("/health")
    assert fake.closed is True, "распознаватель должен закрываться вместе с приложением"
