"""Агрегация дня: суммы, группировка по приёмам, прогресс по лимитам."""


def test_totals_sum_all_nine_nutrients(client, log_food, api):
    log_food(
        calories_kcal=500,
        protein_g=30,
        fat_total_g=20,
        carbs_g=50,
        fat_saturated_g=5,
        fiber_g=7,
        sugar_g=12,
        sodium_mg=800,
        caffeine_mg=0,
    )
    log_food(
        calories_kcal=250,
        protein_g=10,
        fat_total_g=5,
        carbs_g=40,
        fat_saturated_g=1,
        fiber_g=3,
        sugar_g=20,
        sodium_mg=200,
        caffeine_mg=80,
    )
    totals = client.get("/day").json()["totals"]

    assert set(totals) == set(api.NUTRIENTS), "в totals должны быть все 9 нутриентов"
    assert totals["calories_kcal"] == 750
    assert totals["protein_g"] == 40
    assert totals["sodium_mg"] == 1000
    assert totals["caffeine_mg"] == 80


def test_meals_are_ordered_by_meal_order_not_insertion(client, log_food, api):
    """Порядок приёмов пищи задан MEAL_ORDER, а не порядком записи."""
    log_food(meal_type="dinner", calories_kcal=700)
    log_food(meal_type="breakfast", calories_kcal=300)
    log_food(meal_type="snack", calories_kcal=100)

    meals = [m["meal_type"] for m in client.get("/day").json()["meals"]]
    assert meals == ["breakfast", "dinner", "snack"]
    assert meals == [m for m in api.MEAL_ORDER if m in meals]


def test_meal_carries_macro_subtotals(client, log_food):
    log_food(meal_type="lunch", calories_kcal=400, protein_g=25)
    log_food(meal_type="lunch", calories_kcal=100, protein_g=5)

    lunch = client.get("/day").json()["meals"][0]
    assert lunch["calories_kcal"] == 500
    assert lunch["protein_g"] == 30
    assert len(lunch["items"]) == 2


def test_no_progress_without_targets(client, log_food):
    log_food(calories_kcal=500)
    day = client.get("/day").json()
    assert "progress" not in day
    assert "targets" not in day


def test_progress_computed_per_nutrient(client, log_food):
    client.post("/targets", json={"calories_kcal": 2000, "protein_g": 150})
    log_food(calories_kcal=500, protein_g=30)

    progress = client.get("/day").json()["progress"]
    assert progress["calories_kcal"] == {
        "consumed": 500.0,
        "target": 2000.0,
        "remaining": 1500.0,
        "used_percent": 25.0,
    }
    assert progress["protein_g"]["used_percent"] == 20.0
    # лимиты не заданы -> в progress не попадают
    assert "fat_total_g" not in progress
    assert "carbs_g" not in progress


def test_progress_remaining_goes_negative_on_overshoot(client, log_food):
    client.post("/targets", json={"calories_kcal": 2000})
    log_food(calories_kcal=2300)

    progress = client.get("/day").json()["progress"]["calories_kcal"]
    assert progress["remaining"] == -300.0
    assert progress["used_percent"] == 115.0


def test_activity_extends_the_calorie_budget(client, log_food):
    """remaining_with_activity = норма + сожжено − съедено."""
    client.post("/targets", json={"calories_kcal": 2000})
    log_food(calories_kcal=1800)
    client.post("/activities", json={"name": "БЖЖ", "calories_kcal": 600, "duration_min": 60})

    day = client.get("/day").json()
    assert day["activity"]["burned_kcal"] == 600
    assert day["remaining"]["calories_kcal"] == 200  # без учёта тренировки
    assert day["remaining_with_activity"] == 800  # 2000 + 600 − 1800
    assert day["kcal_used_percent"] == 90.0
    assert day["kcal_used_percent_with_activity"] == 69.2  # 1800 / 2600


def test_empty_day_is_all_zeros(client):
    day = client.get("/day?date=2020-01-01").json()
    assert day["totals"]["calories_kcal"] == 0
    assert day["meals"] == []
    assert day["log_count"] == 0
    assert day["activity"]["burned_kcal"] == 0
