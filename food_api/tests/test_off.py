"""Нормализация нутриентов Open Food Facts."""

import pytest


def test_sodium_is_scaled_from_grams_to_milligrams(api):
    """OFF отдаёт натрий в граммах, схема хранит в мг — множитель 1000."""
    assert api._off_value({"sodium_100g": 0.65}, "sodium_100g", scale=1000) == 650.0


def test_missing_key_is_none_not_zero(api):
    """None и 0 — разные вещи: «нет данных» не должно выглядеть как «ноль соли»."""
    assert api._off_value({}, "proteins_100g") is None


def test_non_numeric_is_none(api):
    """OFF местами кладёт в числовые поля строки."""
    assert api._off_value({"proteins_100g": "н/д"}, "proteins_100g") is None
    assert api._off_value({"proteins_100g": None}, "proteins_100g") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(12.345, 12.3), (12.35, 12.3), (0, 0.0), (7, 7.0)],
)
def test_values_are_rounded_to_one_decimal(api, raw, expected):
    assert api._off_value({"k": raw}, "k") == expected
