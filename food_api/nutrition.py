"""Шаг 2 конвейера: собрать питательность позиции и отмасштабировать на порцию.

Приоритет источников:

1. `overrides.json` — правка человека, последнее слово;
2. кэш — для брендового по запросу с брендом, для остального по английскому
   `lookup_query`;
3. Open Food Facts — только брендовое: там числа с этикетки конкретного
   продукта, чего модель знать не может;
4. `per_100g` от модели — основной источник для generic-еды с 2026-10-01
   (обоснование разворота в WEBAPP.md §1.3++);
5. `needs_manual` — ненайденное помечается честно, а не заполняется нулями
   под видом данных.

USDA из рабочего пути убран: поиск сопоставлял «кофе с молоком» с шоколадными
конфетами. `usda_lookup()` оставлена как базовая линия для замера Фазы 3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

NUTRIENT_KEYS = [
    "calories_kcal",
    "protein_g",
    "fat_total_g",
    "fat_saturated_g",
    "carbs_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
    "caffeine_mg",
]

USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
USDA_DETAIL_URL = "https://api.nal.usda.gov/fdc/v1/food/{fdc_id}"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
USER_AGENT = "MeowFood/1.0 (personal food tracker)"

# Open Food Facts отдаёт 503 «Service Temporarily Unavailable» заметной долей
# запросов — замерено 2026-10-01: от одного до двух из четырёх подряд. Без
# повтора позиция уходит в needs_manual с нулями, хотя следующая попытка
# обычно проходит, и человек заполняет руками то, что справочник знает.
LOOKUP_RETRIES = 2
# Два источника в кэше, а не один. Объединять их нельзя: брендовый пудинг
# Ehrmann и обобщённый «high protein pudding» — разная еда с разными числами,
# и под общим ключом второй бренд получал бы этикетку первого.
SOURCE_REFERENCE = "off"  # ключ включает бренд
SOURCE_MODEL = "model"  # ключ — английский lookup_query

log = logging.getLogger(__name__)
LOOKUP_BACKOFF = 0.5

# nutrientId -> наша колонка. Проверено на живом ответе USDA (SR Legacy 171474).
# Энергия приходит дважды: 1008 в kcal и 1062 в kJ — берём только kcal.
# Сахар в части записей лежит под 1063 (Sugars, Total NLEA) вместо 2000.
USDA_NUTRIENT_IDS: dict[int, str] = {
    1008: "calories_kcal",
    1003: "protein_g",
    1004: "fat_total_g",
    1258: "fat_saturated_g",
    1005: "carbs_g",
    1079: "fiber_g",
    2000: "sugar_g",
    1063: "sugar_g",
    1093: "sodium_mg",
    1057: "caffeine_mg",
}

# Единицы, которые умеем переводить в граммы. Для жидкостей берём плотность 1:
# для молока/кефира/сока погрешность меньше, чем у оценки порции по фото.
_GRAMS_PER_UNIT: dict[str, float] = {
    "g": 1.0,
    "г": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "грамм": 1.0,
    "гр": 1.0,
    "kg": 1000.0,
    "кг": 1000.0,
    "ml": 1.0,
    "мл": 1.0,
    "l": 1000.0,
    "л": 1000.0,
}


@dataclass
class RecognizedItem:
    """То, что вернула модель: еда, количество и питательность на 100 г."""

    name: str
    quantity: float
    unit: str
    kind: Literal["branded", "generic"] = "generic"
    lookup_query: str = ""
    brand: str | None = None
    # На 100 г, не на порцию: это свойство еды, его можно кэшировать и
    # масштабировать. None — модель числа не дала (старый вызов или сбой).
    per_100g: dict[str, float] | None = None


@dataclass
class ResolvedItem:
    """Позиция с числами и указанием, откуда они взялись."""

    name: str
    quantity: float
    unit: str
    nutrients: dict[str, float] = field(default_factory=dict)
    source_ref: str = "manual"
    needs_manual: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Форма, которую принимает POST /logs (FoodItemIn + служебные поля)."""
        payload: dict[str, Any] = {
            "name": self.name,
            "quantity": self.quantity,
            "unit": self.unit,
            "source_ref": self.source_ref,
            "needs_manual": self.needs_manual,
        }
        for key in NUTRIENT_KEYS:
            payload[key] = round(self.nutrients.get(key, 0.0), 1)
        return payload


def to_grams(quantity: float, unit: str) -> float | None:
    """Перевод в граммы. None — единица неизвестна (например «шт»).

    Штуки намеренно не угадываем: вес сырника или яблока разнится в разы,
    и подстановка среднего была бы тем же выдумыванием числа, от которого
    мы уходим. Такая позиция уходит в needs_manual.
    """
    factor = _GRAMS_PER_UNIT.get(unit.strip().lower())
    return quantity * factor if factor is not None else None


def scale_per_100g(per_100g: dict[str, float | None], grams: float) -> dict[str, float]:
    """per_100g -> абсолютные значения на съеденное количество."""
    k = grams / 100.0
    return {key: value * k for key, value in per_100g.items() if isinstance(value, int | float)}


# --------------------------------------------------------------------------
# Кэш
# --------------------------------------------------------------------------

CREATE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS nutrition_cache (
    source        TEXT NOT NULL,
    query         TEXT NOT NULL,
    per_100g_json TEXT NOT NULL,
    source_ref    TEXT NOT NULL,
    fetched_at    REAL NOT NULL,
    PRIMARY KEY (source, query)
)
"""


def cache_get(conn: sqlite3.Connection, source: str, query: str) -> tuple[dict, str] | None:
    row = conn.execute(
        "SELECT per_100g_json, source_ref FROM nutrition_cache WHERE source = ? AND query = ?",
        (source, query.lower()),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["per_100g_json"]), row["source_ref"]


def cache_put(
    conn: sqlite3.Connection, source: str, query: str, per_100g: dict, source_ref: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO nutrition_cache"
        " (source, query, per_100g_json, source_ref, fetched_at) VALUES (?, ?, ?, ?, ?)",
        (source, query.lower(), json.dumps(per_100g), source_ref, time.time()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Источники
# --------------------------------------------------------------------------


def load_overrides(path: str | Path) -> dict[str, dict]:
    """Локальная таблица для того, чего в USDA нет или оно названо иначе."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    # ключи с подчёркиванием — комментарии в самом файле, не продукты
    return {k.lower(): v for k, v in raw.items() if not k.startswith("_")}


async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    """GET с повтором на временных отказах справочника.

    Повторяем только то, что имеет шанс измениться само: 5xx, 429 и обрывы
    связи. 4xx — это наш неверный запрос или ключ, и повтор его не исправит,
    только задержит ответ пользователю.
    """
    delay = LOOKUP_BACKOFF
    last_error: Exception | None = None

    for attempt in range(LOOKUP_RETRIES + 1):
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.TransportError as err:  # таймауты сюда же, они подкласс
            last_error = err
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            last_error = None
            if attempt == LOOKUP_RETRIES:
                return resp  # пусть решает вызывающий: raise_for_status или пропуск

        if attempt == LOOKUP_RETRIES:
            break
        await asyncio.sleep(delay)
        delay *= 2

    assert last_error is not None
    raise last_error


def _usda_pick_nutrients(detail: dict) -> dict[str, float]:
    """foodNutrients детального ответа -> наши колонки (значения на 100 г)."""
    out: dict[str, float] = {}
    for entry in detail.get("foodNutrients", []):
        nutrient = entry.get("nutrient") or {}
        key = USDA_NUTRIENT_IDS.get(nutrient.get("id"))
        amount = entry.get("amount")
        if key is None or not isinstance(amount, int | float):
            continue
        # 1008 приходит в kcal, 1062 в kJ — вторая в маппинг не попала,
        # но подстрахуемся на случай расхождений в данных
        if key == "calories_kcal" and (nutrient.get("unitName") or "").lower() != "kcal":
            continue
        out.setdefault(key, float(amount))
    return out


async def usda_lookup(
    client: httpx.AsyncClient, query: str, api_key: str
) -> tuple[dict, str] | None:
    """Поиск generic-еды в USDA FoodData Central.

    Поиск отдаёт урезанный список нутриентов (у Foundation-записей там нет
    даже калорий), поэтому по каждому кандидату дозапрашиваем детальную
    карточку и берём первого, у кого калории реально есть. SR Legacy идёт
    первым: его панели заполнены полнее, чем у Foundation.

    Ключ идёт заголовком, а не query-параметром: httpx вшивает полный URL в
    текст `HTTPStatusError`, а этот текст попадает в лог и в source_ref
    («справочник недоступен»). Ключ в параметрах утёк бы туда при первом же
    сбое USDA. api.data.gov принимает X-Api-Key наравне с ?api_key=.
    """
    headers = {"X-Api-Key": api_key}
    resp = await _get(
        client,
        USDA_SEARCH_URL,
        params={"query": query, "dataType": "Foundation,SR Legacy", "pageSize": 5},
        headers=headers,
    )
    resp.raise_for_status()
    foods = resp.json().get("foods", [])
    foods.sort(key=lambda f: 0 if f.get("dataType") == "SR Legacy" else 1)

    for food in foods:
        fdc_id = food.get("fdcId")
        if not fdc_id:
            continue
        detail_resp = await _get(client, USDA_DETAIL_URL.format(fdc_id=fdc_id), headers=headers)
        if detail_resp.status_code != 200:
            continue
        detail = detail_resp.json()
        per_100g = _usda_pick_nutrients(detail)
        if "calories_kcal" not in per_100g:
            continue  # неполная карточка — пробуем следующего кандидата
        return per_100g, f"USDA {fdc_id} ({detail.get('description', '')})".strip()
    return None


def _off_value(nutriments: dict, key: str, scale: float = 1.0) -> float | None:
    value = nutriments.get(key)
    if isinstance(value, int | float):
        return round(float(value) * scale, 1)
    return None


async def off_lookup(client: httpx.AsyncClient, query: str) -> tuple[dict, str] | None:
    """Брендовый продукт в Open Food Facts."""
    resp = await _get(
        client,
        OFF_SEARCH_URL,
        params={
            "search_terms": query,
            "search_simple": 1,
            "action": "process",
            "json": 1,
            "page_size": 5,
            "fields": "product_name,brands,nutriments",
        },
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    for product in resp.json().get("products", []):
        n = product.get("nutriments", {})
        per_100g = {
            "calories_kcal": _off_value(n, "energy-kcal_100g"),
            "protein_g": _off_value(n, "proteins_100g"),
            "fat_total_g": _off_value(n, "fat_100g"),
            "fat_saturated_g": _off_value(n, "saturated-fat_100g"),
            "carbs_g": _off_value(n, "carbohydrates_100g"),
            "fiber_g": _off_value(n, "fiber_100g"),
            "sugar_g": _off_value(n, "sugars_100g"),
            "sodium_mg": _off_value(n, "sodium_100g", scale=1000),  # OFF отдаёт в граммах
        }
        if per_100g["calories_kcal"] is None:
            continue
        clean = {k: v for k, v in per_100g.items() if v is not None}
        brand = (product.get("brands") or "").split(",")[0].strip()
        name = product.get("product_name") or query
        return clean, f"OFF: {f'{brand} ' if brand else ''}{name}".strip()
    return None


# --------------------------------------------------------------------------
# Разрешение позиций
# --------------------------------------------------------------------------


async def resolve(
    items: list[RecognizedItem],
    conn: sqlite3.Connection,
    *,
    overrides: dict[str, dict],
    client: httpx.AsyncClient,
) -> list[ResolvedItem]:
    """Проставить БЖУ каждой позиции из справочников.

    Позиции обрабатываются параллельно. Внутри одного приёма пищи они обычно
    разные («гречка», «куриная грудка», «салат»), и каждая — это два запроса
    к справочнику подряд: поиск и карточка. Последовательно четыре новых
    позиции складываются в 4-8 с поверх и без того долгого вызова модели.

    Две позиции с одинаковым запросом оба раза промахнутся мимо кэша и сходят
    в сеть дважды — это допущено сознательно: `cache_put` идемпотентен
    (INSERT OR REPLACE), а совпадающие позиции в одном приёме пищи редки.
    Заводить реестр запросов в полёте ради этого случая дороже, чем он стоит.
    """
    return list(
        await asyncio.gather(
            *(_resolve_one(item, conn, overrides=overrides, client=client) for item in items)
        )
    )


def _off_query(item: RecognizedItem, query: str) -> str:
    """Поисковая строка для Open Food Facts.

    Берём английский lookup_query, а не name: name — на языке пользователя,
    и «протеиновый пудинг» в базе немецкого продукта не находится ничего.
    Ровно для этого промпт и просит модель дать запрос по-английски.

    Бренд добавляем, только если его в запросе ещё нет: модель часто включает
    его сама, и «Ehrmann Ehrmann High Protein» ищется хуже оригинала.
    """
    base = query or item.name
    brand = (item.brand or "").strip()
    if brand and brand.lower() not in base.lower():
        return f"{brand} {base}"
    return base


async def _resolve_one(
    item: RecognizedItem,
    conn: sqlite3.Connection,
    *,
    overrides: dict[str, dict],
    client: httpx.AsyncClient,
) -> ResolvedItem:
    grams = to_grams(item.quantity, item.unit)
    query = (item.lookup_query or item.name).strip()

    def manual(reason: str) -> ResolvedItem:
        return ResolvedItem(
            name=item.name,
            quantity=item.quantity,
            unit=item.unit,
            nutrients={},
            source_ref=reason,
            needs_manual=True,
        )

    if grams is None:
        return manual(f"manual: неизвестная единица «{item.unit}»")

    # 1. Локальные переопределения — приоритетнее всего: их правит человек
    for key in (item.name.lower(), query.lower()):
        if key in overrides:
            return ResolvedItem(
                name=item.name,
                quantity=item.quantity,
                unit=item.unit,
                nutrients=scale_per_100g(overrides[key], grams),
                source_ref="override",
            )

    def resolved(per_100g: dict, source_ref: str) -> ResolvedItem:
        return ResolvedItem(
            name=item.name,
            quantity=item.quantity,
            unit=item.unit,
            nutrients=scale_per_100g(per_100g, grams),
            source_ref=source_ref,
        )

    # 2. Брендовое — в Open Food Facts: там числа с этикетки конкретного
    #    продукта, чего модель знать не может.
    if item.kind == "branded":
        off_query = _off_query(item, query)

        # Ключ с брендом, иначе второй пудинг с тем же lookup_query получил бы
        # этикетку первого.
        hit = cache_get(conn, SOURCE_REFERENCE, off_query)
        if hit is not None:
            return resolved(*hit)

        try:
            found = await off_lookup(client, off_query)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.info("OFF недоступен (%s), берём оценку модели", type(exc).__name__)
            found = None

        if found is not None:
            per_100g, source_ref = found
            cache_put(conn, SOURCE_REFERENCE, off_query, per_100g, source_ref)
            return resolved(per_100g, source_ref)

        # Дальше — оценка модели, но в ячейку справочника она НЕ пишется.
        # Иначе один отказ OFF (а он отдаёт 503 заметной долей запросов)
        # навсегда закрепил бы за продуктом догадку вместо данных с этикетки.
        # Цена решения: пока продукта в OFF нет, его спрашивают при каждой
        # записи. Для личного трекера это несколько запросов в день.

    # 3. Кэш оценок модели по английскому lookup_query — одна еда считается
    #    один раз в жизни. Он же даёт воспроизводимость: модель может сегодня
    #    сказать 126, завтра 131, и сравнение дней станет шумным.
    cached = cache_get(conn, SOURCE_MODEL, query)
    if cached is not None:
        return resolved(*cached)

    # 4. Числа модели. Замерено 2026-10-01: на generic-еде они точнее поиска по
    #    USDA, который сопоставлял «кофе с молоком» с шоколадными конфетами.
    if item.per_100g:
        source_ref = f"model: {query}" if query else "model"
        cache_put(conn, SOURCE_MODEL, query or item.name.lower(), item.per_100g, source_ref)
        return resolved(item.per_100g, source_ref)

    return manual("manual: ни справочника, ни оценки модели")
