"""Отдача webapp и целостность его оболочки.

Сборки нет, поэтому ломаться будут не типы, а пути: ссылка в index.html или
запись в списке кэша service worker переживут переименование файла молча.
Здесь это ловится.
"""

import json
import re
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEBAPP = Path(__file__).resolve().parent.parent / "webapp"


# --- отдача ------------------------------------------------------------------


def test_app_root_serves_index(api):
    with TestClient(api.app) as anon:
        resp = anon.get("/app/")
    assert resp.status_code == 200
    assert "<title>Еда</title>" in resp.text


def test_shell_needs_no_auth(api):
    """Экран входа обязан открываться без учётных данных — иначе войти нечем."""
    with TestClient(api.app) as anon:
        for path in ("/app/", "/app/app.js", "/app/styles.css"):
            assert anon.get(path).status_code == 200, path


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/app/styles.css", "text/css"),
        ("/app/app.js", "javascript"),
        ("/app/api.js", "javascript"),
        ("/app/sw.js", "javascript"),
        ("/app/icons/icon-192.png", "image/png"),
    ],
)
def test_static_types(api, path, content_type):
    """ES-модуль с неверным Content-Type браузер откажется исполнять."""
    with TestClient(api.app) as anon:
        resp = anon.get(path)
    assert resp.status_code == 200, path
    assert content_type in resp.headers["content-type"], resp.headers["content-type"]


def test_api_still_wins_over_static(client):
    """Монтирование статики не должно перекрывать эндпоинты."""
    assert client.get("/day").status_code == 200
    assert client.get("/health").status_code == 200


# --- целостность оболочки ----------------------------------------------------


def local_refs(text: str) -> set[str]:
    """Ссылки на свои файлы: href/src в HTML и строки в списке кэша sw.js."""
    found = set()
    for match in re.finditer(r'(?:href|src)="([^"]+)"', text):
        found.add(match.group(1))
    return {r for r in found if not r.startswith(("http", "//", "#", "data:"))}


def test_index_references_existing_files():
    for ref in local_refs((WEBAPP / "index.html").read_text(encoding="utf-8")):
        assert (WEBAPP / ref).exists(), f"index.html ссылается на несуществующий {ref}"


def test_service_worker_caches_only_existing_files():
    """cache.addAll отвергает весь список, если хоть один путь даёт 404.

    Отваливается при этом не отдельный файл, а кэширование целиком — и молча:
    приложение продолжает работать, просто перестаёт открываться офлайн.
    """
    source = (WEBAPP / "sw.js").read_text(encoding="utf-8")
    shell = re.search(r"const SHELL = \[(.*?)\];", source, re.DOTALL)
    assert shell, "список SHELL в sw.js не найден"

    entries = re.findall(r"'([^']+)'", shell.group(1))
    assert entries, "список SHELL пуст"
    for entry in entries:
        rel = entry.removeprefix("./")
        target = WEBAPP / rel if rel else WEBAPP / "index.html"
        assert target.exists(), f"sw.js кэширует несуществующий {entry}"


def test_manifest_is_valid_and_icons_exist():
    manifest = json.loads((WEBAPP / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone", "иначе запустится с адресной строкой"
    assert manifest["icons"], "без иконок на экран «Домой» не поставить"
    for icon in manifest["icons"]:
        assert (WEBAPP / icon["src"]).exists(), icon["src"]


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} не PNG"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize(
    ("name", "expected"),
    [("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)],
)
def test_icons_are_png_of_the_declared_size(name, expected):
    assert png_size(WEBAPP / "icons" / name) == (expected, expected)


def test_dockerfile_ships_the_webapp():
    """Без этой строки образ поднимется, а /app/ отдаст 404."""
    dockerfile = (WEBAPP.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY webapp/" in dockerfile


def test_module_imports_resolve():
    """Без сборщика опечатку в пути импорта не поймает никто.

    Браузер просто не исполнит модуль, и приложение останется пустым экраном.
    """
    for source in WEBAPP.glob("*.js"):
        text = source.read_text(encoding="utf-8")
        specs = re.findall(r"""import\s+(?:.*?\s+from\s+)?['"](\./[^'"]+)['"]""", text, re.DOTALL)
        for spec in specs:
            target = (source.parent / spec).resolve()
            assert target.exists(), f"{source.name} импортирует несуществующий {spec}"


def test_every_element_id_used_by_app_exists_in_html():
    """$('btn-save') по несуществующему id вернёт null.

    Дальше addEventListener роняет весь модуль на загрузке — молча, до того
    как что-либо отрисуется.
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    present = set(re.findall(r'id="([^"]+)"', html))

    for source in (WEBAPP / "app.js",):
        used = set(re.findall(r"\$\('([^']+)'\)", source.read_text(encoding="utf-8")))
        missing = used - present
        assert not missing, f"{source.name} обращается к отсутствующим id: {sorted(missing)}"


def test_draft_item_fields_match_the_api_contract():
    """Экран правки редактирует ровно те поля, которые принимает POST /logs."""
    import api as api_module

    macros = re.search(
        r"const MACROS = \[(.*?)\];", (WEBAPP / "render.js").read_text("utf-8"), re.DOTALL
    )
    assert macros, "список MACROS в render.js не найден"

    keys = re.findall(r"\['([a-z_]+)'", macros.group(1))
    unknown = [k for k in keys if k not in api_module.NUTRIENTS]
    assert not unknown, f"render.js правит поля, которых нет в схеме: {unknown}"
