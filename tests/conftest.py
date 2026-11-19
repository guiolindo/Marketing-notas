import os
import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PASSWORD = "Sup3r-Segura-123"


def _reset_external_db(settings: Settings) -> None:
    """Com TEST_DATABASE_URL (Postgres), cada teste começa com as tabelas vazias."""
    if os.environ.get("TEST_DATABASE_URL"):
        from sqlalchemy import create_engine

        from app.models import Base
        engine = create_engine(settings.sqlalchemy_url)
        Base.metadata.drop_all(engine)
        engine.dispose()


def make_settings(tmp_path, **kw) -> Settings:
    base = dict(secret_key="k" * 48, admin_password=PASSWORD, database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/t.db",
                max_upload_mb=2, allow_sqlite=True, _env_file=None)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path):
    s = make_settings(tmp_path)
    _reset_external_db(s)
    return s


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="https://testserver") as c:
        yield c


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "token CSRF não encontrado na página"
    return m.group(1)


def login(client) -> str:
    r = client.get("/admin/login")
    r = client.post("/admin/login", data={"usuario": "admin", "senha": PASSWORD, "csrf": csrf_from(r.text)},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return csrf_from(client.get("/admin").text)


def upload(client, token, *files):
    return client.post("/admin/enviar", data={"csrf": token},
                       files=[("files", (name, data, "application/pdf")) for name, data in files])


@pytest.fixture
def admin(client):
    client.csrf = login(client)
    return client
