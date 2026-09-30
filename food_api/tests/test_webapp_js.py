"""Поведенческие тесты фронтенда, прогоняемые через Node.

Стенд узкий намеренно: DOM заменён заглушкой, поэтому проверяется логика
разметки и разбор ошибок, а не браузер. Вёрстка, камера и service worker
так не проверяются и проверены быть не могут — это остаётся на устройство.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parent.parent / "webapp" / "tests" / "run.mjs"


def test_frontend_logic():
    """Локально без node тест пропускается, в CI — падает.

    Пропуск удобен на машине, где node не нужен, но в CI он означал бы
    тихую потерю всей проверки фронтенда: зелёный прогон без единого
    утверждения о том, что код делает.
    """
    if shutil.which("node") is None:
        if os.getenv("CI"):
            pytest.fail("в CI обязан быть node — см. setup-node в ci.yml")
        pytest.skip("нет node: тесты фронтенда пропущены")

    result = subprocess.run(
        ["node", str(RUNNER)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout, result.stdout
