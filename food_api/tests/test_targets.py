"""Цели: частичное обновление не должно затирать соседние поля."""


def test_partial_update_keeps_other_fields(client):
    client.post("/targets", json={"calories_kcal": 2300, "protein_g": 160, "weight_kg": 82})
    # трогаем только белок
    targets = client.post("/targets", json={"protein_g": 180}).json()["targets"]

    assert targets["protein_g"] == 180
    assert targets["calories_kcal"] == 2300, "калории не должны были обнулиться"
    assert targets["weight_kg"] == 82


def test_first_write_leaves_unset_fields_none(client):
    targets = client.post("/targets", json={"calories_kcal": 2000}).json()["targets"]
    assert targets["calories_kcal"] == 2000
    assert targets["protein_g"] is None
    assert targets["weight_kg"] is None


def test_empty_payload_rejected(client):
    assert client.post("/targets", json={}).status_code == 400


def test_non_positive_values_rejected(client):
    assert client.post("/targets", json={"calories_kcal": 0}).status_code == 422
    assert client.post("/targets", json={"protein_g": -5}).status_code == 422


def test_response_carries_fresh_day_summary(client, log_food):
    log_food(calories_kcal=500)
    body = client.post("/targets", json={"calories_kcal": 2000}).json()
    assert body["day"]["progress"]["calories_kcal"]["remaining"] == 1500
