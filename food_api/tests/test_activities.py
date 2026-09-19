"""Импорт тренировок из Garmin: дедуп по external_id."""

ACTIVITY = {
    "external_id": "garmin-12345",
    "name": "Бег",
    "activity_type": "running",
    "duration_min": 45,
    "calories_kcal": 520,
    "performed_at": "2026-09-19T07:00:00",
}


def test_import_inserts_new_activity(client):
    body = client.post("/activities/import", json=[ACTIVITY]).json()
    assert body["imported"] == 1
    assert body["skipped"] == 0


def test_reimport_is_deduped_by_external_id(client):
    client.post("/activities/import", json=[ACTIVITY])
    body = client.post("/activities/import", json=[ACTIVITY]).json()

    assert body["imported"] == 0
    assert body["skipped"] == 1, "повторный импорт не должен плодить дубли"

    day = client.get("/day?date=2026-09-19").json()
    assert len(day["activity"]["items"]) == 1
    assert day["activity"]["burned_kcal"] == 520


def test_dedup_within_a_single_batch(client):
    """Тот же external_id дважды в одном запросе — вставка одна."""
    body = client.post("/activities/import", json=[ACTIVITY, ACTIVITY]).json()
    assert (body["imported"], body["skipped"]) == (1, 1)


def test_distinct_external_ids_both_land(client):
    second = {**ACTIVITY, "external_id": "garmin-99999", "calories_kcal": 300}
    body = client.post("/activities/import", json=[ACTIVITY, second]).json()
    assert body["imported"] == 2

    day = client.get("/day?date=2026-09-19").json()
    assert day["activity"]["burned_kcal"] == 820


def test_imported_activity_is_marked_as_garmin(client):
    """source='garmin' — по нему промпт отличает автоимпорт от ручной записи."""
    client.post("/activities/import", json=[ACTIVITY])
    item = client.get("/day?date=2026-09-19").json()["activity"]["items"][0]
    assert item["source"] == "garmin"


def test_manual_activity_is_not_garmin(client):
    client.post("/activities", json={"name": "Зал", "calories_kcal": 200, "duration_min": 40})
    item = client.get("/day").json()["activity"]["items"][0]
    assert item["source"] == "manual"


def test_manual_activity_can_be_deleted(client):
    client.post("/activities", json={"name": "Зал", "calories_kcal": 200, "duration_min": 40})
    activity_id = client.get("/day").json()["activity"]["items"][0]["activity_id"]

    assert client.delete(f"/activities/{activity_id}").status_code == 200
    assert client.get("/day").json()["activity"]["burned_kcal"] == 0
