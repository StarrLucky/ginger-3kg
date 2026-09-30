"""Поведенческие тесты фронтенда, прогоняемые через Node.

Стенд узкий намеренно: DOM заменён заглушкой, поэтому проверяется логика
разметки и разбор ошибок, а не браузер. Вёрстка, камера и service worker
так не проверяются и проверены быть не могут — это остаётся на устройство.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parent.parent / "webapp" / "tests" / "run.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
def test_frontend_logic():
    result = subprocess.run(
        ["node", str(RUNNER)], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout, result.stdout
