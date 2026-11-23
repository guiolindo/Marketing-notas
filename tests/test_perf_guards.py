import io

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text

from app.main import create_app
from app.services.merge import MergeCache, _dedupe
from tests.conftest import _reset_external_db, login, make_settings, upload
from tests.helpers import danfe_pdf, nfse_pdf


def _client(tmp_path, **kw):
    s = make_settings(tmp_path, **kw)
    _reset_external_db(s)
    return TestClient(create_app(s), base_url="https://testserver")


def _two_notes(c):
    token = login(c)
    upload(c, token, ("d.pdf", danfe_pdf(numero=1)), ("n.pdf", nfse_pdf(numero=1)))
    return token


def test_merged_pdf_is_cached_and_reused(tmp_path):
    with _client(tmp_path) as c:
        _two_notes(c)
        a = c.get("/baixar-tudo.pdf").content
        b = c.get("/baixar-tudo.pdf").content
        cache = c.app.state.merge_cache
        assert (cache.misses, cache.hits) == (1, 1) and a == b
        # outro filtro = outro pacote = outra entrada
        c.get("/baixar-tudo.pdf", params={"tipo": "nfse"})
        assert cache.misses == 2


def test_cache_is_invalidated_when_the_set_of_notes_changes(tmp_path):
    with _client(tmp_path) as c:
        token = _two_notes(c)
        assert len(PdfReader(io.BytesIO(c.get("/baixar-tudo.pdf").content)).pages) == 2
        upload(c, token, ("d2.pdf", danfe_pdf(numero=2)))
        assert len(PdfReader(io.BytesIO(c.get("/baixar-tudo.pdf").content)).pages) == 3
        c.post("/admin/nota/1/excluir", data={"csrf": token})
        assert len(PdfReader(io.BytesIO(c.get("/baixar-tudo.pdf").content)).pages) == 2


def test_editing_category_does_not_invalidate_cache(tmp_path):
    with _client(tmp_path) as c:
        token = _two_notes(c)
        c.get("/baixar-tudo.pdf")
        c.post("/admin/nota/1", data={"csrf": token, "categoria": "X", "obs": ""})
        c.get("/baixar-tudo.pdf")
        assert c.app.state.merge_cache.hits == 1


def test_page_cap_refuses_instead_of_truncating(tmp_path):
    with _client(tmp_path, max_merged_pages=1) as c:
        _two_notes(c)
        r = c.get("/baixar-tudo.pdf", headers={"accept": "text/html"})
        assert r.status_code == 413 and "máximo 1" in r.text
        assert c.get("/baixar-tudo.pdf", params={"tipo": "nfse"}).status_code == 200  # 1 página cabe


def test_corrupt_stored_pdf_fails_loudly_not_silently(tmp_path):
    with _client(tmp_path) as c:
        _two_notes(c)
        with c.app.state.engine.begin() as conn:
            conn.execute(text("update notes set pdf_data = :b where id = 1"), {"b": b"lixo"})
        r = c.get("/baixar-tudo.pdf", headers={"accept": "text/html"})
        assert r.status_code == 500 and "Não foi possível juntar" in r.text


def test_concurrency_limit_returns_503(tmp_path):
    with _client(tmp_path, merge_concurrency=1) as c:
        _two_notes(c)
        slots = c.app.state.merge_slots
        assert slots.acquire(blocking=False)  # outro PDF "em geração"
        try:
            assert c.get("/baixar-tudo.pdf").status_code == 503
        finally:
            slots.release()
        assert c.get("/baixar-tudo.pdf").status_code == 200


def test_heavy_routes_are_rate_limited_per_ip(tmp_path):
    with _client(tmp_path, merge_rate_per_minute=3) as c:
        _two_notes(c)
        codes = [c.get("/baixar-tudo.pdf").status_code for _ in range(3)] + [c.get("/exportar.xlsx").status_code]
        assert codes == [200, 200, 200, 429]
        assert c.get("/").status_code == 200  # a página comum não é afetada


def test_gzip_for_text_but_not_for_pdf(tmp_path):
    with _client(tmp_path) as c:
        _two_notes(c)
        h = {"accept-encoding": "gzip"}
        assert c.get("/", headers=h).headers.get("content-encoding") == "gzip"
        assert c.get("/static/css/app.css", headers=h).headers.get("content-encoding") == "gzip"
        assert c.get("/baixar-tudo.pdf", headers=h).headers.get("content-encoding") in (None, "identity")
        assert c.get("/nota/1.pdf", headers=h).content.startswith(b"%PDF-")


def test_static_assets_are_cached_by_content_hash(client):
    html = client.get("/").text
    import re
    v = re.search(r"app\.css\?v=(\w+)", html).group(1)
    assert len(v) == 10
    r = client.get(f"/static/css/app.css?v={v}")
    assert "immutable" in r.headers["cache-control"]
    assert "no-store" in client.get("/").headers["cache-control"]


def test_lru_cache_evicts_by_size():
    c = MergeCache(max_bytes=10)
    c.put("a", b"123456")
    c.put("b", b"123456")
    assert c.get("a") is None and c.get("b") == b"123456"
    c.put("grande", b"x" * 11)  # maior que o teto: nem entra
    assert c.get("grande") is None
    assert MergeCache(0).get("x") is None


def test_dedupe_never_breaks_the_merge(monkeypatch):
    class W:
        def compress_identical_objects(self, **kw):
            raise RuntimeError("falha qualquer da biblioteca")
    _dedupe(W())  # não propaga: é só otimização
