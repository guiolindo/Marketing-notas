import io

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import Settings
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    _reset_external_db,
    csrf_from,
    login,
    make_settings,
    upload,
)
from tests.helpers import danfe_pdf, nfse_pdf

# ---------------------------------------------------------------- acesso e segurança

def test_public_page_and_admin_protected(client):
    assert client.get("/").status_code == 200
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_anonymous_cannot_upload_or_delete(client):
    r = client.post("/admin/enviar", files=[("files", ("a.pdf", danfe_pdf(), "application/pdf"))],
                    data={"csrf": "x"}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert "Nenhuma nota lançada ainda" in client.get("/").text
    assert client.post("/admin/nota/1/excluir", data={"csrf": "x"}, follow_redirects=False).status_code in (303, 403)


def test_login_requires_csrf_and_right_password(client):
    page = client.get("/admin/login")
    token = csrf_from(page.text)
    assert client.post("/admin/login", data={"usuario": "admin", "senha": PASSWORD, "csrf": "errado"}).status_code == 403
    r = client.post("/admin/login", data={"usuario": "admin", "senha": "senha-errada-123", "csrf": token})
    assert r.status_code == 401 and "incorretos" in r.text
    r = client.post("/admin/login", data={"usuario": "outro", "senha": PASSWORD, "csrf": token})
    assert r.status_code == 401


def test_login_lockout(client):
    token = csrf_from(client.get("/admin/login").text)
    for _ in range(5):
        assert client.post("/admin/login", data={"usuario": "admin", "senha": "errada-errada-1", "csrf": token}).status_code == 401
    r = client.post("/admin/login", data={"usuario": "admin", "senha": PASSWORD, "csrf": token})
    assert r.status_code == 429  # nem a senha certa entra enquanto estiver travado


def test_security_headers_and_cookie(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "noindex" in r.headers["x-robots-tag"] and "strict-transport-security" in r.headers
    r = client.get("/admin/login")
    cookie = r.headers["set-cookie"].lower()
    assert "__host-nm_session" in cookie and "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "script-src 'self'" in r.headers["content-security-policy"] and "unsafe-inline" not in r.headers["content-security-policy"]


def test_post_from_other_origin_is_blocked(admin):
    r = admin.post("/admin/sair", data={"csrf": admin.csrf}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_logout_ends_session(admin):
    assert admin.post("/admin/sair", data={"csrf": admin.csrf}, follow_redirects=False).status_code == 303
    assert admin.get("/admin", follow_redirects=False).status_code == 303


def test_weak_config_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        create_app(Settings(secret_key="k" * 48, admin_password="123", database_url=f"sqlite:///{tmp_path}/x.db", _env_file=None))
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(Settings(secret_key="curta", admin_password=PASSWORD, database_url=f"sqlite:///{tmp_path}/x.db", _env_file=None))


def test_viewer_password_gate(tmp_path):
    s = make_settings(tmp_path, viewer_password="entrada-1234")
    _reset_external_db(s)
    with TestClient(create_app(s), base_url="https://testserver") as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/entrar"
        assert c.get("/baixar-tudo.pdf", follow_redirects=False).status_code == 303
        token = csrf_from(c.get("/entrar").text)
        assert c.post("/entrar", data={"senha": "errada", "csrf": token}).status_code == 401
        assert c.post("/entrar", data={"senha": "entrada-1234", "csrf": token}, follow_redirects=False).status_code == 303
        assert c.get("/").status_code == 200


# ---------------------------------------------------------------- lançamento e duplicidade

def test_upload_and_totals(admin):
    r = upload(admin, admin.csrf, ("danfe.pdf", danfe_pdf()), ("nfse.pdf", nfse_pdf()))
    assert r.status_code == 200
    assert r.text.count("Lançada") == 2
    home = admin.get("/").text
    assert "R$ 2.700,00" in home  # 1.400,00 + 1.300,00
    assert "GRAFICA EXEMPLO LTDA" in home and "Baixar tudo em PDF" in home


def test_same_file_twice_is_duplicate(admin):
    pdf = danfe_pdf()
    assert "Lançada" in upload(admin, admin.csrf, ("a.pdf", pdf)).text
    r = upload(admin, admin.csrf, ("b.pdf", pdf))
    assert "Duplicada" in r.text and "Lançada" not in r.text
    assert "R$ 1.400,00" in admin.get("/").text and "Notas</div><div class=\"value\">1<" in admin.get("/").text


def test_same_note_in_different_pdf_bytes_is_duplicate(admin):
    assert "Lançada" in upload(admin, admin.csrf, ("a.pdf", danfe_pdf())).text
    again = upload(admin, admin.csrf, ("copia.pdf", danfe_pdf() + b"% outro arquivo, mesma nota\n"))
    assert "Duplicada" in again.text and "já foi lançada" in again.text


def test_duplicates_inside_one_batch(admin):
    r = upload(admin, admin.csrf, ("a.pdf", nfse_pdf()), ("b.pdf", nfse_pdf() + b"% x\n"))
    assert r.text.count("Lançada") == 1 and r.text.count("Duplicada") == 1


def test_invalid_files_are_reported(admin):
    r = upload(admin, admin.csrf, ("x.pdf", b"nao sou pdf"), ("y.pdf", b""), ("ok.pdf", danfe_pdf()))
    assert r.text.count("Recusada") == 2 and r.text.count("Lançada") == 1


def test_upload_size_limit(admin):
    big = danfe_pdf() + b"0" * (3 * 1024 * 1024)
    assert "Recusada" in upload(admin, admin.csrf, ("big.pdf", big)).text


def test_upload_without_csrf_is_blocked(admin):
    r = admin.post("/admin/enviar", data={"csrf": "x"}, files=[("files", ("a.pdf", danfe_pdf(), "application/pdf"))])
    assert r.status_code == 403
    assert "Nenhuma nota lançada ainda" in admin.get("/").text


def test_delete_then_reupload(admin):
    pdf = danfe_pdf()
    upload(admin, admin.csrf, ("a.pdf", pdf))
    assert admin.post("/admin/nota/1/excluir", data={"csrf": admin.csrf}, follow_redirects=False).status_code == 303
    assert "Nenhuma nota" in admin.get("/").text
    assert "Lançada" in upload(admin, admin.csrf, ("a.pdf", pdf)).text


def test_edit_category(admin):
    upload(admin, admin.csrf, ("a.pdf", danfe_pdf()))
    r = admin.post("/admin/nota/1", data={"csrf": admin.csrf, "categoria": "Lonas", "obs": "campanha <b>x</b>"},
                   follow_redirects=True)
    assert "Lonas" in r.text and "<b>x</b>" not in r.text  # escapado
    assert "campanha &lt;b&gt;x&lt;/b&gt;" in r.text


# ---------------------------------------------------------------- filtros, PDF único, CSV

@pytest.fixture
def loaded(admin):
    upload(admin, admin.csrf, ("d1.pdf", danfe_pdf(numero=1, total="500,00", desconto="0,00")),
           ("d2.pdf", danfe_pdf(numero=2, total="1.400,00")), ("n1.pdf", nfse_pdf(numero=1, total="1.300,00")))
    return admin


def test_filters(loaded):
    c = loaded
    assert "R$ 3.200,00" in c.get("/").text
    r = c.get("/", params={"tipo": "nfse"})
    assert "R$ 1.300,00" in r.text and "R$ 3.200,00" not in r.text
    assert "R$ 1.400,00" in c.get("/", params={"vmin": "1000", "tipo": "danfe"}).text
    assert "Nenhuma nota com esses filtros" in c.get("/", params={"q": "inexistente"}).text
    assert "R$ 500,00" in c.get("/", params={"vmax": "500,00"}).text
    assert "Nenhuma nota com esses filtros" in c.get("/", params={"de": "2030-01-01"}).text
    assert c.get("/", params={"de": "lixo", "vmin": "abc", "ordem": "'; drop table notes;--"}).status_code == 200
    assert c.get("/", params={"q": "100%_\\"}).status_code == 200


def test_merged_pdf_contains_all_pages_and_respects_filters(loaded):
    r = loaded.get("/baixar-tudo.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 3 and len(reader.outline) == 3
    only = PdfReader(io.BytesIO(loaded.get("/baixar-tudo.pdf", params={"tipo": "nfse"}).content))
    assert len(only.pages) == 1
    assert loaded.get("/baixar-tudo.pdf", params={"q": "inexistente"}).status_code == 404


def test_single_pdf_view_and_download(loaded):
    r = loaded.get("/nota/1.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-") and "inline" in r.headers["content-disposition"]
    assert "attachment" in loaded.get("/nota/1/baixar").headers["content-disposition"]
    assert loaded.get("/nota/999.pdf").status_code == 404


def test_csv_export(loaded):
    r = loaded.get("/exportar.csv")
    assert r.status_code == 200 and r.text.startswith("﻿Tipo;")
    assert len(r.text.strip().splitlines()) == 4 and "1400,00" in r.text


def test_csv_neutralizes_formulas(loaded):
    loaded.post("/admin/nota/1", data={"csrf": loaded.csrf, "categoria": "=HYPERLINK(\"x\")", "obs": ""})
    assert "'=HYPERLINK" in loaded.get("/exportar.csv").text


def test_encrypted_storage(tmp_path):
    from cryptography.fernet import Fernet
    s = make_settings(tmp_path, document_encryption_key=Fernet.generate_key().decode())
    _reset_external_db(s)
    with TestClient(create_app(s), base_url="https://testserver") as c:
        token = login(c)
        upload(c, token, ("a.pdf", danfe_pdf()))
        assert c.get("/nota/1.pdf").content.startswith(b"%PDF-")
        from sqlalchemy import text
        with c.app.state.engine.connect() as conn:
            raw = conn.execute(text("select pdf_data from notes")).scalar()
        assert not bytes(raw).startswith(b"%PDF-")
        assert len(PdfReader(io.BytesIO(c.get("/baixar-tudo.pdf").content)).pages) == 1


def test_error_pages_do_not_leak(client):
    r = client.get("/nao-existe", headers={"accept": "text/html"})
    assert r.status_code == 404 and "Traceback" not in r.text
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
