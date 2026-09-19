"""Режим БД: WAL и параллельная запись.

База общая с food-api из meow_food, обслуживающим Custom GPT: в один файл
пишут два процесса. Без WAL два одновременных INSERT дают "database is locked".
"""

import sqlite3
import threading


def test_journal_mode_is_wal(api, client):
    conn = api._get_db()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_busy_timeout_is_set(api, client):
    conn = api._get_db()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_schema_exists_after_startup_without_any_request(api):
    """Схему создаёт lifespan, а не первый запрос."""
    from fastapi.testclient import TestClient

    with TestClient(api.app):
        pass  # ни одного запроса не сделали

    conn = sqlite3.connect(api.DB_PATH)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"food_logs", "food_items", "targets", "activity_logs"} <= tables


def test_reader_is_not_blocked_by_open_writer(api, client, log_food):
    """WAL: читатель видит данные, пока писатель держит открытую транзакцию."""
    log_food(calories_kcal=500)

    writer = api._get_db()
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO food_logs (created_at, consumed_at, log_date, meal_type, note, source)"
            " VALUES ('x', 'x', '2026-01-01', 'snack', '', 'text')"
        )
        # читаем другим соединением, пока писатель не закоммитил
        assert client.get("/day").json()["totals"]["calories_kcal"] == 500
    finally:
        writer.rollback()
        writer.close()


def test_parallel_writes_do_not_deadlock(api, client, log_food):
    """Восемь одновременных записей из разных потоков проходят без блокировки."""
    errors = []

    def write(n):
        try:
            log_food(calories_kcal=100, name=f"еда-{n}")
        except Exception as exc:  # noqa: BLE001 — нужен любой сбой, включая sqlite3.OperationalError
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"параллельная запись сломалась: {errors}"
    assert client.get("/day").json()["totals"]["calories_kcal"] == 800
