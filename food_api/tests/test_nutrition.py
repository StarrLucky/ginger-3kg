"""Шаг 2: питательность из справочников.

Сеть замокана фикстурой no_network; здесь мы подменяем сам httpx-клиент,
поэтому проверяем логику приоритетов и арифметику, а не доступность USDA.
"""

import asyncio
import json

import httpx
import nutrition as nut
import pytest


@pytest.fixture
def conn(api):
    c = api._get_db()
    c.execute(nut.CREATE_CACHE_TABLE)
    c.commit()
    yield c
    c.close()


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# Типичные числа модели на 100 г отварной гречки.
MODEL_100G = {
    "calories_kcal": 92.0,
    "protein_g": 3.4,
    "fat_total_g": 0.6,
    "carbs_g": 19.9,
}


def item(**kw) -> nut.RecognizedItem:
    """По умолчанию БЕЗ чисел модели: так виден путь, где их неоткуда взять."""
    return nut.RecognizedItem(
        **{"name": "гречка", "quantity": 100, "unit": "g", "lookup_query": "buckwheat", **kw}
    )


# --- единицы и масштабирование -------------------------------------------


@pytest.mark.parametrize(
    ("qty", "unit", "grams"),
    [(100, "g", 100), (100, "г", 100), (1.5, "кг", 1500), (250, "мл", 250), (0.5, "l", 500)],
)
def test_units_convert_to_grams(qty, unit, grams):
    assert nut.to_grams(qty, unit) == grams


def test_pieces_are_not_guessed():
    """Штуки не угадываем: вес сырника разнится в разы."""
    assert nut.to_grams(2, "шт") is None
    assert nut.to_grams(1, "piece") is None


def test_scaling_is_linear_from_per_100g():
    scaled = nut.scale_per_100g({"calories_kcal": 343.0, "protein_g": 13.0}, 180)
    assert scaled["calories_kcal"] == pytest.approx(617.4)
    assert scaled["protein_g"] == pytest.approx(23.4)


def test_scaling_skips_missing_values():
    """None не должен превращаться в 0 — это разные утверждения."""
    assert "fiber_g" not in nut.scale_per_100g({"calories_kcal": 100, "fiber_g": None}, 50)


# --- приоритет источников -------------------------------------------------


async def test_override_wins_over_external_lookup(conn, tmp_path):
    path = tmp_path / "o.json"
    path.write_text(
        json.dumps({"_comment": "игнор", "гречка": {"calories_kcal": 999, "protein_g": 50}}),
        encoding="utf-8",
    )

    def boom(request):
        raise AssertionError("при наличии override во внешний справочник ходить не надо")

    async with mock_client(boom) as client:
        [res] = await nut.resolve(
            [item(quantity=200)],
            conn,
            overrides=nut.load_overrides(path),
            client=client,
        )
    assert res.source_ref == "override"
    assert res.nutrients["calories_kcal"] == pytest.approx(1998)


def test_load_overrides_skips_comment_keys(tmp_path):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"_comment": ["x"], "Творог 5%": {"calories_kcal": 121}}), "utf-8")
    loaded = nut.load_overrides(path)
    assert set(loaded) == {"творог 5%"}, "служебные ключи и регистр"


def test_shipped_overrides_file_is_valid():
    """Файл в репозитории должен грузиться и содержать только числа."""
    from pathlib import Path

    loaded = nut.load_overrides(Path(__file__).parent.parent / "overrides.json")
    assert loaded, "переопределения не загрузились"
    for name, values in loaded.items():
        assert values.get("calories_kcal") is not None, f"{name} без калорий"
        for key, value in values.items():
            assert key in nut.NUTRIENT_KEYS, f"{name}: неизвестная колонка {key}"
            assert isinstance(value, int | float), f"{name}.{key} не число"


async def test_unknown_unit_goes_to_manual_without_network(conn):
    def boom(request):
        raise AssertionError("не надо ходить в сеть, если единицу не перевести")

    async with mock_client(boom) as client:
        [res] = await nut.resolve([item(quantity=2, unit="шт")], conn, overrides={}, client=client)
    assert res.needs_manual is True
    assert res.nutrients == {}
    assert "шт" in res.source_ref


# --- USDA: базовая линия для замера Фазы 3, не рабочий путь -------------------
#
# С 2026-10-01 числа в рабочем пути даёт модель: на живых данных поиск по USDA
# сопоставлял «кофе с молоком» с шоколадными конфетами (1098 ккал вместо ~45).
# Функция и её тесты оставлены намеренно — в них накоплено знание о квирках
# API (энергия в kJ под другим id, неполные карточки Foundation, порядок
# SR Legacy), и Фаза 3 будет мерить её против модели.


def usda_handler(search: dict, details: dict[int, dict]):
    """Мок USDA: поиск + детальные карточки по fdcId."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "foods/search" in request.url.path:
            return httpx.Response(200, json=search)
        fdc_id = int(request.url.path.rsplit("/", 1)[-1])
        if fdc_id not in details:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=details[fdc_id])

    handle.calls = calls
    return handle


def usda_food(fdc_id: int, data_type: str, description: str = "еда"):
    return {"fdcId": fdc_id, "dataType": data_type, "description": description}


def usda_detail(description: str, nutrients: dict[int, tuple[str, float]]):
    return {
        "description": description,
        "foodNutrients": [
            {"nutrient": {"id": nid, "unitName": unit}, "amount": amount}
            for nid, (unit, amount) in nutrients.items()
        ],
    }


FULL_PANEL = {
    1008: ("kcal", 343.0),
    1003: ("g", 13.25),
    1004: ("g", 3.4),
    1005: ("g", 71.5),
    1258: ("g", 0.74),
    1079: ("g", 10.0),
    2000: ("g", 0.0),
    1093: ("mg", 1.0),
}


async def test_usda_maps_nutrients():
    handler = usda_handler(
        {"foods": [usda_food(170286, "SR Legacy", "Buckwheat")]},
        {170286: usda_detail("Buckwheat", FULL_PANEL)},
    )
    async with mock_client(handler) as client:
        per_100g, ref = await nut.usda_lookup(client, "buckwheat", "k")

    assert ref.startswith("USDA 170286")
    assert per_100g["calories_kcal"] == pytest.approx(343.0)
    assert per_100g["sodium_mg"] == pytest.approx(1.0)


async def test_usda_key_goes_in_header_not_url():
    """Ключ в query утёк бы в текст HTTPStatusError, а оттуда в лог."""
    handler = usda_handler(
        {"foods": [usda_food(1, "SR Legacy")]}, {1: usda_detail("x", FULL_PANEL)}
    )
    async with mock_client(handler) as client:
        await nut.usda_lookup(client, "buckwheat", "zzz-no-key")

    assert handler.calls, "до USDA дело не дошло"
    for url in handler.calls:
        assert "zzz-no-key" not in url, f"ключ в URL: {url}"
        assert "api_key" not in url, f"параметр api_key остался в URL: {url}"


async def test_usda_skips_candidate_without_calories():
    """Найдено на живом API: у Foundation-записей энергии часто нет."""
    handler = usda_handler(
        {
            "foods": [
                usda_food(1, "Foundation", "Chicken, raw"),
                usda_food(2, "SR Legacy", "Chicken, broilers"),
            ]
        },
        {
            1: usda_detail("Chicken, raw", {1003: ("g", 22.5)}),  # без 1008
            2: usda_detail("Chicken, broilers", {1008: ("kcal", 172.0)}),
        },
    )
    async with mock_client(handler) as client:
        per_100g, ref = await nut.usda_lookup(client, "chicken breast", "k")

    assert ref.startswith("USDA 2"), "должна победить полная карточка"
    assert per_100g["calories_kcal"] == pytest.approx(172.0)


async def test_usda_prefers_sr_legacy_ordering():
    """SR Legacy запрашивается первым: его панели заполнены полнее."""
    handler = usda_handler(
        {"foods": [usda_food(1, "Foundation"), usda_food(2, "SR Legacy")]},
        {1: usda_detail("foundation", FULL_PANEL), 2: usda_detail("sr legacy", FULL_PANEL)},
    )
    async with mock_client(handler) as client:
        await nut.usda_lookup(client, "buckwheat", "k")

    detail_calls = [c for c in handler.calls if "/food/" in c]
    assert "/food/2" in detail_calls[0], "SR Legacy должен опрашиваться раньше Foundation"


async def test_kj_energy_is_not_mistaken_for_kcal():
    """1062 приходит в kJ — принять его за калории значит завысить вчетверо."""
    handler = usda_handler(
        {"foods": [usda_food(9, "SR Legacy")]},
        {9: usda_detail("x", {1062: ("kJ", 720.0), 1003: ("g", 20.0)})},
    )
    async with mock_client(handler) as client:
        assert await nut.usda_lookup(client, "x", "k") is None, "карточка без kcal неполна"


# --- временные отказы справочника --------------------------------------------


def flaky(statuses: list[int], ok_payload: dict):
    """Отдаёт заданные коды по очереди, затем всегда 200 с payload."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= len(statuses):
            return httpx.Response(statuses[len(calls) - 1], json={})
        return httpx.Response(200, json=ok_payload)

    handle.calls = calls
    return handle


async def test_transient_503_is_retried(conn):
    """OFF отдаёт 503 заметной долей запросов (замерено на живом API).

    Без повтора позиция уходила в needs_manual с нулями, хотя следующая
    попытка проходит, и человек заполнял руками то, что справочник знает.
    """
    payload = {
        "products": [
            {"product_name": "Pudding", "brands": "Ehrmann", "nutriments": {"energy-kcal_100g": 78}}
        ]
    }
    handler = flaky([503, 503], payload)

    async with mock_client(handler) as client:
        [res] = await nut.resolve(
            [item(kind="branded", quantity=100)],
            conn,
            overrides={},
            client=client,
        )

    assert len(handler.calls) == 3, "две неудачи и успех"
    assert res.needs_manual is False
    assert res.nutrients["calories_kcal"] == pytest.approx(78)


async def test_429_is_retried_too(conn):
    payload = {"products": [{"product_name": "X", "nutriments": {"energy-kcal_100g": 50}}]}
    handler = flaky([429], payload)
    async with mock_client(handler) as client:
        [res] = await nut.resolve([item(kind="branded")], conn, overrides={}, client=client)
    assert len(handler.calls) == 2
    assert res.needs_manual is False


async def test_404_is_not_retried(conn):
    """4xx — это наш запрос, повтор его не исправит, только задержит ответ."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404, json={})

    async with mock_client(handle) as client:
        [res] = await nut.resolve([item(kind="branded")], conn, overrides={}, client=client)
    assert len(calls) == 1, f"404 повторяться не должен, запросов: {len(calls)}"
    assert res.needs_manual is True


async def test_connection_error_is_retried_then_succeeds(conn):
    attempts = []
    payload = {"products": [{"product_name": "X", "nutriments": {"energy-kcal_100g": 50}}]}

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("сеть моргнула")
        return httpx.Response(200, json=payload)

    async with mock_client(handle) as client:
        [res] = await nut.resolve([item(kind="branded")], conn, overrides={}, client=client)
    assert len(attempts) == 2
    assert res.needs_manual is False


async def test_persistent_failure_still_gives_up(conn):
    """Повтор не должен превращаться в бесконечное ожидание."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={})

    async with mock_client(handle) as client:
        [res] = await nut.resolve([item(kind="branded")], conn, overrides={}, client=client)
    assert len(calls) == nut.LOOKUP_RETRIES + 1, "первая попытка плюс повторы, не больше"
    assert res.needs_manual is True


# --- кэш по lookup_query -----------------------------------------------------


async def test_model_numbers_are_cached_by_lookup_query(conn):
    """Одна еда считается один раз в жизни, дальше — из кэша.

    Кэш здесь не только про скорость: модель может сегодня сказать 126, завтра
    131, и сравнение дней станет шумным. Из кэша число всегда одно и то же.
    """

    def boom(request):
        raise AssertionError("числа у модели уже есть, в сеть ходить незачем")

    async with mock_client(boom) as client:
        [first] = await nut.resolve(
            [item(quantity=180, per_100g=dict(MODEL_100G))],
            conn,
            overrides={},
            client=client,
        )
        # Второй раз модель чисел не даёт вовсе — берём из кэша по тому же ключу
        [second] = await nut.resolve(
            [item(quantity=90, per_100g=None)],
            conn,
            overrides={},
            client=client,
        )

    assert first.nutrients["calories_kcal"] == pytest.approx(165.6)  # 92 * 1.8
    assert second.nutrients["calories_kcal"] == pytest.approx(82.8)  # 92 * 0.9
    assert second.source_ref == first.source_ref, "происхождение должно сохраниться"


async def test_cache_key_is_the_english_query_not_the_name(conn):
    """Иначе одна и та же еда, названная по-разному, считалась бы заново."""
    async with mock_client(lambda r: httpx.Response(500, json={})) as client:
        await nut.resolve(
            [
                item(
                    name="гречка",
                    lookup_query="buckwheat groats, cooked",
                    per_100g=dict(MODEL_100G),
                )
            ],
            conn,
            overrides={},
            client=client,
        )
        [again] = await nut.resolve(
            [item(name="гречневая каша", lookup_query="buckwheat groats, cooked", per_100g=None)],
            conn,
            overrides={},
            client=client,
        )
    assert again.needs_manual is False
    assert again.nutrients["calories_kcal"] == pytest.approx(92.0)


async def test_items_are_resolved_in_parallel(conn):
    """Брендовые позиции ходят в OFF и не должны ждать друг друга."""
    inflight = 0
    peak = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return httpx.Response(200, json={"products": []})

    items = [
        item(name=name, lookup_query=name, kind="branded", brand=name, per_100g=dict(MODEL_100G))
        for name in ("pudding", "yogurt", "bar", "shake")
    ]
    async with mock_client(handle) as client:
        await nut.resolve(items, conn, overrides={}, client=client)

    assert peak >= 2, f"запросы шли по одному (пик {peak}) — resolve стал последовательным"


# --- порядок источников ------------------------------------------------------


async def test_model_numbers_are_used_for_generic_food(conn):
    """Generic больше не ходит в USDA: там «кофе с молоком» становился конфетами."""

    def boom(request):
        raise AssertionError("generic-еда в сеть ходить не должна")

    async with mock_client(boom) as client:
        [res] = await nut.resolve(
            [item(quantity=180, per_100g=dict(MODEL_100G))],
            conn,
            overrides={},
            client=client,
        )

    assert res.needs_manual is False
    assert res.nutrients["calories_kcal"] == pytest.approx(165.6)
    assert res.source_ref.startswith("model:"), res.source_ref
    assert "buckwheat" in res.source_ref, "видно, за что именно взяты числа"


async def test_overrides_still_beat_the_model(conn, tmp_path):
    """Правка человека — последнее слово, она и вводилась ради этого."""
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"гречка": {"calories_kcal": 999}}), encoding="utf-8")

    async with mock_client(lambda r: httpx.Response(500, json={})) as client:
        [res] = await nut.resolve(
            [item(quantity=100, per_100g=dict(MODEL_100G))],
            conn,
            overrides=nut.load_overrides(path),
            client=client,
        )
    assert res.source_ref == "override"
    assert res.nutrients["calories_kcal"] == pytest.approx(999)


async def test_branded_prefers_off_over_the_model(conn):
    """У OFF числа с этикетки конкретного продукта — модель их знать не может."""
    payload = {
        "products": [
            {
                "product_name": "High Protein Pudding",
                "brands": "Ehrmann",
                "nutriments": {"energy-kcal_100g": 78},
            }
        ]
    }
    async with mock_client(lambda r: httpx.Response(200, json=payload)) as client:
        [res] = await nut.resolve(
            [item(kind="branded", brand="Ehrmann", quantity=100, per_100g=dict(MODEL_100G))],
            conn,
            overrides={},
            client=client,
        )
    assert res.source_ref.startswith("OFF:")
    assert res.nutrients["calories_kcal"] == pytest.approx(78)


async def test_branded_falls_back_to_the_model_when_off_is_down(conn):
    """OFF отдаёт 503 заметной долей запросов — это не повод терять позицию."""
    async with mock_client(lambda r: httpx.Response(503, json={})) as client:
        [res] = await nut.resolve(
            [item(kind="branded", brand="Ehrmann", quantity=100, per_100g=dict(MODEL_100G))],
            conn,
            overrides={},
            client=client,
        )
    assert res.needs_manual is False
    assert res.source_ref.startswith("model:")
    assert res.nutrients["calories_kcal"] == pytest.approx(92.0)


async def test_without_numbers_anywhere_the_hole_stays_honest(conn):
    """Ни кэша, ни OFF, ни оценки модели — нули под видом данных не подставляем."""
    async with mock_client(lambda r: httpx.Response(503, json={})) as client:
        [res] = await nut.resolve(
            [item(kind="branded", brand="X", per_100g=None)],
            conn,
            overrides={},
            client=client,
        )
    assert res.needs_manual is True
    assert res.nutrients == {}


# --- Open Food Facts -------------------------------------------------------


async def test_branded_goes_to_off_with_sodium_scaling(conn):
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
            }
        ]
    }

    def handle(request):
        assert "openfoodfacts" in request.url.host, "брендовое не должно идти в USDA"
        return httpx.Response(200, json=payload)

    async with mock_client(handle) as client:
        [res] = await nut.resolve(
            [
                item(
                    name="пудинг",
                    kind="branded",
                    brand="Ehrmann",
                    quantity=200,
                    lookup_query="high protein pudding",
                )
            ],
            conn,
            overrides={},
            client=client,
        )

    assert res.source_ref == "OFF: Ehrmann High Protein Pudding"
    assert res.nutrients["calories_kcal"] == pytest.approx(156.0)  # 78 * 2
    assert res.nutrients["sodium_mg"] == pytest.approx(160.0)  # 0.08 г -> 80 мг на 100 г, ×2


async def test_off_is_searched_in_english_not_the_user_language(conn):
    """Проверено на живом OFF: «Ehrmann протеиновый пудинг» не находит ничего,
    «Ehrmann High Protein Pudding» находит товар. Промпт просит английский
    lookup_query ровно для этого — брать name здесь значит его выбросить."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("search_terms", ""))
        return httpx.Response(
            200, json={"products": [{"product_name": "X", "nutriments": {"energy-kcal_100g": 70}}]}
        )

    async with mock_client(handle) as client:
        await nut.resolve(
            [
                item(
                    name="протеиновый пудинг",
                    kind="branded",
                    brand="Ehrmann",
                    lookup_query="High Protein Pudding",
                )
            ],
            conn,
            overrides={},
            client=client,
        )

    assert seen == ["Ehrmann High Protein Pudding"], seen
    assert "протеиновый" not in seen[0], "русское название в запрос попадать не должно"


async def test_brand_is_not_duplicated_when_already_in_the_query(conn):
    """Модель часто включает бренд сама; «Ehrmann Ehrmann ...» ищется хуже."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("search_terms", ""))
        return httpx.Response(
            200, json={"products": [{"product_name": "X", "nutriments": {"energy-kcal_100g": 70}}]}
        )

    async with mock_client(handle) as client:
        await nut.resolve(
            [
                item(
                    name="пудинг",
                    kind="branded",
                    brand="Ehrmann",
                    lookup_query="Ehrmann High Protein Pudding",
                )
            ],
            conn,
            overrides={},
            client=client,
        )
    assert seen == ["Ehrmann High Protein Pudding"], seen


async def test_branded_without_lookup_query_falls_back_to_the_name(conn):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("search_terms", ""))
        return httpx.Response(
            200, json={"products": [{"product_name": "X", "nutriments": {"energy-kcal_100g": 70}}]}
        )

    async with mock_client(handle) as client:
        await nut.resolve(
            [item(name="пудинг", kind="branded", brand="Ehrmann", lookup_query="")],
            conn,
            overrides={},
            client=client,
        )
    assert seen == ["Ehrmann пудинг"], seen


async def test_off_skips_products_without_calories(conn):
    payload = {
        "products": [
            {"product_name": "пустышка", "nutriments": {}},
            {"product_name": "нормальный", "brands": "X", "nutriments": {"energy-kcal_100g": 50}},
        ]
    }
    async with mock_client(lambda r: httpx.Response(200, json=payload)) as client:
        [res] = await nut.resolve(
            [item(kind="branded", quantity=100)],
            conn,
            overrides={},
            client=client,
        )
    assert res.source_ref == "OFF: X нормальный"
    assert res.nutrients["calories_kcal"] == pytest.approx(50.0)
