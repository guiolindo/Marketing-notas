import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from app.services.devices import parse_ua
from tests.conftest import _reset_external_db, login, make_settings

CHROME_ANDROID = "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
EDGE_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
IPAD = "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0"
INSTAGRAM = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Instagram 330.0"
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


@pytest.mark.parametrize("ua,expected", [
    (CHROME_ANDROID, ("Chrome", "Android", "Celular")),
    (IPHONE, ("Safari", "iOS", "Celular")),
    (EDGE_WIN, ("Edge", "Windows", "Computador")),
    (IPAD, ("Safari", "iOS", "Tablet")),
    (FIREFOX, ("Firefox", "Linux", "Computador")),
    (INSTAGRAM, ("Instagram (app)", "iOS", "Celular")),
    (GOOGLEBOT, None), ("curl/8.5.0", None), ("python-httpx/0.27", None), ("", None),
])
def test_parse_ua(ua, expected):
    assert parse_ua(ua) == expected


def _client(tmp_path):
    s = make_settings(tmp_path)
    _reset_external_db(s)
    return TestClient(create_app(s), base_url="https://testserver")


def _visit(app_client_factory, ua, path="/", **headers):
    c = app_client_factory()
    c.get(path, headers={"user-agent": ua, **headers})
    return c


def test_distinct_devices_counted_once_each(tmp_path):
    with _client(tmp_path) as base:
        app = base.app
        def new(): return TestClient(app, base_url="https://testserver")
        phone, pc = new(), new()
        for _ in range(3):  # o mesmo celular abrindo várias vezes = 1 dispositivo
            phone.get("/", headers={"user-agent": CHROME_ANDROID})
        pc.get("/", headers={"user-agent": EDGE_WIN})
        base.csrf = login(base)
        html = base.get("/admin", headers={"user-agent": FIREFOX}).text
        assert "Dispositivos que acessaram o site" in html
        # celular + pc + o próprio admin (Firefox)
        assert '<div class="label">Dispositivos diferentes</div><div class="value">3</div>' in html
        assert "<strong>Chrome</strong> · Android" in html and "<strong>Edge</strong> · Windows" in html
        assert "<strong>Celular</strong> 1" in html


def test_cookie_is_private_and_hashed_in_db(tmp_path):
    with _client(tmp_path) as c:
        r = c.get("/", headers={"user-agent": CHROME_ANDROID})
        cookie = r.headers["set-cookie"]
        assert "__Host-nm_dev=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie.replace("samesite", "SameSite")
        raw_id = c.cookies.get("__Host-nm_dev")
        with c.app.state.engine.connect() as conn:
            rows = conn.execute(text("select * from devices")).mappings().all()
        assert len(rows) == 1
        blob = " ".join(str(v) for v in rows[0].values())
        assert raw_id not in blob                 # o código do cookie não é guardado, só o hash
        assert "Mozilla" not in blob and "SM-S918B" not in blob and "testclient" not in blob and "127.0.0.1" not in blob
        assert set(rows[0].keys()) == {"id_hash", "first_seen", "last_seen", "visits", "browser", "os", "kind"}


def test_bots_static_health_pdf_and_dnt_are_not_counted(tmp_path):
    with _client(tmp_path) as c:
        c.get("/", headers={"user-agent": GOOGLEBOT})
        c.get("/health", headers={"user-agent": CHROME_ANDROID})
        c.get("/static/css/app.css", headers={"user-agent": CHROME_ANDROID})
        c.get("/robots.txt", headers={"user-agent": CHROME_ANDROID})
        r = c.get("/", headers={"user-agent": CHROME_ANDROID, "dnt": "1"})
        assert "nm_dev" not in r.headers.get("set-cookie", "")
        r = c.get("/", headers={"user-agent": CHROME_ANDROID, "sec-gpc": "1"})
        assert "nm_dev" not in r.headers.get("set-cookie", "")
        with c.app.state.engine.connect() as conn:
            assert conn.execute(text("select count(*) from devices")).scalar() == 0


def test_forged_cookie_is_replaced_and_errors_not_counted(tmp_path):
    with _client(tmp_path) as c:
        c.cookies.set("__Host-nm_dev", "<script>", domain="testserver")
        r = c.get("/", headers={"user-agent": EDGE_WIN})
        assert "<script>" not in r.headers["set-cookie"]
        c.get("/nao-existe", headers={"user-agent": IPHONE, "accept": "text/html"})  # 404 não conta
        with c.app.state.engine.connect() as conn:
            assert conn.execute(text("select count(*) from devices")).scalar() == 1


def test_public_page_does_not_show_device_panel(tmp_path):
    with _client(tmp_path) as c:
        html = c.get("/", headers={"user-agent": CHROME_ANDROID}).text
        assert "Dispositivos que acessaram" not in html and "Dispositivos diferentes" not in html


def test_tracking_failure_never_breaks_pages(tmp_path):
    with _client(tmp_path) as c:
        def boom(*a, **k): raise RuntimeError("banco fora")
        c.app.state.devices.touch = boom
        assert c.get("/", headers={"user-agent": CHROME_ANDROID}).status_code == 200
