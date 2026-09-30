"""Шаг 1: распознавание.

Главное здесь — не «модель вернула правильную еду» (это работа eval из Фазы 3),
а что схема ответа не даёт вернуть БЖУ. Это и есть защита от галлюцинаций,
поэтому она закреплена тестом, а не только комментарием в коде.

Сеть замокана фикстурой no_network; клиенты подменяются целиком.
"""

import json
from types import SimpleNamespace

import httpx
import nutrition as nut
import pytest
import recognize as rec

PROMPT = "тестовый промпт"

MEAL = {
    "items": [
        {
            "name": "гречка",
            "quantity": 180,
            "unit": "g",
            "kind": "generic",
            "lookup_query": "buckwheat groats, cooked",
            "brand": None,
        }
    ],
    "meal_type": "lunch",
    "notes": "",
    "confidence": 0.8,
}


# --- защита от галлюцинаций ------------------------------------------------


def test_schema_has_no_nutrition_fields():
    """Ни одного поля питательности в схеме — выдумать число некуда."""
    blob = json.dumps(rec.RECOGNIZE_SCHEMA)
    for key in nut.NUTRIENT_KEYS:
        assert key not in blob


def test_model_rejects_nutrition_field():
    """Даже если модель припишет калории сбоку, extra=forbid это отвергнет."""
    payload = json.loads(json.dumps(MEAL))
    payload["items"][0]["calories_kcal"] = 620
    with pytest.raises(rec.RecognizeError, match="валидацию"):
        rec.validate(payload)


def test_prompt_forbids_nutrition_numbers():
    """Промпт на диске не должен снова начать просить БЖУ."""
    assert "do not return nutrition numbers" in rec.load_prompt().lower()


def test_schema_matches_model():
    """Схема и Pydantic-модель не расходятся: два источника правды сверены."""
    assert set(rec.RECOGNIZE_SCHEMA["properties"]) == set(rec.RecognizedMeal.model_fields)
    item_schema = rec.RECOGNIZE_SCHEMA["properties"]["items"]["items"]
    assert set(item_schema["properties"]) == set(rec.RecognizedItemOut.model_fields)
    assert item_schema["additionalProperties"] is False


# --- валидация и перевод в шаг 2 -------------------------------------------


@pytest.mark.parametrize(("given", "expected"), [(95, 1.0), (-1, 0.0), (0.42, 0.42)])
def test_confidence_is_clamped(given, expected):
    """Схема не умеет minimum/maximum, поэтому 95 вместо 0.95 придёт и пройдёт.

    Ронять из-за косметического поля весь разбор несоразмерно — подрезаем.
    """
    assert rec.validate({**MEAL, "confidence": given}).confidence == expected


def test_to_items_feeds_nutrition():
    items = rec.validate(MEAL).to_items()
    assert isinstance(items[0], nut.RecognizedItem)
    assert (items[0].name, items[0].quantity, items[0].kind) == ("гречка", 180, "generic")


@pytest.mark.parametrize(
    "broken",
    [
        {**MEAL, "meal_type": "brunch"},
        {**MEAL, "items": [{**MEAL["items"][0], "quantity": 0}]},
        {**MEAL, "items": [{**MEAL["items"][0], "kind": "homemade"}]},
    ],
    ids=["meal_type", "quantity", "kind"],
)
def test_validation_rejects(broken):
    with pytest.raises(rec.RecognizeError):
        rec.validate(broken)


def test_fenced_json_is_parsed():
    """Локальные модели любят обернуть ответ в ```json — это не ошибка."""
    assert rec._loads('```json\n{"a": 1}\n```') == {"a": 1}


# --- общие проверки входа ---------------------------------------------------


async def test_requires_text_or_image():
    with pytest.raises(rec.RecognizeError, match="text или image_b64"):
        await rec.AnthropicRecognizer(client=object(), prompt=PROMPT).recognize()


async def test_image_size_limit():
    big = "x" * (rec.MAX_IMAGE_B64_CHARS + 1)
    with pytest.raises(rec.RecognizeError, match="лимит"):
        await rec.AnthropicRecognizer(client=object(), prompt=PROMPT).recognize(image_b64=big)


# --- Anthropic --------------------------------------------------------------


def anthropic_response(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)]
    )


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeAnthropic:
    def __init__(self, *responses):
        self.messages = FakeMessages(responses)


async def test_anthropic_sends_schema_and_image():
    fake = FakeAnthropic(anthropic_response(MEAL))
    meal = await rec.AnthropicRecognizer(client=fake, prompt=PROMPT).recognize(
        text="180 г гречки", image_b64="aGk=", now="2026-09-30 13:00"
    )
    assert meal.items[0].name == "гречка"

    sent = fake.messages.calls[0]
    assert sent["output_config"]["format"]["schema"] is rec.RECOGNIZE_SCHEMA
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    blocks = sent["messages"][0]["content"]
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["data"] == "aGk="
    assert "Local time: 2026-09-30 13:00" in blocks[1]["text"]


async def test_anthropic_refusal_is_an_error():
    fake = FakeAnthropic(SimpleNamespace(stop_reason="refusal", content=[]))
    with pytest.raises(rec.RecognizeError, match="отказалась"):
        await rec.AnthropicRecognizer(client=fake, prompt=PROMPT).recognize(text="еда")


async def test_anthropic_retries_once():
    """Structured outputs гарантируют набор полей, но не диапазоны значений.

    minimum/maximum/minLength в них не поддержаны, поэтому ответ может пройти
    схему и упасть на Pydantic — второй шанс нужен и здесь, не только локальной
    модели.
    """
    fake = FakeAnthropic(anthropic_response("не json"), anthropic_response(MEAL))
    meal = await rec.AnthropicRecognizer(client=fake, prompt=PROMPT).recognize(text="еда")
    assert meal.items[0].name == "гречка"
    assert len(fake.messages.calls) == 2


async def test_anthropic_reports_truncation():
    """Обрезка по max_tokens получает свой диагноз, а не «не разобрался JSON»."""
    fake = FakeAnthropic(
        SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="text", text="{")])
    )
    with pytest.raises(rec.RecognizeError, match="не уместился"):
        await rec.AnthropicRecognizer(client=fake, prompt=PROMPT).recognize(text="еда")


# --- локальная модель -------------------------------------------------------


def local_client(*bodies):
    """Клиент, отдающий заготовленные ответы по очереди, и счётчик вызовов."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        body = bodies[min(len(calls) - 1, len(bodies) - 1)]
        content = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_local_retries_once():
    client, calls = local_client("не json", MEAL)
    meal = await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="гречка")
    assert meal.items[0].name == "гречка"
    assert len(calls) == 2
    assert "rejected" in json.dumps(calls[1], ensure_ascii=False)


async def test_local_retries_exactly_once():
    """Второй провал — ошибка, а не бесконечный цикл за наш счёт."""
    client, calls = local_client("не json")
    with pytest.raises(rec.RecognizeError):
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="гречка")
    assert len(calls) == 2


async def test_local_sends_image_as_data_url():
    client, calls = local_client(MEAL)
    await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(image_b64="aGk=")
    content = calls[0]["messages"][1]["content"]
    assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,aGk="


async def test_http_error_becomes_recognize_error():
    def handler(request):
        return httpx.Response(500, text="oops")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(rec.RecognizeError, match="не удался"):
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")


# --- Gemini -----------------------------------------------------------------


def test_schema_uses_documented_union_form():
    """anyOf — то, что structured outputs поддерживают; список типов там не значится."""
    brand = rec.RECOGNIZE_SCHEMA["properties"]["items"]["items"]["properties"]["brand"]
    assert brand == {"anyOf": [{"type": "string"}, {"type": "null"}]}


def test_gemini_schema_dialect():
    """У Gemini нет additionalProperties, а null выражается через nullable."""
    schema = rec.to_gemini_schema(rec.RECOGNIZE_SCHEMA)
    assert "additionalProperties" not in json.dumps(schema)
    assert "anyOf" not in json.dumps(schema)
    brand = schema["properties"]["items"]["items"]["properties"]["brand"]
    assert brand == {"type": "string", "nullable": True}
    assert schema["properties"]["meal_type"]["enum"] == list(rec.MEAL_TYPES)


async def test_gemini_parses_response():
    calls = []

    def handler(request):
        calls.append({"body": json.loads(request.content), "url": str(request.url)})
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": json.dumps(MEAL)}]}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meal = await rec.GeminiRecognizer(api_key="k", client=client, prompt=PROMPT).recognize(
        text="гречка"
    )
    assert meal.items[0].lookup_query == "buckwheat groats, cooked"
    assert calls[0]["body"]["generationConfig"]["responseMimeType"] == "application/json"


async def test_gemini_key_goes_in_header_not_url():
    """Ключ в query утекал бы в текст httpx-ошибки, а оттуда в лог."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(MEAL)}]}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Не похожий на настоящий: строка вида AIza... ловится сканером секретов,
    # и правильно делает.
    key = "not-a-real-key"
    await rec.GeminiRecognizer(api_key=key, client=client, prompt=PROMPT).recognize(text="еда")
    assert seen["key"] == key
    assert key not in seen["url"]


# --- фолбэк и сборка по окружению -------------------------------------------


class Stub:
    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = 0

    async def recognize(self, **kwargs):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_fallback_moves_to_next_provider():
    first = Stub("anthropic", rec.RecognizeError("нет ключа"))
    second = Stub("local", rec.validate(MEAL))
    meal = await rec.FallbackRecognizer([first, second]).recognize(text="еда")
    assert meal.items[0].name == "гречка"
    assert (first.calls, second.calls) == (1, 1)


async def test_fallback_reports_every_failure():
    chain = rec.FallbackRecognizer(
        [Stub("anthropic", rec.RecognizeError("нет ключа")), Stub("local", OSError("нет Ollama"))]
    )
    with pytest.raises(rec.RecognizeError) as err:
        await chain.recognize(text="еда")
    assert "anthropic: нет ключа" in str(err.value)
    assert "local: нет Ollama" in str(err.value)


def test_build_single_provider():
    built = rec.build_recognizer({"RECOGNIZE_PROVIDER": "local", "LOCAL_LLM_URL": "http://m4:1/v1"})
    assert isinstance(built, rec.LocalRecognizer)
    assert built.url == "http://m4:1/v1"


def test_build_chain_keeps_order():
    built = rec.build_recognizer({"RECOGNIZE_PROVIDER": "anthropic,local"})
    assert isinstance(built, rec.FallbackRecognizer)
    assert [r.name for r in built.recognizers] == ["anthropic", "local"]


def test_build_defaults_to_anthropic_opus():
    built = rec.build_recognizer({})
    assert isinstance(built, rec.AnthropicRecognizer)
    assert built.model == "claude-opus-5"


@pytest.mark.parametrize(
    "env",
    [{"RECOGNIZE_PROVIDER": "ollama"}, {"RECOGNIZE_PROVIDER": "gemini"}],
    ids=["unknown", "gemini-without-key"],
)
def test_build_rejects_bad_config(env):
    with pytest.raises(ValueError):
        rec.build_recognizer(env)


async def test_transport_error_is_not_retried():
    """Повтор с текстом ошибки лечит форму ответа, а не недоступность сервера."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(rec.RecognizeError, match="не удался"):
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")
    assert len(calls) == 1


async def test_non_json_body_is_a_format_error():
    """200 с HTML от прокси — ошибка формы: она получает повтор, а не 500."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, text="<html>bad gateway</html>")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(MEAL)}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meal = await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")
    assert meal.items[0].name == "гречка"
    assert len(calls) == 2


async def test_owned_http_client_is_reused_and_closed():
    """Один клиент на распознаватель: вызов и так стоит 5-20 с, TLS на каждый — лишнее."""
    recognizer = rec.LocalRecognizer(prompt=PROMPT)
    assert recognizer.http() is recognizer.http()
    await recognizer.aclose()
    assert recognizer._owned is None


async def test_injected_http_client_is_not_closed():
    """Чужой клиент закрывать не наше дело."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    recognizer = rec.LocalRecognizer(client=client, prompt=PROMPT)
    await recognizer.aclose()
    assert not client.is_closed
    await client.aclose()


async def test_fallback_closes_every_provider():
    class Closable(Stub):
        closed = False

        async def aclose(self):
            self.closed = True

    first, second = Closable("a", OSError("нет")), Closable("b", rec.validate(MEAL))
    await rec.FallbackRecognizer([first, second]).aclose()
    assert (first.closed, second.closed) == (True, True)
