"""Границы суток и часовые пояса — место, где легко потерять приём пищи."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException


def test_naive_time_is_read_in_user_tz_and_stored_as_utc(api):
    """Наивное время трактуется в USER_TZ (Москва, UTC+3) и хранится в UTC."""
    dt = api._parse_consumed_at("2026-09-19T23:30:00")
    assert dt.tzinfo is not None
    assert dt.isoformat() == "2026-09-19T20:30:00+00:00"


def test_explicit_offset_is_respected(api):
    """Если смещение указано явно, USER_TZ не навязывается."""
    dt = api._parse_consumed_at("2026-09-19T23:30:00+05:00")
    assert dt.isoformat() == "2026-09-19T18:30:00+00:00"


def test_late_dinner_stays_in_its_own_day(log_food):
    """Поздний ужин не утекает в следующие сутки.

    23:30 по Москве = 20:30 UTC. Если бы log_date считался по UTC-дате,
    всё бы сошлось случайно — поэтому берём случай, где UTC-дата отличается.
    """
    body = log_food(consumed_at="2026-09-19T23:30:00", calories_kcal=500)
    assert body["log_date"] == "2026-09-19"


def test_after_midnight_utc_still_previous_local_day(log_food):
    """01:30 по Москве 20-го = 22:30 UTC 19-го. Локальная дата должна победить."""
    body = log_food(consumed_at="2026-09-20T01:30:00", calories_kcal=300)
    assert body["log_date"] == "2026-09-20"


def test_etc_gmt_sign_is_inverted(api, monkeypatch):
    """Ловушка из .env.example: в зонах Etc/* знак инвертирован.

    Etc/GMT-4 — это UTC+4, а не UTC−4. Тест фиксирует это как поведение,
    чтобы никто не «исправил» конфиг, сместив все сутки на 8 часов.
    """
    monkeypatch.setattr(api, "USER_TZ", ZoneInfo("Etc/GMT-4"))
    dt = api._parse_consumed_at("2026-09-19T12:00:00")
    assert dt.isoformat() == "2026-09-19T08:00:00+00:00"


def test_today_uses_user_tz(api, monkeypatch):
    """_today() считает дату в USER_TZ, а не в UTC."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 22:30 UTC 19 сентября = 01:30 по Москве 20-го
            return datetime(2026, 9, 19, 22, 30, tzinfo=ZoneInfo("UTC"))

    monkeypatch.setattr(api, "datetime", FrozenDatetime)
    assert api._today() == "2026-09-20"


@pytest.mark.parametrize("bad", ["19-09-2026", "2026-13-01", "вчера", ""])
def test_validate_date_rejects_garbage(api, bad):
    with pytest.raises(HTTPException) as exc:
        api._validate_date(bad)
    assert exc.value.status_code == 400


def test_validate_date_accepts_iso(api):
    assert api._validate_date("2026-09-19") == "2026-09-19"


def test_parse_consumed_at_rejects_garbage(api):
    with pytest.raises(HTTPException) as exc:
        api._parse_consumed_at("позавчера вечером")
    assert exc.value.status_code == 400
