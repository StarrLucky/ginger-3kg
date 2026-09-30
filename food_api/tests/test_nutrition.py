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


def item(**kw) -> nut.RecognizedItem:
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
            usda_api_key="k",
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
        [res] = await nut.resolve(
            [item(quantity=2, unit="шт")], conn, overrides={}, usda_api_key="k", client=client
        )
    assert res.needs_manual is True
    assert res.nutrients == {}
    assert "шт" in res.source_ref


# --- USDA ------------------------------------------------------------------


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


async def test_usda_lookup_maps_nutrients_and_scales(conn):
    handler = usda_handler(
        {"foods": [usda_food(170286, "SR Legacy", "Buckwheat")]},
        {170286: usda_detail("Buckwheat", FULL_PANEL)},
    )
    async with mock_client(handler) as client:
        [res] = await nut.resolve(
            [item(quantity=180)], conn, overrides={}, usda_api_key="k", client=client
        )

    assert res.needs_manual is False
    assert res.source_ref.startswith("USDA 170286")
    assert res.nutrients["calories_kcal"] == pytest.approx(617.4)  # 343 * 1.8
    assert res.nutrients["protein_g"] == pytest.approx(23.85)
    assert res.nutrients["sodium_mg"] == pytest.approx(1.8)


async def test_usda_key_goes_in_header_not_url(conn):
    """Ключ в query утёк бы в текст HTTPStatusError, а оттуда в лог и source_ref."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("x-api-key")))
        if "foods/search" in request.url.path:
            return httpx.Response(200, json={"foods": [usda_food(1, "SR Legacy")]})
        return httpx.Response(200, json=usda_detail("x", FULL_PANEL))

    async with mock_client(handle) as client:
        await nut.resolve([item()], conn, overrides={}, usda_api_key="zzz-no-key", client=client)

    assert seen, "до USDA дело не дошло"
    for url, header in seen:
        assert "zzz-no-key" not in url, f"ключ в URL: {url}"
        assert "api_key" not in url, f"параметр api_key остался в URL: {url}"
        assert header == "zzz-no-key", "ключ не доехал заголовком"


async def test_usda_skips_candidate_without_calories(conn):
    """Найдено на живом API: у Foundation-записей энергии часто нет.

    Такой кандидат должен пропускаться, а не приводить к позиции без калорий.
    """
    handler = usda_handler(
        {
            "foods": [
                usda_food(1, "Foundation", "Chicken, raw"),
                usda_food(2, "SR Legacy", "Chicken, broilers"),
            ]
        },
        {
            1: usda_detail("Chicken, raw", {1003: ("g", 22.5), 1004: ("g", 1.9)}),  # без 1008
            2: usda_detail("Chicken, broilers", {1008: ("kcal", 172.0), 1003: ("g", 20.85)}),
        },
    )
    async with mock_client(handler) as client:
        [res] = await nut.resolve(
            [item(name="курица", lookup_query="chicken breast", quantity=100)],
            conn,
            overrides={},
            usda_api_key="k",
            client=client,
        )

    assert res.source_ref.startswith("USDA 2"), "должна победить полная карточка"
    assert res.nutrients["calories_kcal"] == pytest.approx(172.0)


async def test_usda_prefers_sr_legacy_ordering(conn):
    """SR Legacy запрашивается первым: его панели заполнены полнее."""
    handler = usda_handler(
        {"foods": [usda_food(1, "Foundation"), usda_food(2, "SR Legacy")]},
        {
            1: usda_detail("foundation", FULL_PANEL),
            2: usda_detail("sr legacy", FULL_PANEL),
        },
    )
    async with mock_client(handler) as client:
        await nut.resolve([item()], conn, overrides={}, usda_api_key="k", client=client)

    detail_calls = [c for c in handler.calls if "/food/" in c]
    assert "/food/2" in detail_calls[0], "SR Legacy должен опрашиваться раньше Foundation"


async def test_kj_energy_is_not_mistaken_for_kcal(conn):
    """1062 приходит в kJ — принять его за калории значит завысить в 4 раза."""
    handler = usda_handler(
        {"foods": [usda_food(9, "SR Legacy")]},
        {9: usda_detail("x", {1062: ("kJ", 720.0), 1003: ("g", 20.0)})},
    )
    async with mock_client(handler) as client:
        [res] = await nut.resolve([item()], conn, overrides={}, usda_api_key="k", client=client)
    assert res.needs_manual is True, "карточка без kcal считается неполной"


async def test_nothing_found_is_marked_manual_not_zeroed(conn):
    handler = usda_handler({"foods": []}, {})
    async with mock_client(handler) as client:
        [res] = await nut.resolve([item()], conn, overrides={}, usda_api_key="k", client=client)

    assert res.needs_manual is True
    assert res.nutrients == {}, "дырка честная, а не нули под видом данных"


async def test_upstream_failure_is_marked_manual(conn):
    def explode(request):
        raise httpx.ConnectError("нет сети")

    async with mock_client(explode) as client:
        [res] = await nut.resolve([item()], conn, overrides={}, usda_api_key="k", client=client)
    assert res.needs_manual is True
    assert "недоступен" in res.source_ref


# --- кэш -------------------------------------------------------------------


async def test_second_lookup_hits_cache(conn):
    handler = usda_handler(
        {"foods": [usda_food(170286, "SR Legacy", "Buckwheat")]},
        {170286: usda_detail("Buckwheat", FULL_PANEL)},
    )
    async with mock_client(handler) as client:
        await nut.resolve([item()], conn, overrides={}, usda_api_key="k", client=client)
        first = len(handler.calls)
        [res] = await nut.resolve(
            [item(quantity=50)], conn, overrides={}, usda_api_key="k", client=client
        )

    assert len(handler.calls) == first, "второй раз в сеть ходить не надо"
    assert res.nutrients["calories_kcal"] == pytest.approx(171.5)  # 343 * 0.5
    assert res.source_ref.startswith("USDA 170286")


async def test_items_are_resolved_in_parallel(conn):
    """Позиции в одном приёме пищи не должны ждать друг друга.

    Четыре новых продукта — это восемь запросов к справочнику. Последовательно
    они складываются в секунды поверх и без того долгого вызова модели.
    """
    inflight = 0
    peak = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return httpx.Response(200, json={"foods": []})

    items = [
        item(name=name, lookup_query=name) for name in ("buckwheat", "chicken", "salad", "bread")
    ]
    async with mock_client(handle) as client:
        await nut.resolve(items, conn, overrides={}, usda_api_key="k", client=client)

    assert peak >= 2, f"запросы шли по одному (пик {peak}) — resolve стал последовательным"


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
            [item(name="пудинг", kind="branded", brand="Ehrmann", quantity=200)],
            conn,
            overrides={},
            usda_api_key="k",
            client=client,
        )

    assert res.source_ref == "OFF: Ehrmann High Protein Pudding"
    assert res.nutrients["calories_kcal"] == pytest.approx(156.0)  # 78 * 2
    assert res.nutrients["sodium_mg"] == pytest.approx(160.0)  # 0.08 г -> 80 мг на 100 г, ×2


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
            usda_api_key="k",
            client=client,
        )
    assert res.source_ref == "OFF: X нормальный"
    assert res.nutrients["calories_kcal"] == pytest.approx(50.0)
