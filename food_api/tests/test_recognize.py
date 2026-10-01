"""Шаг 1: распознавание.

Главное здесь — не «модель вернула правильную еду» (это работа eval из Фазы 3),
а форма ответа: питательность приходит **на 100 г**, а не на порцию. От этого
зависит и кэш, и масштабирование, и возможность поправить число один раз.

Сеть замокана фикстурой no_network; клиенты подменяются целиком.
"""

import json
from types import SimpleNamespace

import httpx
import nutrition as nut
import pytest
import recognize as rec

PROMPT = "тестовый промпт"

PER_100G = {
    "calories_kcal": 92.0,
    "protein_g": 3.4,
    "fat_total_g": 0.6,
    "fat_saturated_g": 0.1,
    "carbs_g": 19.9,
    "fiber_g": 2.7,
    "sugar_g": 0.9,
    "sodium_mg": 4.0,
    "caffeine_mg": 0.0,
}

MEAL = {
    "items": [
        {
            "name": "гречка",
            "quantity": 180,
            "unit": "g",
            "kind": "generic",
            "lookup_query": "buckwheat groats, cooked",
            "brand": None,
            "per_100g": dict(PER_100G),
        }
    ],
    "meal_type": "lunch",
    "notes": "",
    "confidence": 0.8,
}


def local_body(meal: dict) -> dict:
    """Ответ OpenAI-совместимого эндпоинта (Ollama)."""
    return {"choices": [{"message": {"content": json.dumps(meal)}}]}


# --- форма ответа ----------------------------------------------------------


def test_schema_asks_for_every_nutrient():
    """Все девять колонок обязательны: недостающую потом нечем заполнить."""
    per_100g = rec.RECOGNIZE_SCHEMA["properties"]["items"]["items"]["properties"]["per_100g"]
    assert set(per_100g["properties"]) == set(nut.NUTRIENT_KEYS)
    assert set(per_100g["required"]) == set(nut.NUTRIENT_KEYS)
    assert per_100g["additionalProperties"] is False


def test_nutrition_is_asked_per_100g_not_per_portion():
    """От этого зависит всё остальное: кэш, масштабирование, правка значения.

    Сумма на порцию не переиспользуется для другого веса и заставляет модель
    умножать — а в умножении ошибаются чаще, чем в самих значениях.
    """
    assert "per_100g" in rec.RECOGNIZE_SCHEMA["properties"]["items"]["items"]["properties"]
    prompt = rec.load_prompt().lower()
    assert "per 100 g, never per portion" in prompt
    assert "not the amount eaten" in prompt


def test_prompt_does_not_promise_a_lookup_that_no_longer_happens():
    """Промпт уходит в модель дословно — это живая инструкция, не комментарий.

    Пока он обещал «generic ищется в USDA», модель готовила lookup_query для
    базы, которую никто больше не опрашивает.
    """
    text = rec.load_prompt() + json.dumps(rec.RECOGNIZE_SCHEMA, ensure_ascii=False)
    assert "USDA" not in text, "USDA убран из рабочего пути, обещать его нельзя"


def test_model_rejects_unknown_field():
    """Поле сбоку от схемы — всё ещё ошибка: extra=forbid на месте."""
    payload = json.loads(json.dumps(MEAL))
    payload["items"][0]["glycemic_index"] = 54
    with pytest.raises(rec.RecognizeError, match="валидацию"):
        rec.validate(payload)


@pytest.mark.parametrize("given,expected", [(-5.0, 0.0), (0.0, 0.0), (12.5, 12.5)])
def test_negative_macros_are_clamped(given, expected):
    """Схема не умеет minimum; отрицательный белок не повод терять весь разбор."""
    payload = json.loads(json.dumps(MEAL))
    payload["items"][0]["per_100g"]["protein_g"] = given
    assert rec.validate(payload).items[0].per_100g.protein_g == expected


def test_schema_matches_model():
    """Схема и Pydantic-модель не расходятся: два источника правды сверены."""
    assert set(rec.RECOGNIZE_SCHEMA["properties"]) == set(rec.RecognizedMeal.model_fields)
    item_schema = rec.RECOGNIZE_SCHEMA["properties"]["items"]["items"]
    assert set(item_schema["properties"]) == set(rec.RecognizedItemOut.model_fields)
    assert item_schema["additionalProperties"] is False

    per_100g = item_schema["properties"]["per_100g"]
    assert set(per_100g["properties"]) == set(rec.Per100g.model_fields)


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


async def test_persistent_server_error_gives_up_with_a_readable_message():
    def handler(request):
        return httpx.Response(500, text="oops")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(rec.RecognizeError, match="провайдер недоступен"):
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")


async def test_error_message_carries_no_url():
    """В адресе нет ничего полезного пользователю, а когда-то там был ключ.

    Код состояния («HTTP 500») остаётся — он помогает понять, что случилось.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(rec.RecognizeError) as err:
        await rec.LocalRecognizer(
            url="http://secret-host:11434/v1/chat", client=client, prompt=PROMPT
        ).recognize(text="еда")
    message = str(err.value)
    assert "://" not in message, message
    assert "secret-host" not in message, message
    assert "HTTP 500" in message, "код состояния стоит оставить"


def test_chain_can_name_a_model_per_entry():
    """Квота бесплатного тарифа Gemini — 20 запросов в сутки НА МОДЕЛЬ.

    Перебор нескольких моделей одного провайдера и есть способ прожить день;
    без этого все элементы gemini брали бы GEMINI_MODEL и упирались в одну
    и ту же квоту.
    """
    chain = rec.build_recognizer(
        {
            "RECOGNIZE_PROVIDER": "gemini:gemini-3.5-flash-lite,gemini:gemini-3.6-flash",
            "GEMINI_API_KEY": "not-a-real-key",
        }
    )
    assert [r.model for r in chain.recognizers] == ["gemini-3.5-flash-lite", "gemini-3.6-flash"]


def test_entry_without_a_model_falls_back_to_the_env():
    chain = rec.build_recognizer(
        {
            "RECOGNIZE_PROVIDER": "gemini,gemini:gemini-3.6-flash",
            "GEMINI_API_KEY": "not-a-real-key",
            "GEMINI_MODEL": "gemini-3.5-flash",
        }
    )
    assert [r.model for r in chain.recognizers] == ["gemini-3.5-flash", "gemini-3.6-flash"]


def test_unknown_provider_is_still_rejected_with_a_model_suffix():
    with pytest.raises(ValueError, match="нетакого"):
        rec.build_recognizer({"RECOGNIZE_PROVIDER": "нетакого:модель"})


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


async def test_transient_server_error_is_retried_then_succeeds():
    """503 через пару секунд обычно проходит — терять из-за него запрос незачем."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=local_body(MEAL))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    meal = await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")
    assert len(calls) == 2
    assert meal.items[0].name == "гречка"


GOOGLE_DAILY_QUOTA = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                        "quotaValue": "20",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "40s"},
        ],
    }
}

GOOGLE_THROTTLE = {
    "error": {
        "code": 429,
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerMinutePerProject-FreeTier"}],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"},
        ],
    }
}


async def test_daily_quota_is_not_retried():
    """Замерено на живом ключе: бесплатный тариф Gemini — 20 запросов В СУТКИ.

    Повторять бессмысленно: до завтра ничего не изменится, а три попытки
    только жгут время человека, который смотрит на индикатор.
    """
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json=GOOGLE_DAILY_QUOTA)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(rec.RecognizeRateLimited, match="суточная квота") as err:
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")

    assert len(calls) == 1, "суточную квоту повторять незачем"
    assert err.value.retry_after is None
    assert "минуту" not in str(err.value), "сообщать про минуту было бы враньём"


async def test_throttling_is_retried_and_reports_the_delay():
    """Минутный лимит — другое дело: повтор уместен, задержку берём у провайдера."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json=GOOGLE_THROTTLE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(rec.RecognizeRateLimited, match="через 7 с") as err:
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")

    assert len(calls) == rec.TRANSIENT_RETRIES + 1
    assert err.value.retry_after == 7.0


async def test_unparseable_429_still_gives_a_sane_message():
    """У другого провайдера форма своя — общий текст лучше падения."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down"))
    )
    with pytest.raises(rec.RecognizeRateLimited, match="лимит обращений"):
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")


async def test_rate_limit_has_its_own_error_type():
    """API отдаёт его клиенту как 429, чтобы тот сказал «подожди», а не «сломалось».

    Поймано вживую на бесплатном тарифе Gemini: раньше пользователь видел
    сырой текст с кодом 429 и ссылкой на MDN.
    """
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, text="slow down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(rec.RecognizeRateLimited, match="лимит"):
        await rec.LocalRecognizer(client=client, prompt=PROMPT).recognize(text="еда")
    assert len(calls) == rec.TRANSIENT_RETRIES + 1, "429 без разбора считаем троттлингом"


async def test_rate_limit_is_a_recognize_error_too():
    """Вызывающие ловят RecognizeError — подкласс не должен проскочить мимо."""
    assert issubclass(rec.RecognizeRateLimited, rec.RecognizeError)


async def test_client_error_is_not_retried():
    """4xx — это наш запрос, повтор его не исправит, только задержит ответ."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text="bad request")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(rec.RecognizeError, match="отклонён"):
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
