"""Авторизация: заголовок для интеграций, кука сессии для webapp.

Заголовок X-API-Key трогать нельзя — по нему ходят Shortcut и garmin-sync.
Кука добавлена рядом, а не вместо.
"""

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


def test_cookie_flags_protect_it(api):
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
