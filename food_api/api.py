import json
import os
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
import nutrition as nut
import recognize as rec
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Схема создаётся один раз на старте, а не на каждом запросе.

    Здесь же живут долгоживущие ресурсы шага распознавания: httpx-клиент под
    лукапы в справочники и сам распознаватель. Оба закрываются на выходе —
    иначе asyncio ругается на брошенные соединения при остановке.
    """
    _init_schema()
    app.state.http = httpx.AsyncClient(timeout=LOOKUP_TIMEOUT)
    app.state.overrides = nut.load_overrides(OVERRIDES_PATH)
    app.state.recognizer = None
    try:
        yield
    finally:
        await app.state.http.aclose()
        if app.state.recognizer is not None:
            await app.state.recognizer.aclose()


app = FastAPI(title="Meow Food API", version="1.0.0", lifespan=lifespan)

DB_PATH = os.getenv("DB_PATH", "data/food.db")
API_KEY = os.getenv("FOOD_API_KEY", "")
USER_TZ = ZoneInfo(os.getenv("USER_TIMEZONE", "UTC"))
OVERRIDES_PATH = Path(__file__).parent / "overrides.json"
WEBAPP_DIR = Path(__file__).parent / "webapp"
# Лукап в справочник — два коротких запроса подряд; если USDA молчит полминуты,
# ждать её дольше смысла нет, позиция всё равно уйдёт в needs_manual.
LOOKUP_TIMEOUT = 30.0
# Кука сессии для webapp. Secure по умолчанию; выключается только для локальной
# отладки по http, где браузер Secure-куку просто не сохранит.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") != "0"
SESSION_COOKIE = "session"
SESSION_MAX_AGE = 365 * 24 * 60 * 60
# Неудачные попытки входа на один адрес до временной блокировки. Вход теперь
# достижим из любого браузера через туннель, а вся защита держится на энтропии
# FOOD_API_KEY — без счётчика её подбирают без ограничений по скорости.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300
# За туннелем request.client.host — адрес прокси, а не клиента, и счётчик
# сложил бы всех в одно ведро. Заголовок с настоящим адресом берём только если
# он назван явно (Cloudflare — CF-Connecting-IP, Caddy — X-Forwarded-For):
# доверять ему без настройки нельзя, клиент подставит любой и обойдёт счётчик.
TRUST_CLIENT_IP_HEADER = os.getenv("TRUST_CLIENT_IP_HEADER", "")

# Nutrient columns mirror Apple HealthKit dietary types so a future
# Apple Health / Garmin / MyFitnessPal exporter is a plain field copy.
# fmt: off
NUTRIENTS = [
    "calories_kcal",     # HKQuantityTypeIdentifierDietaryEnergyConsumed
    "protein_g",         # DietaryProtein
    "fat_total_g",       # DietaryFatTotal
    "fat_saturated_g",   # DietaryFatSaturated
    "carbs_g",           # DietaryCarbohydrates
    "fiber_g",           # DietaryFiber
    "sugar_g",           # DietarySugar
    "sodium_mg",         # DietarySodium
    "caffeine_mg",       # DietaryCaffeine
]
# fmt: on
MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack"]
MACRO_KEYS = ["calories_kcal", "protein_g", "fat_total_g", "carbs_g"]

_CREATE_LOGS = """
CREATE TABLE IF NOT EXISTS food_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    log_date    TEXT NOT NULL,
    meal_type   TEXT NOT NULL DEFAULT 'snack',
    note        TEXT DEFAULT '',
    source      TEXT DEFAULT 'text',
    health_exported_at TEXT DEFAULT NULL
)
"""

_CREATE_LOGS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_log_date
ON food_logs(log_date)
"""

_CREATE_ITEMS = """
CREATE TABLE IF NOT EXISTS food_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id          INTEGER NOT NULL,
    name            TEXT NOT NULL,
    quantity        REAL DEFAULT 1,
    unit            TEXT DEFAULT 'serving',
    calories_kcal   REAL NOT NULL DEFAULT 0,
    protein_g       REAL DEFAULT 0,
    fat_total_g     REAL DEFAULT 0,
    fat_saturated_g REAL DEFAULT 0,
    carbs_g         REAL DEFAULT 0,
    fiber_g         REAL DEFAULT 0,
    sugar_g         REAL DEFAULT 0,
    sodium_mg       REAL DEFAULT 0,
    caffeine_mg     REAL DEFAULT 0,
    FOREIGN KEY (log_id) REFERENCES food_logs(id)
)
"""

_CREATE_ITEMS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_items_log
ON food_items(log_id)
"""

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
)
"""

_CREATE_TARGETS = """
CREATE TABLE IF NOT EXISTS targets (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    calories_kcal REAL,
    protein_g     REAL,
    fat_total_g   REAL,
    carbs_g       REAL,
    weight_kg     REAL
)
"""

_CREATE_ACTIVITY = """
CREATE TABLE IF NOT EXISTS activity_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    performed_at  TEXT NOT NULL,
    log_date      TEXT NOT NULL,
    name          TEXT NOT NULL,
    activity_type TEXT DEFAULT '',
    duration_min  REAL DEFAULT 0,
    calories_kcal REAL NOT NULL DEFAULT 0,
    note          TEXT DEFAULT '',
    source        TEXT DEFAULT 'manual',
    external_id   TEXT DEFAULT NULL
)
"""

_CREATE_ACTIVITY_DATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_activity_date
ON activity_logs(log_date)
"""

_CREATE_ACTIVITY_EXT_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_ext
ON activity_logs(external_id) WHERE external_id IS NOT NULL
"""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(food_logs)")}
    if "health_exported_at" not in cols:
        conn.execute("ALTER TABLE food_logs ADD COLUMN health_exported_at TEXT DEFAULT NULL")
    target_cols = {row["name"] for row in conn.execute("PRAGMA table_info(targets)")}
    if target_cols and "weight_kg" not in target_cols:
        conn.execute("ALTER TABLE targets ADD COLUMN weight_kg REAL")


_SCHEMA = [
    _CREATE_LOGS,
    _CREATE_LOGS_INDEX,
    _CREATE_ITEMS,
    _CREATE_ITEMS_INDEX,
    _CREATE_TARGETS,
    _CREATE_SESSIONS,
    _CREATE_ACTIVITY,
    _CREATE_ACTIVITY_DATE_INDEX,
    _CREATE_ACTIVITY_EXT_INDEX,
    nut.CREATE_CACHE_TABLE,
]


def _connect() -> sqlite3.Connection:
    """Соединение с БД.

    WAL обязателен: в этот же файл пишет food-api из meow_food, обслуживающий
    Custom GPT. Без WAL два одновременных INSERT дают "database is locked".
    journal_mode персистентен для файла, busy_timeout — свойство соединения,
    поэтому ставится каждый раз.
    """
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_schema() -> None:
    """Идемпотентно. Вызывается на старте приложения, не на каждом запросе."""
    conn = _connect()
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _get_db() -> sqlite3.Connection:
    return _connect()


def _same(presented: str, expected: str) -> bool:
    """Сравнение за постоянное время.

    Обычное `!=` выходит на первом различающемся байте, и время ответа
    подсказывает, сколько символов угадано. Пока ключ жил только в заголовке
    от Shortcut, это было теоретизированием; с кукой поверхность стала
    браузерной, и экономить тут больше не на чем.
    """
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _session_exists(token: str) -> bool:
    """Живая ли сессия.

    Срок проверяется на сервере, а не только через max-age куки: max-age —
    это просьба к браузеру, а не ограничение. Токен, снятый с устройства,
    иначе оставался бы действительным вечно.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=SESSION_MAX_AGE)).isoformat()
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE token = ? AND created_at > ?", (token, cutoff)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _check_key(
    x_api_key: str | None = Header(None),
    # alias, а не имя параметра: иначе переименование SESSION_COOKIE меняло бы
    # только запись куки, а читалась бы она по-прежнему как "session" — и
    # каждый вход молча создавал бы мёртвую сессию.
    session: str | None = Cookie(None, alias=SESSION_COOKIE),
):
    """Заголовок или кука сессии.

    Заголовок остаётся ровно тем же, что был, — Shortcut и garmin-sync не
    замечают изменения. Кука добавлена для webapp: положить ключ в localStorage
    нельзя, Safari чистит script-writable storage при простое.
    """
    if not API_KEY:
        raise HTTPException(500, "API key not configured on server")
    if x_api_key is not None:
        if not _same(x_api_key, API_KEY):
            raise HTTPException(401, "Invalid API key")
        return
    if session is not None:
        if not _session_exists(session):
            raise HTTPException(401, "Session expired or revoked")
        return
    raise HTTPException(401, "Provide the X-API-Key header or sign in")


def _today() -> str:
    return datetime.now(UTC).astimezone(USER_TZ).date().isoformat()


def _validate_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"Invalid date '{value}', expected YYYY-MM-DD") from None
    return value


def _parse_consumed_at(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"Invalid consumed_at '{value}', expected ISO 8601") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=USER_TZ)
    return dt.astimezone(UTC)


def _item_out(row: sqlite3.Row) -> dict:
    out = {
        "log_id": row["log_id"],
        "name": row["name"],
        "quantity": row["quantity"],
        "unit": row["unit"],
    }
    for key in NUTRIENTS:
        out[key] = round(row[key] or 0, 1)
    return out


def _day_summary(conn: sqlite3.Connection, day: str) -> dict:
    rows = conn.execute(
        """
        SELECT l.id AS log_id, l.consumed_at, l.meal_type,
               i.name, i.quantity, i.unit,
               i.calories_kcal, i.protein_g, i.fat_total_g, i.fat_saturated_g,
               i.carbs_g, i.fiber_g, i.sugar_g, i.sodium_mg, i.caffeine_mg
        FROM food_logs l
        JOIN food_items i ON i.log_id = l.id
        WHERE l.log_date = ?
        ORDER BY l.consumed_at, i.id
        """,
        (day,),
    ).fetchall()

    totals = {key: 0.0 for key in NUTRIENTS}
    meals: dict[str, dict] = {}
    log_ids = set()
    for row in rows:
        log_ids.add(row["log_id"])
        meal = meals.setdefault(
            row["meal_type"],
            {"meal_type": row["meal_type"], **{k: 0.0 for k in MACRO_KEYS}, "items": []},
        )
        meal["items"].append(_item_out(row))
        for key in NUTRIENTS:
            totals[key] += row[key] or 0
        for key in MACRO_KEYS:
            meal[key] += row[key] or 0

    for meal in meals.values():
        for key in MACRO_KEYS:
            meal[key] = round(meal[key], 1)

    summary = {
        "date": day,
        "totals": {key: round(value, 1) for key, value in totals.items()},
        "meals": [meals[m] for m in MEAL_ORDER if m in meals],
        "log_count": len(log_ids),
        "item_count": len(rows),
    }

    acts = conn.execute(
        "SELECT * FROM activity_logs WHERE log_date = ? ORDER BY performed_at, id",
        (day,),
    ).fetchall()
    burned = sum(a["calories_kcal"] or 0 for a in acts)
    summary["activity"] = {
        "burned_kcal": round(burned, 1),
        "items": [
            {
                "activity_id": a["id"],
                "name": a["name"],
                "activity_type": a["activity_type"],
                "duration_min": a["duration_min"],
                "calories_kcal": round(a["calories_kcal"] or 0, 1),
                "source": a["source"],
            }
            for a in acts
        ],
    }

    targets = _get_targets(conn)
    if targets:
        summary["targets"] = targets
        summary["remaining"] = {
            key: round(targets[key] - totals[key], 1)
            for key in TARGET_KEYS
            if targets.get(key) is not None
        }
        # per-nutrient progress: everything the GPT needs to render one
        # consumed/target/percent line per limit that is actually set
        summary["progress"] = {
            key: {
                "consumed": round(totals[key], 1),
                "target": round(targets[key], 1),
                "remaining": round(targets[key] - totals[key], 1),
                "used_percent": round(totals[key] / targets[key] * 100, 1),
            }
            for key in TARGET_KEYS
            if targets.get(key)
        }
        if targets.get("calories_kcal"):
            summary["kcal_used_percent"] = round(
                totals["calories_kcal"] / targets["calories_kcal"] * 100, 1
            )
            budget = targets["calories_kcal"] + burned
            summary["remaining_with_activity"] = round(budget - totals["calories_kcal"], 1)
            summary["kcal_used_percent_with_activity"] = round(
                totals["calories_kcal"] / budget * 100, 1
            )
    return summary


TARGET_KEYS = ["calories_kcal", "protein_g", "fat_total_g", "carbs_g"]


def _get_targets(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM targets WHERE id = 1").fetchone()
    if row is None:
        return None
    return {key: row[key] for key in TARGET_KEYS + ["weight_kg"]}


class FoodItemIn(BaseModel):
    name: str
    quantity: float = Field(..., gt=0)
    unit: str
    calories_kcal: float = Field(..., ge=0)
    # macros are required on purpose: otherwise the GPT skips estimating them
    protein_g: float = Field(..., ge=0)
    fat_total_g: float = Field(..., ge=0)
    fat_saturated_g: float = Field(0, ge=0)
    carbs_g: float = Field(..., ge=0)
    fiber_g: float = Field(0, ge=0)
    sugar_g: float = Field(0, ge=0)
    sodium_mg: float = Field(0, ge=0)
    caffeine_mg: float = Field(0, ge=0)


class TargetsIn(BaseModel):
    calories_kcal: float | None = Field(None, gt=0)
    protein_g: float | None = Field(None, gt=0)
    fat_total_g: float | None = Field(None, gt=0)
    carbs_g: float | None = Field(None, gt=0)
    weight_kg: float | None = Field(None, gt=0)


class FoodLogIn(BaseModel):
    items: list[FoodItemIn] = Field(..., min_length=1)
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] = "snack"
    note: str = ""
    source: Literal["text", "voice", "photo"] = "text"
    consumed_at: str | None = None


class LoginIn(BaseModel):
    key: str


# Счётчик в памяти процесса, а не в базе: перезапуск сбрасывает его, и это
# приемлемо — защита здесь от перебора, а не от терпеливого противника.
# Ключ — адрес клиента, значение — (число неудач, время последней).
_login_failures: dict[str, tuple[int, float]] = {}


def _client_id(request: Request) -> str:
    """Кого считать одним клиентом для счётчика попыток.

    Без TRUST_CLIENT_IP_HEADER — адрес сокета. Это верно при прямом доступе и
    вырождается в общий счётчик за прокси: неприятно (можно заблокировать себя),
    но безопасно. Доверять заголовку по умолчанию было бы хуже: его подставляет
    сам клиент, и счётчик обходится сменой значения на каждой попытке.
    """
    if TRUST_CLIENT_IP_HEADER:
        value = request.headers.get(TRUST_CLIENT_IP_HEADER)
        if value:
            # X-Forwarded-For — список; наш прокси дописывает адрес клиента
            # первым слева.
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_blocked(client: str) -> int:
    """Сколько секунд осталось ждать. 0 — можно пробовать."""
    failures, last = _login_failures.get(client, (0, 0.0))
    if failures < LOGIN_MAX_FAILURES:
        return 0
    left = LOGIN_LOCKOUT_SECONDS - (time.monotonic() - last)
    if left <= 0:
        _login_failures.pop(client, None)
        return 0
    return int(left) + 1


def _note_login_failure(client: str) -> None:
    failures, _ = _login_failures.get(client, (0, 0.0))
    _login_failures[client] = (failures + 1, time.monotonic())


@app.post("/auth/login")
def login(payload: LoginIn, response: Response, request: Request):
    """Обменять ключ на куку сессии.

    В куке лежит случайный токен, а не сам FOOD_API_KEY. Разница практическая:
    утёкшую куку можно отозвать через /auth/logout, не трогая ключ, которым
    ходят Shortcut и garmin-sync. Положи мы в куку ключ — отзыв означал бы
    ротацию ключа и поломку обоих.
    """
    if not API_KEY:
        raise HTTPException(500, "API key not configured on server")

    client = _client_id(request)
    wait = _login_blocked(client)
    if wait:
        raise HTTPException(429, f"Too many attempts, try again in {wait}s")

    if not _same(payload.key, API_KEY):
        _note_login_failure(client)
        raise HTTPException(401, "Invalid API key")
    _login_failures.pop(client, None)

    token = secrets.token_urlsafe(32)
    conn = _get_db()
    try:
        conn.execute(
            "DELETE FROM sessions WHERE created_at <= ?",
            ((datetime.now(UTC) - timedelta(seconds=SESSION_MAX_AGE)).isoformat(),),
        )
        conn.execute(
            "INSERT INTO sessions (token, created_at) VALUES (?, ?)",
            (token, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,  # из JS не прочитать — при XSS куку не украсть
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return {"status": "ok"}


@app.post("/auth/logout")
def logout(response: Response, session: str | None = Cookie(None, alias=SESSION_COOKIE)):
    """Отозвать сессию. Без куки — не ошибка: выход и так состоялся."""
    if session is not None:
        conn = _get_db()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (session,))
            conn.commit()
        finally:
            conn.close()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@app.get("/auth/status", dependencies=[Depends(_check_key)])
def auth_status():
    """Живая ли сессия. Фронтенду нужно знать, показывать ли экран входа."""
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok", "today": _today(), "timezone": str(USER_TZ)}


@app.post("/logs", dependencies=[Depends(_check_key)])
def create_log(payload: FoodLogIn):
    now_utc = datetime.now(UTC)
    consumed = _parse_consumed_at(payload.consumed_at) if payload.consumed_at else now_utc
    log_date = consumed.astimezone(USER_TZ).date().isoformat()

    conn = _get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO food_logs (created_at, consumed_at, log_date, meal_type, note, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                now_utc.isoformat(),
                consumed.isoformat(),
                log_date,
                payload.meal_type,
                payload.note,
                payload.source,
            ),
        )
        log_id = cur.lastrowid
        columns = ", ".join(NUTRIENTS)
        placeholders = ", ".join("?" for _ in NUTRIENTS)
        for item in payload.items:
            conn.execute(
                f"""
                INSERT INTO food_items (log_id, name, quantity, unit, {columns})
                VALUES (?, ?, ?, ?, {placeholders})
                """,
                (
                    log_id,
                    item.name,
                    item.quantity,
                    item.unit,
                    *(getattr(item, key) for key in NUTRIENTS),
                ),
            )
        conn.commit()
        return {
            "log_id": log_id,
            "log_date": log_date,
            "meal_type": payload.meal_type,
            "items_saved": len(payload.items),
            "day": _day_summary(conn, log_date),
        }
    finally:
        conn.close()


class RecognizeIn(BaseModel):
    text: str | None = None
    image_b64: str | None = None
    media_type: str = "image/jpeg"
    # Если пользователь сам выбрал приём пищи — его выбор важнее догадки модели.
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    consumed_at: str | None = None


def _get_recognizer() -> rec.Recognizer:
    """Собрать распознаватель при первом обращении, а не на старте.

    Ошибка в RECOGNIZE_PROVIDER не должна ронять приложение целиком: в эту же
    базу через /logs и /day ходит Custom GPT, которому распознавание не нужно.
    Плохая конфигурация превращается в 503 на одном эндпоинте, а не в
    недоступность всего сервиса.
    """
    if app.state.recognizer is None:
        try:
            app.state.recognizer = rec.build_recognizer()
        except ValueError as exc:
            raise HTTPException(503, f"Recognition is not configured: {exc}") from None
    return app.state.recognizer


@app.post("/recognize", dependencies=[Depends(_check_key)])
async def recognize_meal(payload: RecognizeIn):
    """Распознать еду и проставить БЖУ. Ничего не сохраняет.

    Возвращает черновик в форме, которую принимает POST /logs: отправляет его
    человек, после того как посмотрел. Позиции с needs_manual — там, где
    справочник промолчал; нулей под видом данных в ответе нет.

    Хендлер async, а не sync: вызов модели занимает 5-20 с, и синхронный занял
    бы воркер из общего пула — ровно та проблема, которую MULTIUSER.md §1
    отмечает для пятнадцатисекундного /products.
    """
    if not payload.text and not payload.image_b64:
        raise HTTPException(400, "Need text or image_b64")
    if payload.image_b64 and len(payload.image_b64) > rec.MAX_IMAGE_B64_CHARS:
        raise HTTPException(
            413,
            f"Image is {len(payload.image_b64)} base64 chars,"
            f" limit is {rec.MAX_IMAGE_B64_CHARS}. Downscale before sending.",
        )

    consumed = _parse_consumed_at(payload.consumed_at) if payload.consumed_at else datetime.now(UTC)
    recognizer = _get_recognizer()

    try:
        meal = await recognizer.recognize(
            text=payload.text,
            image_b64=payload.image_b64,
            media_type=payload.media_type,
            now=consumed.astimezone(USER_TZ).isoformat(),
        )
    except rec.RecognizeError as exc:
        # 502, а не 500: отказал апстрим, а не мы. Клиенту стоит повторить.
        raise HTTPException(502, f"Recognition failed: {exc}") from None

    # Соединение синхронное и живёт через await'ы лукапов. Запросы к нему
    # короткие и локальные (кэш), event loop они держат на микросекунды.
    conn = _get_db()
    try:
        resolved = await nut.resolve(
            meal.to_items(),
            conn,
            overrides=app.state.overrides,
            client=app.state.http,
        )
    finally:
        conn.close()

    items = [item.to_payload() for item in resolved]
    return {
        "draft": {
            "items": items,
            "meal_type": payload.meal_type or meal.meal_type,
            "note": meal.notes,
            "source": "photo" if payload.image_b64 else "text",
            "consumed_at": consumed.isoformat(),
        },
        "confidence": meal.confidence,
        "needs_manual": any(item["needs_manual"] for item in items),
        "provider": recognizer.name,
    }


@app.get("/day", dependencies=[Depends(_check_key)])
def get_day(date: str | None = Query(None, description="YYYY-MM-DD, defaults to today")):
    day = _validate_date(date) if date else _today()
    conn = _get_db()
    try:
        return _day_summary(conn, day)
    finally:
        conn.close()


@app.get("/days", dependencies=[Depends(_check_key)])
def get_days(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    _validate_date(start)
    _validate_date(end)
    if start > end:
        raise HTTPException(400, "start must be <= end")

    columns = ", ".join(f"SUM(i.{key}) AS {key}" for key in NUTRIENTS)
    conn = _get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT l.log_date, COUNT(DISTINCT l.id) AS log_count, {columns}
            FROM food_logs l
            JOIN food_items i ON i.log_id = l.id
            WHERE l.log_date BETWEEN ? AND ?
            GROUP BY l.log_date
            ORDER BY l.log_date
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    days = [
        {
            "date": row["log_date"],
            "log_count": row["log_count"],
            **{key: round(row[key] or 0, 1) for key in NUTRIENTS},
        }
        for row in rows
    ]
    tracked = len(days)
    avg_kcal = round(sum(d["calories_kcal"] for d in days) / tracked, 1) if tracked else 0
    return {
        "start": start,
        "end": end,
        "days_tracked": tracked,
        "average_calories_kcal": avg_kcal,
        "days": days,
    }


@app.post("/targets", dependencies=[Depends(_check_key)])
def set_targets(payload: TargetsIn):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Provide at least one target field")
    conn = _get_db()
    try:
        current = _get_targets(conn) or {key: None for key in TARGET_KEYS + ["weight_kg"]}
        current.update(updates)
        conn.execute(
            """
            INSERT INTO targets (id, calories_kcal, protein_g, fat_total_g, carbs_g, weight_kg)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                calories_kcal = excluded.calories_kcal,
                protein_g = excluded.protein_g,
                fat_total_g = excluded.fat_total_g,
                carbs_g = excluded.carbs_g,
                weight_kg = excluded.weight_kg
            """,
            tuple(current[key] for key in TARGET_KEYS + ["weight_kg"]),
        )
        conn.commit()
        return {"targets": current, "day": _day_summary(conn, _today())}
    finally:
        conn.close()


class ActivityIn(BaseModel):
    name: str
    calories_kcal: float = Field(..., ge=0)
    duration_min: float = Field(0, ge=0)
    activity_type: str = ""
    note: str = ""
    performed_at: str | None = None


class GarminActivityIn(BaseModel):
    external_id: str
    name: str
    activity_type: str = ""
    duration_min: float = 0
    calories_kcal: float = 0
    performed_at: str


@app.post("/activities", dependencies=[Depends(_check_key)])
def log_activity(payload: ActivityIn):
    now_utc = datetime.now(UTC)
    performed = _parse_consumed_at(payload.performed_at) if payload.performed_at else now_utc
    log_date = performed.astimezone(USER_TZ).date().isoformat()
    conn = _get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO activity_logs
                (created_at, performed_at, log_date, name, activity_type,
                 duration_min, calories_kcal, note, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual')
            """,
            (
                now_utc.isoformat(),
                performed.isoformat(),
                log_date,
                payload.name,
                payload.activity_type,
                payload.duration_min,
                payload.calories_kcal,
                payload.note,
            ),
        )
        conn.commit()
        return {
            "activity_id": cur.lastrowid,
            "log_date": log_date,
            "day": _day_summary(conn, log_date),
        }
    finally:
        conn.close()


@app.post("/activities/import", dependencies=[Depends(_check_key)])
def import_activities(payload: list[GarminActivityIn]):
    now = datetime.now(UTC).isoformat()
    imported = skipped = 0
    conn = _get_db()
    try:
        for a in payload:
            exists = conn.execute(
                "SELECT 1 FROM activity_logs WHERE external_id = ?", (a.external_id,)
            ).fetchone()
            if exists:
                skipped += 1
                continue
            performed = _parse_consumed_at(a.performed_at)
            conn.execute(
                """
                INSERT INTO activity_logs
                    (created_at, performed_at, log_date, name, activity_type,
                     duration_min, calories_kcal, source, external_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'garmin', ?)
                """,
                (
                    now,
                    performed.isoformat(),
                    performed.astimezone(USER_TZ).date().isoformat(),
                    a.name,
                    a.activity_type,
                    a.duration_min,
                    a.calories_kcal,
                    a.external_id,
                ),
            )
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return {"imported": imported, "skipped": skipped}


@app.delete("/activities/{activity_id}", dependencies=[Depends(_check_key)])
def delete_activity(activity_id: int):
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT log_date FROM activity_logs WHERE id = ?", (activity_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Activity {activity_id} not found")
        conn.execute("DELETE FROM activity_logs WHERE id = ?", (activity_id,))
        conn.commit()
        return {
            "deleted_activity_id": activity_id,
            "day": _day_summary(conn, row["log_date"]),
        }
    finally:
        conn.close()


# --- Apple Health export queue (consumed by an iOS Shortcut, not by the GPT) ---


@app.get("/export/pending", dependencies=[Depends(_check_key)])
def export_pending(limit: int = Query(200, ge=1, le=1000)):
    conn = _get_db()
    try:
        logs = conn.execute(
            """
            SELECT id, created_at, consumed_at, meal_type
            FROM food_logs
            WHERE health_exported_at IS NULL
            ORDER BY created_at, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for log in logs:
            rows = conn.execute(
                "SELECT * FROM food_items WHERE log_id = ? ORDER BY id", (log["id"],)
            ).fetchall()
            # local wall time without offset/microseconds: the only datetime
            # format iOS Shortcuts parses reliably into a correct Date
            consumed_local = (
                datetime.fromisoformat(log["consumed_at"])
                .astimezone(USER_TZ)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            for row in rows:
                item = {
                    "log_id": log["id"],
                    "consumed_at": log["consumed_at"],
                    "consumed_at_local": consumed_local,
                    "meal_type": log["meal_type"],
                    "name": row["name"],
                    "quantity": row["quantity"],
                    "unit": row["unit"],
                }
                for key in NUTRIENTS:
                    item[key] = round(row[key] or 0, 1)
                items.append(item)
    finally:
        conn.close()
    return {
        "count": len(items),
        "ack_token": logs[-1]["created_at"] if logs else None,
        "items": items,
    }


@app.post("/export/ack", dependencies=[Depends(_check_key)])
def export_ack(token: str = Query(..., description="ack_token from /export/pending")):
    # '+' in an unencoded query string arrives as a space - restore it,
    # so the Shortcut does not need to URL-encode the token
    token = token.replace(" ", "+")
    try:
        datetime.fromisoformat(token)
    except ValueError:
        raise HTTPException(400, f"Invalid ack token '{token}'") from None
    now = datetime.now(UTC).isoformat()
    conn = _get_db()
    try:
        cur = conn.execute(
            """
            UPDATE food_logs SET health_exported_at = ?
            WHERE health_exported_at IS NULL AND created_at <= ?
            """,
            (now, token),
        )
        conn.commit()
        return {"acked": cur.rowcount}
    finally:
        conn.close()


OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"


def _off_value(nutriments: dict, key: str, scale: float = 1):
    value = nutriments.get(key)
    if isinstance(value, (int, float)):
        return round(float(value) * scale, 1)
    return None


@app.get("/products", dependencies=[Depends(_check_key)])
def search_products(
    query: str = Query(..., min_length=2, description="Product name, ideally with brand"),
    limit: int = Query(5, ge=1, le=10),
):
    params = urllib.parse.urlencode(
        {
            "search_terms": query,
            "search_simple": 1,
            "action": "process",
            "json": 1,
            "page_size": limit,
            "fields": "product_name,brands,quantity,serving_size,nutriments",
        }
    )
    req = urllib.request.Request(
        f"{OFF_SEARCH_URL}?{params}",
        headers={"User-Agent": "MeowFood/1.0 (personal food tracker)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as exc:
        raise HTTPException(502, f"Open Food Facts unavailable: {exc}") from exc

    products = []
    for p in data.get("products", []):
        n = p.get("nutriments", {})
        per_100g = {
            "calories_kcal": _off_value(n, "energy-kcal_100g"),
            "protein_g": _off_value(n, "proteins_100g"),
            "fat_total_g": _off_value(n, "fat_100g"),
            "fat_saturated_g": _off_value(n, "saturated-fat_100g"),
            "carbs_g": _off_value(n, "carbohydrates_100g"),
            "sugar_g": _off_value(n, "sugars_100g"),
            "fiber_g": _off_value(n, "fiber_100g"),
            "sodium_mg": _off_value(n, "sodium_100g", scale=1000),
        }
        if per_100g["calories_kcal"] is None:
            continue
        products.append(
            {
                "name": p.get("product_name") or "",
                "brands": p.get("brands") or "",
                "package_quantity": p.get("quantity") or "",
                "serving_size": p.get("serving_size") or "",
                "per_100g": per_100g,
            }
        )
    return {"query": query, "count": len(products), "products": products}


@app.delete("/logs/{log_id}", dependencies=[Depends(_check_key)])
def delete_log(log_id: int):
    conn = _get_db()
    try:
        row = conn.execute("SELECT log_date FROM food_logs WHERE id = ?", (log_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"Log {log_id} not found")
        conn.execute("DELETE FROM food_items WHERE log_id = ?", (log_id,))
        conn.execute("DELETE FROM food_logs WHERE id = ?", (log_id,))
        conn.commit()
        return {"deleted_log_id": log_id, "day": _day_summary(conn, row["log_date"])}
    finally:
        conn.close()


# Статика webapp. Путь абсолютный, а не "webapp": рабочий каталог различается
# у uvicorn в контейнере, у локального запуска и у pytest из корня репозитория.
# html=True отдаёт index.html на /app/ — без него был бы 404 на самом входе.
app.mount("/app", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
