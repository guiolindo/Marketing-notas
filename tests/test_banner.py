from fastapi.testclient import TestClient

from app.main import create_app
from app.services.site import MAX_TEXT, PRESETS, BannerStore
from tests.conftest import _reset_external_db, login, make_settings


def set_banner(c, **data):
    return c.post("/admin/aviso", data={"csrf": c.csrf, **data}, follow_redirects=True)


def test_no_banner_by_default(client):
    assert "site-banner" not in client.get("/").text


def test_admin_picks_each_preset_and_public_sees_it(admin):
    for key, (_, kind, text) in PRESETS.items():
        r = set_banner(admin, preset=key)
        assert "Aviso do site atualizado" in r.text
        home = admin.get("/").text
        assert f'class="site-banner {kind}"' in home and text in home


def test_custom_message_and_color(admin):
    set_banner(admin, preset="custom", texto="Notas de junho a caminho", tipo="ok")
    home = admin.get("/").text
    assert "Notas de junho a caminho" in home and 'class="site-banner ok"' in home
    assert 'value="Notas de junho a caminho"' in admin.get("/admin").text  # volta preenchido no painel


def test_turn_off(admin):
    set_banner(admin, preset="disponivel")
    assert "site-banner" in admin.get("/").text
    assert "desligado" in set_banner(admin, preset="off").text
    assert "site-banner" not in admin.get("/").text.split("page-head")[0]


def test_validation(admin):
    assert "Escreva a mensagem" in set_banner(admin, preset="custom", texto="   ").text
    assert "máximo" in set_banner(admin, preset="custom", texto="x" * (MAX_TEXT + 1)).text
    assert "inválida" in set_banner(admin, preset="hack").text
    assert 'class="site-banner warn"' not in admin.get("/").text


def test_message_is_escaped(admin):
    set_banner(admin, preset="custom", texto='<script>alert(1)</script> <b>oi</b>', tipo="rojo")
    home = admin.get("/").text
    assert "<script>alert(1)" not in home and "&lt;script&gt;" in home
    assert 'class="site-banner info"' in home  # cor inválida cai para o padrão


def test_only_admin_with_csrf_can_change(client, admin):
    anon = client.post("/admin/aviso", data={"csrf": "x", "preset": "disponivel"}, follow_redirects=False)
    assert anon.status_code in (303, 403)
    assert admin.post("/admin/aviso", data={"csrf": "errado", "preset": "disponivel"}).status_code == 403
    assert "site-banner" not in admin.get("/").text.split("page-head")[0]


def test_banner_survives_restart(tmp_path):
    s = make_settings(tmp_path)
    _reset_external_db(s)
    with TestClient(create_app(s), base_url="https://testserver") as c:
        c.csrf = login(c)
        set_banner(c, preset="procurando")
    with TestClient(create_app(s), base_url="https://testserver") as c2:  # "redeploy" com o mesmo banco
        assert PRESETS["procurando"][2] in c2.get("/").text


def test_banner_failure_never_breaks_pages(client):
    class Broken:
        def __call__(self):
            raise RuntimeError("banco fora")
    store: BannerStore = client.app.state.banner
    store._sf = Broken()
    store._cache = None
    assert client.get("/").status_code == 200
