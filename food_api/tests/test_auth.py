"""Авторизация: заголовок для интеграций, кука сессии для webapp.

Заголовок X-API-Key трогать нельзя — по нему ходят Shortcut и garmin-sync.
Кука добавлена рядом, а не вместо.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient


@pytest.fixture
def insecure_cookies(api, monkeypatch):
    """TestClient ходит по http, а Secure-куку http.cookiejar не сохранит.

    Флаг Secure проверяется отдельным тестом по сырому заголовку Set-Cookie.
    """
    monkeypatch.setattr(api, "COOKIE_SECURE", False)
    return api


@pytest.fixture
def anon(insecure_cookies):
    """Клиент без заголовка: всё держится на куке."""
    with TestClient(insecure_cookies.app) as c:
        yield c


def login(client, key=API_KEY):
    return client.post("/auth/login", json={"key": key})


# --- вход --------------------------------------------------------------------


def test_login_with_the_right_key_opens_a_session(anon):
    assert anon.get("/day").status_code == 401, "до входа доступа нет"
    assert login(anon).status_code == 200
    assert anon.get("/day").status_code == 200, "после входа кука работает сама"


def test_login_with_a_wrong_key_is_401_and_sets_no_cookie(anon):
    resp = login(anon, "не тот ключ")
    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers
    assert anon.get("/day").status_code == 401


def test_cookie_does_not_contain_the_api_key(anon):
    """Смысл отдельного токена: утёкшую куку можно отозвать, не трогая ключ.

    Лежал бы в куке сам FOOD_API_KEY — отзыв означал бы ротацию ключа и
    поломку Shortcut с garmin-sync заодно.
    """
    login(anon)
    token = anon.cookies["session"]
    assert token, "кука не поставлена"
    assert API_KEY not in token
    assert token != API_KEY


def test_forged_token_is_rejected(anon):
    anon.cookies.set("session", "forged-token-i-made-up")
    assert anon.get("/day").status_code == 401


# --- выход -------------------------------------------------------------------


def test_logout_revokes_the_session(anon):
    login(anon)
    assert anon.get("/day").status_code == 200

    assert anon.post("/auth/logout").status_code == 200
    assert anon.get("/day").status_code == 401, "кука должна перестать работать"


def test_logout_revokes_only_its_own_session(insecure_cookies):
    """Выход на телефоне не должен выкидывать из сессии на ноутбуке."""
    app = insecure_cookies.app
    with TestClient(app) as phone, TestClient(app) as laptop:
        login(phone)
        login(laptop)
        phone.post("/auth/logout")

        assert phone.get("/day").status_code == 401
        assert laptop.get("/day").status_code == 200, "чужая сессия не тронута"


def test_logout_without_a_session_is_not_an_error(anon):
    assert anon.post("/auth/logout").status_code == 200


def test_revoked_token_stays_dead_when_replayed(anon):
    """Отзыв — на сервере, а не «браузер забыл куку»."""
    login(anon)
    token = anon.cookies["session"]
    anon.post("/auth/logout")

    anon.cookies.set("session", token)
    assert anon.get("/day").status_code == 401


# --- флаги куки --------------------------------------------------------------


def test_cookie_flags_protect_it(api, monkeypatch):
    # Явно, а не из окружения: .env.example советует COOKIE_SECURE=0 для
    # локальной отладки, и экспортировавший его получал бы красный тест,
    # не имеющий отношения к его правке.
    monkeypatch.setattr(api, "COOKIE_SECURE", True)
    with TestClient(api.app) as c:
        raw = login(c).headers["set-cookie"]

    lowered = raw.lower()
    assert "httponly" in lowered, "из JS не должна читаться — иначе XSS её украдёт"
    assert "secure" in lowered, "по http не отдавать"
    assert "samesite=lax" in raw.lower(), "защита от CSRF"
    assert "path=/" in lowered


def test_secure_flag_can_be_dropped_for_local_http(anon):
    """Локальная отладка по http: иначе браузер куку просто не сохранит."""
    assert "Secure" not in login(anon).headers["set-cookie"]


# --- сосуществование с заголовком -------------------------------------------


def test_header_still_works_without_any_cookie(api):
    with TestClient(api.app) as c:
        resp = c.get("/day", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200, "Shortcut и garmin-sync ходят так"


def test_wrong_header_is_not_rescued_by_a_valid_cookie(anon):
    """Явно переданный неверный заголовок — отказ, а не молчаливый откат к куке."""
    login(anon)
    assert anon.get("/day", headers={"X-API-Key": "wrong"}).status_code == 401


def test_auth_status_reports_the_session(anon):
    assert anon.get("/auth/status").status_code == 401
    login(anon)
    assert anon.get("/auth/status").status_code == 200


# --- срок жизни и чистка -----------------------------------------------------


def stale_session(api, token: str, days_ago: int) -> None:
    """Положить в базу сессию с давним created_at."""
    conn = api._get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (token, created_at) VALUES (?, ?)",
            (token, (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def test_expired_session_is_rejected(anon, insecure_cookies):
    """max-age — просьба к браузеру, а не ограничение.

    Токен, снятый с устройства, без проверки на сервере жил бы вечно.
    """
    stale_session(insecure_cookies, "old-token", days_ago=400)
    anon.cookies.set("session", "old-token")
    assert anon.get("/day").status_code == 401


def test_fresh_session_within_the_window_still_works(anon, insecure_cookies):
    stale_session(insecure_cookies, "recent-token", days_ago=10)
    anon.cookies.set("session", "recent-token")
    assert anon.get("/day").status_code == 200


def test_login_prunes_expired_sessions(anon, insecure_cookies):
    """Таблица лежит в базе, общей с food-api из meow_food: расти ей незачем."""
    stale_session(insecure_cookies, "old-token", days_ago=400)
    login(anon)

    conn = insecure_cookies._get_db()
    try:
        rows = conn.execute("SELECT token FROM sessions").fetchall()
    finally:
        conn.close()
    tokens = {row["token"] for row in rows}
    assert "old-token" not in tokens, "просроченная сессия должна быть удалена"
    assert len(tokens) == 1, "осталась ровно новая"


# --- ограничение подбора -----------------------------------------------------


def test_login_locks_out_after_repeated_failures(anon):
    """Вход достижим из любого браузера через туннель; без счётчика ключ
    подбирают без ограничений по скорости."""
    for _ in range(5):
        assert login(anon, "wrong").status_code == 401

    resp = login(anon, "wrong")
    assert resp.status_code == 429
    assert "try again" in resp.json()["detail"]


def test_lockout_applies_to_the_right_key_too(anon):
    """Иначе блокировка обходится подстановкой верного ключа на шестой попытке."""
    for _ in range(5):
        login(anon, "wrong")
    assert login(anon).status_code == 429


def test_successful_login_clears_the_counter(anon):
    for _ in range(4):
        login(anon, "wrong")
    assert login(anon).status_code == 200

    for _ in range(4):
        assert login(anon, "wrong").status_code == 401, "счётчик должен был обнулиться"


# --- имя куки ----------------------------------------------------------------


def test_cookie_name_is_the_same_on_write_and_read(anon, insecure_cookies):
    """Константа должна быть нагруженной с обеих сторон.

    Читалась бы кука по имени параметра, а писалась по константе — и
    переименование давало бы молча мёртвые сессии при каждом входе.
    """
    name = insecure_cookies.SESSION_COOKIE
    raw = login(anon).headers["set-cookie"]
    assert raw.startswith(f"{name}="), raw

    token = anon.cookies[name]
    with TestClient(insecure_cookies.app) as fresh:
        fresh.cookies.set(name, token)
        assert fresh.get("/day").status_code == 200


# --- счётчик за прокси (§2.6) ------------------------------------------------


def test_clients_share_a_bucket_behind_a_proxy_by_default(anon, insecure_cookies):
    """Без настройки все — один клиент: за туннелем адрес сокета общий.

    Это вырождение, а не поломка: заблокировать можно себя, но счётчик
    остаётся неподделываемым.
    """
    assert insecure_cookies.TRUST_CLIENT_IP_HEADER == "", "по умолчанию заголовку не доверяем"
    for _ in range(5):
        login(anon, "wrong")
    # Другой «клиент» с подставленным заголовком блокировку не обходит
    resp = anon.post("/auth/login", json={"key": API_KEY}, headers={"CF-Connecting-IP": "10.0.0.7"})
    assert resp.status_code == 429


def test_named_header_separates_clients(anon, insecure_cookies, monkeypatch):
    monkeypatch.setattr(insecure_cookies, "TRUST_CLIENT_IP_HEADER", "CF-Connecting-IP")

    for _ in range(5):
        anon.post("/auth/login", json={"key": "wrong"}, headers={"CF-Connecting-IP": "10.0.0.1"})

    blocked = anon.post(
        "/auth/login", json={"key": "wrong"}, headers={"CF-Connecting-IP": "10.0.0.1"}
    )
    assert blocked.status_code == 429, "перебиравший должен быть заблокирован"

    other = anon.post(
        "/auth/login", json={"key": API_KEY}, headers={"CF-Connecting-IP": "10.0.0.2"}
    )
    assert other.status_code == 200, "чужая блокировка не должна мешать входу"


def test_forwarded_for_takes_the_leftmost_address(anon, insecure_cookies, monkeypatch):
    """X-Forwarded-For — список; наш прокси дописывает клиента слева."""
    monkeypatch.setattr(insecure_cookies, "TRUST_CLIENT_IP_HEADER", "X-Forwarded-For")
    chain = {"X-Forwarded-For": "10.0.0.1, 172.16.0.1, 192.168.1.1"}

    for _ in range(5):
        anon.post("/auth/login", json={"key": "wrong"}, headers=chain)

    same = anon.post("/auth/login", json={"key": API_KEY}, headers=chain)
    assert same.status_code == 429

    # Хвост цепочки тот же — меняется только клиент слева. Если брать адрес
    # справа, ключом станет общий прокси и разные клиенты сольются в один.
    other = anon.post(
        "/auth/login",
        json={"key": API_KEY},
        headers={"X-Forwarded-For": "10.0.0.9, 172.16.0.1, 192.168.1.1"},
    )
    assert other.status_code == 200, "клиента различает левый адрес, а не прокси справа"
