"""Шаг 1 конвейера: что съели, сколько и что в этом содержится.

Модель называет еду, оценивает порцию и даёт питательность **на 100 г** —
не на съеденное. Умножает сервер; на 100 г число кэшируется по lookup_query и
переиспользуется для любой порции.

Числа у модели забирали до 2026-10-01 ради защиты от галлюцинаций, а брали из
USDA. Замер показал, что ошибка сопоставления крупнее: «кофе с молоком»
находился как шоколадные конфеты, 1098 ккал вместо ~45. Обоснование разворота —
в WEBAPP.md §1.3++. Защита теперь не в схеме, а в происхождении: source_ref
говорит, откуда число, кэш делает ошибку повторяемой и потому заметной, а
overrides.json правит её навсегда.

Провайдер выбирается переменной RECOGNIZE_PROVIDER (можно списком через запятую —
тогда следующий пробуется, если предыдущий упал). Абстракция здесь именно потому,
что выбор модели решается замером на своих фото, а не архитектурой.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from nutrition import NUTRIENT_KEYS, RecognizedItem
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).with_name("recognize_prompt.md")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
# Проверено живым ключом 2026-10-01: gemini-2.5-flash и -lite отвечают 404
# «no longer available to new users» — то есть на новом ключе дефолт не
# работал вовсе. Более новая gemini-3.8-flash в тот момент стабильно отдавала
# 503 «high demand», поэтому дефолт — 3.5-flash: она отвечает. Переопределяется
# через GEMINI_MODEL, и когда 3.8 перестанет быть перегруженной, её стоит
# попробовать снова.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_LOCAL_MODEL = "qwen3-vl:8b"
DEFAULT_LOCAL_URL = "http://localhost:11434/v1/chat/completions"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Потолок на картинку: 1.5 МБ base64 ≈ 1.1 МБ бинарных. Оригинал с 12 Мп камеры
# сюда не влезает — и не должен: клиент обязан сжать перед отправкой (§2.3).
MAX_IMAGE_B64_CHARS = 1_500_000

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

# Ответ короткий, но на Opus 5 адаптивное мышление включено по умолчанию и тоже
# считается в max_tokens. Обрезанный по лимиту JSON не разберётся, а structured
# outputs от этого не спасают, поэтому берём рекомендованные для нестримингового
# вызова ~16k, а не «сколько кажется достаточным».
MAX_TOKENS = 16000
# Временные отказы провайдера: 429 на бесплатном тарифе ловится легко, и через
# пару секунд запрос обычно проходит. Дольше ждать нельзя — на том конце
# человек смотрит на индикатор.
TRANSIENT_RETRIES = 2
TRANSIENT_BACKOFF = 2.0


class RecognizeError(RuntimeError):
    """Провайдер не смог вернуть валидный разбор."""


class RecognizeRateLimited(RecognizeError):
    """Провайдер попросил притормозить.

    Отдельный класс, а не разбор текста ошибки: API отдаёт его клиенту как 429,
    и тот может сказать «подожди минуту» вместо «что-то пошло не так».
    """


class RecognizeFormatError(RecognizeError):
    """Модель ответила не в той форме: не JSON или не по схеме.

    Отделено от сетевых и прочих отказов намеренно: повторять с текстом ошибки
    в промпте осмысленно только здесь. Повтор на 500 от сервера — это просто
    второй счёт за ту же недоступность.
    """


class Per100g(BaseModel):
    """Питательность на 100 г продукта — не на съеденную порцию.

    На 100 г, потому что это свойство еды, а не приёма пищи: его можно
    закэшировать по lookup_query и отмасштабировать арифметикой. Попроси мы
    сумму на порцию — кэш стал бы бесполезен (каждая порция своя), а модель
    считала бы умножение, в котором ошибаются чаще, чем в самих значениях.
    """

    model_config = ConfigDict(extra="forbid")

    calories_kcal: float
    protein_g: float
    fat_total_g: float
    fat_saturated_g: float
    carbs_g: float
    fiber_g: float
    sugar_g: float
    sodium_mg: float
    caffeine_mg: float

    @field_validator("*")
    @classmethod
    def _no_negatives(cls, value: float) -> float:
        """Отрицательных макросов не бывает.

        Схема structured outputs не умеет minimum, так что отсечь можно только
        здесь. Подрезаем, а не отвергаем: одно странное поле не повод терять
        весь разбор.
        """
        return max(0.0, value)


class RecognizedItemOut(BaseModel):
    """Одна позиция в ответе модели."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1)
    kind: Literal["branded", "generic"]
    lookup_query: str = Field(min_length=1)
    brand: str | None = None
    per_100g: Per100g


class RecognizedMeal(BaseModel):
    """Разбор приёма пищи целиком."""

    model_config = ConfigDict(extra="forbid")

    items: list[RecognizedItemOut]
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    notes: str = ""
    confidence: float

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        """Подрезать, а не отвергать.

        Structured outputs не умеют minimum/maximum — модель вправе вернуть 95
        вместо 0.95, и схема это пропустит. Ронять из-за косметического поля
        весь разбор приёма пищи несоразмерно: позиции-то распознаны.
        """
        return min(1.0, max(0.0, value))

    def to_items(self) -> list[RecognizedItem]:
        """Перевод в то, что принимает nutrition.resolve()."""
        return [
            RecognizedItem(
                name=item.name,
                quantity=item.quantity,
                unit=item.unit,
                kind=item.kind,
                lookup_query=item.lookup_query,
                brand=item.brand,
                per_100g=item.per_100g.model_dump(),
            )
            for item in self.items
        ]


# Схема для structured outputs. Написана руками, а не выведена из Pydantic:
# провайдеры расходятся в поддержке $defs/$ref, а здесь нужна форма, которую
# принимают все три. Тест сверяет её с моделью — расхождение не проедет молча.
RECOGNIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "In the user's language"},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string", "description": "g, ml, шт"},
                    "kind": {"type": "string", "enum": ["branded", "generic"]},
                    "lookup_query": {
                        "type": "string",
                        "description": "English; cache key and Open Food Facts query",
                    },
                    "brand": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "per_100g": {
                        "type": "object",
                        "description": "Per 100 g of the food as prepared, not per portion",
                        "properties": {key: {"type": "number"} for key in NUTRIENT_KEYS},
                        "required": list(NUTRIENT_KEYS),
                        "additionalProperties": False,
                    },
                },
                "required": [
                    "name",
                    "quantity",
                    "unit",
                    "kind",
                    "lookup_query",
                    "brand",
                    "per_100g",
                ],
                "additionalProperties": False,
            },
        },
        "meal_type": {"type": "string", "enum": list(MEAL_TYPES)},
        "notes": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["items", "meal_type", "notes", "confidence"],
    "additionalProperties": False,
}


@lru_cache(maxsize=1)
def load_prompt(path: str | None = None) -> str:
    """Системный промпт из recognize_prompt.md."""
    return Path(path or PROMPT_PATH).read_text(encoding="utf-8")


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Схема в диалекте Gemini: он не знает additionalProperties и type-списков.

    Nullable у него выражается флагом `nullable`, а не через `anyOf`, и
    `additionalProperties` он не знает вовсе. Отдельная функция, а не вторая
    копия схемы, — чтобы источник правды остался один.
    """
    if "anyOf" in schema:
        variants = [v for v in schema["anyOf"] if v.get("type") != "null"]
        if len(variants) != 1:
            raise ValueError(f"anyOf из {len(variants)} вариантов Gemini не выразит")
        converted = to_gemini_schema(variants[0])
        if len(variants) != len(schema["anyOf"]):
            converted["nullable"] = True
        return converted

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key in {"properties", "items"} and isinstance(value, dict):
            out[key] = (
                {k: to_gemini_schema(v) for k, v in value.items()}
                if key == "properties"
                else to_gemini_schema(value)
            )
        else:
            out[key] = value
    return out


def _user_text(text: str | None, now: str | None) -> str:
    """Пользовательская часть запроса. Время нужно модели для meal_type."""
    parts = []
    if now:
        parts.append(f"Local time: {now}")
    parts.append(f"User said: {text}" if text else "Photo of a meal, no text given.")
    return "\n".join(parts)


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _loads(raw: str) -> dict[str, Any]:
    """JSON из ответа модели. Локальные модели любят обернуть его в ```json."""
    match = _FENCE.match(raw)
    if match:
        raw = match.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise RecognizeFormatError(f"ответ не разобрался как JSON: {err}") from err
    if not isinstance(data, dict):
        raise RecognizeFormatError(f"ожидался объект, пришёл {type(data).__name__}")
    return data


def validate(payload: dict[str, Any]) -> RecognizedMeal:
    """Pydantic-валидация ответа модели."""
    try:
        return RecognizedMeal.model_validate(payload)
    except ValidationError as err:
        raise RecognizeFormatError(f"ответ не прошёл валидацию: {err}") from err


class Recognizer(Protocol):
    """Контракт шага 1. Провайдер — конфиг, а не архитектура."""

    name: str

    async def recognize(
        self,
        *,
        text: str | None = None,
        image_b64: str | None = None,
        media_type: str = "image/jpeg",
        now: str | None = None,
    ) -> RecognizedMeal: ...

    async def aclose(self) -> None: ...


class BaseRecognizer(ABC):
    """Общая обвязка: проверки на входе, валидация и политика повтора на выходе.

    Повтор включён у всех провайдеров, а не только у локального. Structured
    outputs гарантируют набор полей, но не диапазоны: `minimum`/`maximum`/
    `minLength` в них не поддержаны, поэтому «quantity: 0» или «confidence: 95»
    пройдут схему и упадут на Pydantic. Один повтор с текстом ошибки дешевле,
    чем отказ всего запроса из-за такого ответа.
    """

    name = "base"
    retry_on_invalid = True

    def __init__(self, prompt: str | None = None) -> None:
        self.prompt = prompt if prompt is not None else load_prompt()

    async def recognize(
        self,
        *,
        text: str | None = None,
        image_b64: str | None = None,
        media_type: str = "image/jpeg",
        now: str | None = None,
    ) -> RecognizedMeal:
        if not text and not image_b64:
            raise RecognizeError("нужен text или image_b64")
        if image_b64 and len(image_b64) > MAX_IMAGE_B64_CHARS:
            raise RecognizeError(
                f"картинка — {len(image_b64)} символов base64, лимит {MAX_IMAGE_B64_CHARS}"
            )

        user_text = _user_text(text, now)
        try:
            payload = await self._call(
                user_text=user_text, image_b64=image_b64, media_type=media_type
            )
            return validate(payload)
        except RecognizeFormatError as err:
            if not self.retry_on_invalid:
                raise
            log.warning("%s: ответ не в той форме, повтор с текстом ошибки", self.name)
            payload = await self._call(
                user_text=user_text,
                image_b64=image_b64,
                media_type=media_type,
                repair=str(err),
            )
            return validate(payload)

    async def aclose(self) -> None:  # noqa: B027 - не абстрактный намеренно
        """Отпустить ресурсы. У провайдера без своего клиента их нет."""

    @abstractmethod
    async def _call(
        self,
        *,
        user_text: str,
        image_b64: str | None,
        media_type: str,
        repair: str | None = None,
    ) -> dict[str, Any]: ...


class HttpRecognizer(BaseRecognizer):
    """Провайдер, который ходит по HTTP сам (Gemini, локальная модель).

    Клиент создаётся один раз и переиспользуется: вызов модели и так стоит
    5-20 с, и открывать под каждый новый TLS-хэндшейк — платить за латентность
    дважды. Переданный извне клиент считается чужим и не закрывается.
    """

    def __init__(self, prompt: str | None = None, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(prompt)
        self._client = client
        self._owned: httpx.AsyncClient | None = None

    def http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=60.0)
        return self._owned

    async def aclose(self) -> None:
        if self._owned is not None:
            await self._owned.aclose()
            self._owned = None

    async def _post_json(
        self,
        url: str,
        *,
        json: dict[str, Any],
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST с повтором временных отказов и человеческими сообщениями.

        Повторяем 429, 5xx и обрывы связи — через пару секунд они обычно
        проходят. 4xx не повторяем: это наш запрос, повтор его не исправит.

        Тело с кодом 200, не разобравшееся как JSON (HTML-страница от прокси,
        пустой ответ), — ошибка формы: её повторяет BaseRecognizer, добавив
        текст ошибки в промпт.

        Сообщения не включают URL: в нём нет ничего полезного пользователю, а
        когда-то там был ключ.
        """
        delay = TRANSIENT_BACKOFF

        for attempt in range(TRANSIENT_RETRIES + 1):
            last = attempt == TRANSIENT_RETRIES
            try:
                response = await self.http().post(url, json=json, params=params, headers=headers)
            except httpx.InvalidURL as err:
                raise RecognizeError(f"{self.name}: неверный адрес провайдера") from err
            except httpx.TransportError as err:  # таймауты тоже сюда
                if last:
                    raise RecognizeError(
                        f"{self.name}: нет связи с провайдером ({type(err).__name__})"
                    ) from err
            else:
                code = response.status_code
                if code == 429:
                    if last:
                        raise RecognizeRateLimited(
                            f"{self.name}: превышен лимит обращений, попробуй через минуту"
                        )
                elif code >= 500:
                    if last:
                        raise RecognizeError(f"{self.name}: провайдер недоступен (HTTP {code})")
                elif code >= 400:
                    raise RecognizeError(f"{self.name}: запрос отклонён (HTTP {code})")
                else:
                    try:
                        return response.json()
                    except ValueError as err:
                        raise RecognizeFormatError(f"{self.name}: ответ не JSON ({err})") from err

            await asyncio.sleep(delay)
            delay *= 2

        raise AssertionError("недостижимо")  # pragma: no cover


class AnthropicRecognizer(BaseRecognizer):
    """Baseline. Structured outputs гарантируют форму — парсить текст не нужно."""

    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: str | None = None,
        client: Any = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(prompt)
        self.model = model
        self.api_key = api_key
        self._client = client
        self._owns_client = client is None

    def client(self) -> Any:
        """Ленивое создание клиента: без вызова SDK не нужен даже установленным."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as err:  # pragma: no cover - зависит от окружения
                raise RecognizeError("пакет anthropic не установлен") from err
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key or None)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.close()
            self._client = None

    async def _call(
        self,
        *,
        user_text: str,
        image_b64: str | None,
        media_type: str,
        repair: str | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if image_b64:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                }
            )
        content.append({"type": "text", "text": user_text})

        response = await self.client().messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            # Промпт ~850 токенов — выше минимума кэширования Opus 5 (512),
            # так что breakpoint здесь действительно работает, а не стоит для вида.
            system=[{"type": "text", "text": self.prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={
                "format": {"type": "json_schema", "schema": RECOGNIZE_SCHEMA},
                "effort": "low",
            },
        )
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise RecognizeError("модель отказалась отвечать")
        if stop == "max_tokens":
            # JSON оборван на полуслове. Без этой ветки диагноз был бы
            # «ответ не разобрался как JSON» — правдивый, но уводящий не туда.
            raise RecognizeError(f"ответ не уместился в {MAX_TOKENS} токенов")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise RecognizeError("в ответе нет текстового блока")
        return _loads(text)


class GeminiRecognizer(HttpRecognizer):
    """Бесплатный тариф Google AI Studio.

    Не дефолт осознанно: на free tier Google учится на данных, а здесь это фото
    еды и данные о здоровье. Квоты резались в декабре 2025 — на них не строится
    ничего критичного.
    """

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        client: httpx.AsyncClient | None = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(prompt, client)
        self.api_key = api_key
        self.model = model

    async def _call(
        self,
        *,
        user_text: str,
        image_b64: str | None,
        media_type: str,
        repair: str | None = None,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if image_b64:
            parts.append({"inline_data": {"mime_type": media_type, "data": image_b64}})
        parts.append({"text": user_text if not repair else f"{user_text}\n\nFix: {repair}"})

        body = {
            "systemInstruction": {"parts": [{"text": self.prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": to_gemini_schema(RECOGNIZE_SCHEMA),
            },
        }
        # Ключ заголовком, а не в query: httpx вшивает URL с параметрами в текст
        # httpx.HTTPStatusError, а этот текст уходит в лог и в сообщение об ошибке.
        # Ключ в строке запроса утекал бы при каждом 400 от Gemini.
        data = await self._post_json(
            GEMINI_URL.format(model=self.model),
            json=body,
            headers={"x-goog-api-key": self.api_key},
        )
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as err:
            raise RecognizeError(f"неожиданная форма ответа Gemini: {data}") from err
        return _loads(text)


class LocalRecognizer(HttpRecognizer):
    """Ollama на M4 через OpenAI-совместимый эндпоинт.

    Локальные модели держат схему хуже всех, но политика повтора теперь общая
    (см. BaseRecognizer): ошибка формы уходит в промпт и даётся второй шанс.
    """

    name = "local"

    def __init__(
        self,
        *,
        url: str = DEFAULT_LOCAL_URL,
        model: str = DEFAULT_LOCAL_MODEL,
        client: httpx.AsyncClient | None = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(prompt, client)
        self.url = url
        self.model = model

    async def _call(
        self,
        *,
        user_text: str,
        image_b64: str | None,
        media_type: str,
        repair: str | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        if image_b64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                }
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt},
            {"role": "user", "content": content},
        ]
        if repair:
            messages.append(
                {
                    "role": "user",
                    "content": f"Your previous answer was rejected: {repair}. "
                    "Return only JSON matching the schema.",
                }
            )

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "recognized_meal",
                    "schema": RECOGNIZE_SCHEMA,
                    "strict": True,
                },
            },
        }
        data = await self._post_json(self.url, json=body)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as err:
            raise RecognizeError(f"неожиданная форма ответа локальной модели: {data}") from err
        return _loads(text)


class FallbackRecognizer:
    """Цепочка провайдеров: следующий пробуется, если предыдущий упал."""

    name = "fallback"

    def __init__(self, recognizers: list[Recognizer]) -> None:
        if not recognizers:
            raise ValueError("нужен хотя бы один провайдер")
        self.recognizers = recognizers

    async def recognize(
        self,
        *,
        text: str | None = None,
        image_b64: str | None = None,
        media_type: str = "image/jpeg",
        now: str | None = None,
    ) -> RecognizedMeal:
        errors = []
        for recognizer in self.recognizers:
            try:
                return await recognizer.recognize(
                    text=text, image_b64=image_b64, media_type=media_type, now=now
                )
            except Exception as err:
                log.warning("провайдер %s не смог: %s", recognizer.name, err)
                errors.append(f"{recognizer.name}: {err}")
        raise RecognizeError("все провайдеры отказали — " + "; ".join(errors))

    async def aclose(self) -> None:
        for recognizer in self.recognizers:
            await recognizer.aclose()


def _build_anthropic(env: dict[str, str]) -> Recognizer:
    if not env.get("ANTHROPIC_API_KEY"):
        # Не ошибка: SDK умеет брать учётку и из профиля `ant auth login`.
        # Но на Pi профиля нет, и молчать об этом до первого фото не стоит.
        log.warning("ANTHROPIC_API_KEY не задан — SDK будет искать учётку сам")
    return AnthropicRecognizer(
        model=env.get("RECOGNIZE_MODEL") or DEFAULT_ANTHROPIC_MODEL,
        api_key=env.get("ANTHROPIC_API_KEY"),
    )


def _build_gemini(env: dict[str, str]) -> Recognizer:
    key = env.get("GEMINI_API_KEY")
    if not key:
        raise ValueError("RECOGNIZE_PROVIDER=gemini, но GEMINI_API_KEY не задан")
    return GeminiRecognizer(api_key=key, model=env.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL)


def _build_local(env: dict[str, str]) -> Recognizer:
    return LocalRecognizer(
        url=env.get("LOCAL_LLM_URL") or DEFAULT_LOCAL_URL,
        model=env.get("LOCAL_LLM_MODEL") or DEFAULT_LOCAL_MODEL,
    )


_BUILDERS = {"anthropic": _build_anthropic, "gemini": _build_gemini, "local": _build_local}


def build_recognizer(env: dict[str, str] | None = None) -> Recognizer:
    """Собрать распознаватель по окружению.

    RECOGNIZE_PROVIDER — имя или список через запятую (`anthropic,local`):
    порядок и есть порядок фолбэка, без неявных правил.
    """
    env = dict(os.environ) if env is None else env
    names = [n.strip() for n in env.get("RECOGNIZE_PROVIDER", "anthropic").split(",") if n.strip()]
    if not names:
        raise ValueError("RECOGNIZE_PROVIDER пуст")
    unknown = [n for n in names if n not in _BUILDERS]
    if unknown:
        raise ValueError(f"неизвестный провайдер: {', '.join(unknown)}")

    chain = [_BUILDERS[name](env) for name in names]
    return chain[0] if len(chain) == 1 else FallbackRecognizer(chain)
