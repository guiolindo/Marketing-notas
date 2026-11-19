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
        assert set(rows[0].keys()) == {"id_hash", "first_seen", "last_seen", "visits", "browser", "os", "kind", "model"}


def test_bots_static_health_pdf_and_gpc_are_not_counted(tmp_path):
    with _client(tmp_path) as c:
        c.get("/", headers={"user-agent": GOOGLEBOT})
        c.get("/health", headers={"user-agent": CHROME_ANDROID})
        c.get("/static/css/app.css", headers={"user-agent": CHROME_ANDROID})
        c.get("/robots.txt", headers={"user-agent": CHROME_ANDROID})
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


def test_dnt_header_alone_does_not_block_but_gpc_does(tmp_path):
    with _client(tmp_path) as c:
        assert "nm_dev" in c.get("/", headers={"user-agent": CHROME_ANDROID, "dnt": "1"}).headers["set-cookie"]


# ---------------------------------------------------------------- modelo do aparelho

def test_html_asks_chrome_for_the_model(client):
    assert client.get("/").headers["accept-ch"] == "Sec-CH-UA-Model"
    assert "accept-ch" not in client.get("/static/css/app.css").headers


@pytest.mark.parametrize("raw,expected", [
    ('"SM-S918B"', "Samsung SM-S918B"), ('"Pixel 7"', "Pixel 7"), ('"moto g54 5G"', "moto g54 5G"),
    ('"K"', ""), ('""', ""), ("", ""), ('"Generic Android"', ""), ("<script>x</script>", ""), ('"' + "x" * 80 + '"', ""),
])
def test_clean_model(raw, expected):
    from app.services.devices import clean_model
    assert clean_model(raw) == expected


def test_model_arrives_on_second_visit_and_shows_in_panel(tmp_path):
    with _client(tmp_path) as base:
        phone = TestClient(base.app, base_url="https://testserver")
        phone.get("/", headers={"user-agent": CHROME_ANDROID})                                   # 1º acesso: sem dica
        phone.get("/", headers={"user-agent": CHROME_ANDROID, "sec-ch-ua-model": '"SM-S918B"'})  # 2º: Chrome manda o modelo
        base.csrf = login(base)
        html = base.get("/admin", headers={"user-agent": EDGE_WIN}).text
        assert "Samsung SM-S918B" in html and 'class="dev-h3">Modelo' in html
        with base.app.state.engine.connect() as conn:
            assert conn.execute(text("select model from devices where kind = 'Celular'")).scalar() == "Samsung SM-S918B"


def test_adds_model_column_to_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect

    from app.database import init_db
    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url)
    with eng.begin() as conn:  # tabela como era antes da coluna "model"
        conn.execute(text("create table devices (id_hash varchar(64) primary key, first_seen datetime, last_seen datetime,"
                          " visits integer, browser varchar(30), os varchar(30), kind varchar(15))"))
        conn.execute(text("insert into devices values ('abc', '2026-01-01', '2026-01-02', 3, 'Chrome', 'Android', 'Celular')"))
    init_db(eng)
    assert "model" in {c["name"] for c in inspect(eng).get_columns("devices")}
    with eng.connect() as conn:
        assert conn.execute(text("select visits from devices")).scalar() == 3  # dados antigos preservados
    init_db(eng)  # rodar de novo não quebra


# ---------------------------------------------------------------- "este aparelho foi contado?"

def test_panel_tells_whether_this_device_was_counted(tmp_path):
    with _client(tmp_path) as c:
        c.headers["user-agent"] = CHROME_ANDROID
        c.csrf = login(c)
        h = {"user-agent": CHROME_ANDROID}
        c.get("/admin", headers=h)
        assert "Este aparelho — Contado: Chrome · Android" in c.get("/admin", headers=h).text
        assert "sinal de privacidade (Sec-GPC)" in c.get("/admin", headers={**h, "sec-gpc": "1"}).text
        assert "robô" in c.get("/admin", headers={"user-agent": GOOGLEBOT}).text


def test_footer_has_no_tracking_notice(client):
    html = client.get("/").text
    assert "contagem anônima de acessos" not in html and "uso interno" in html
