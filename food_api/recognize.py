"""Шаг 1 конвейера: распознавание. Что съели и сколько — без единого числа БЖУ.

Питательность считает nutrition.py по справочникам. Здесь модель отвечает только
на вопрос «что на тарелке и сколько грамм», и защита от галлюцинаций встроена в
схему: полей под калории и макросы в ней нет, а `extra: forbid` не даёт их
дописать сбоку. Выдумать число невозможно, если его некуда положить.

Провайдер выбирается переменной RECOGNIZE_PROVIDER (можно списком через запятую —
тогда следующий пробуется, если предыдущий упал). Абстракция здесь именно потому,
что выбор модели решается замером на своих фото, а не архитектурой.
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from nutrition import RecognizedItem
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).with_name("recognize_prompt.md")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_LOCAL_MODEL = "qwen3-vl:8b"
DEFAULT_LOCAL_URL = "http://localhost:11434/v1/chat/completions"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Потолок на картинку: 1.5 МБ base64 ≈ 1.1 МБ бинарных. Оригинал с 12 Мп камеры
# сюда не влезает — и не должен: клиент обязан сжать перед отправкой (§2.3).
MAX_IMAGE_B64_CHARS = 1_500_000

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

# Ответ короткий, но на Opus 5 адаптивное мышление тоже считается в max_tokens,
# поэтому запас, а не 512.
MAX_TOKENS = 4096


class RecognizeError(RuntimeError):
    """Провайдер не смог вернуть валидный разбор."""


class RecognizeFormatError(RecognizeError):
    """Модель ответила не в той форме: не JSON или не по схеме.

    Отделено от сетевых и прочих отказов намеренно: повторять с текстом ошибки
    в промпте осмысленно только здесь. Повтор на 500 от сервера — это просто
    второй счёт за ту же недоступность.
    """


class RecognizedItemOut(BaseModel):
    """Одна позиция в ответе модели. Полей питательности здесь нет намеренно."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1)
    kind: Literal["branded", "generic"]
    lookup_query: str = Field(min_length=1)
    brand: str | None = None


class RecognizedMeal(BaseModel):
    """Разбор приёма пищи целиком."""

    model_config = ConfigDict(extra="forbid")

    items: list[RecognizedItemOut]
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    notes: str = ""
    confidence: float = Field(ge=0.0, le=1.0)

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
                    "lookup_query": {"type": "string", "description": "English, for USDA/OFF"},
                    "brand": {"type": ["string", "null"]},
                },
                "required": ["name", "quantity", "unit", "kind", "lookup_query", "brand"],
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

    Вместо `"type": ["string", "null"]` у него `nullable`. Отдельная функция,
    а не вторая копия схемы, — чтобы источник правды остался один.
    """
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "type" and isinstance(value, list):
            out["type"] = next(t for t in value if t != "null")
            if "null" in value:
                out["nullable"] = True
        elif key in {"properties", "items"} and isinstance(value, dict):
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


class BaseRecognizer(ABC):
    """Общая обвязка: проверки на входе, валидация и политика повтора на выходе.

    Повтор включён только там, где схему не гарантирует провайдер (локальная
    модель). Anthropic и Gemini держат форму сами — там ретрай лишь удвоил бы
    счёт за ту же ошибку.
    """

    name = "base"
    retry_on_invalid = False

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
                f"картинка {len(image_b64)} байт base64, лимит {MAX_IMAGE_B64_CHARS}"
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

    @abstractmethod
    async def _call(
        self,
        *,
        user_text: str,
        image_b64: str | None,
        media_type: str,
        repair: str | None = None,
    ) -> dict[str, Any]: ...


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

    def client(self) -> Any:
        """Ленивое создание клиента: без вызова SDK не нужен даже установленным."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as err:  # pragma: no cover - зависит от окружения
                raise RecognizeError("пакет anthropic не установлен") from err
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key or None)
        return self._client

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
        if getattr(response, "stop_reason", None) == "refusal":
            raise RecognizeError("модель отказалась отвечать")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise RecognizeError("в ответе нет текстового блока")
        return _loads(text)


class GeminiRecognizer(BaseRecognizer):
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
        super().__init__(prompt)
        self.api_key = api_key
        self.model = model
        self._client = client

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
        data = await _post_json(
            self._client,
            GEMINI_URL.format(model=self.model),
            json=body,
            params={"key": self.api_key},
        )
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as err:
            raise RecognizeError(f"неожиданная форма ответа Gemini: {data}") from err
        return _loads(text)


class LocalRecognizer(BaseRecognizer):
    """Ollama на M4 через OpenAI-совместимый эндпоинт.

    Локальные модели держат схему хуже, поэтому единственный провайдер с
    включённым повтором: ошибка валидации уходит в промпт и даётся второй шанс.
    """

    name = "local"
    retry_on_invalid = True

    def __init__(
        self,
        *,
        url: str = DEFAULT_LOCAL_URL,
        model: str = DEFAULT_LOCAL_MODEL,
        client: httpx.AsyncClient | None = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(prompt)
        self.url = url
        self.model = model
        self._client = client

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
        data = await _post_json(self._client, self.url, json=body)
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


async def _post_json(
    client: httpx.AsyncClient | None,
    url: str,
    *,
    json: dict[str, Any],
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST с общей обработкой ошибок. Свой клиент, если не передали чужой."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=60.0)
    try:
        response = await client.post(url, json=json, params=params)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as err:
        raise RecognizeError(f"запрос к {url} не удался: {err}") from err
    finally:
        if owned:
            await client.aclose()


def _build_anthropic(env: dict[str, str]) -> Recognizer:
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
