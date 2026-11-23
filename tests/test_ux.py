import re

from tests.conftest import upload
from tests.helpers import danfe_pdf, nfe_key, nfse_pdf
from tests.test_conciliacao import apply, good_rows, preview, xlsx


def test_note_detail_page(admin):
    upload(admin, admin.csrf, ("d.pdf", danfe_pdf(numero=316)), ("n.pdf", nfse_pdf(numero=1)))
    apply(admin, preview(admin, xlsx(good_rows())).text)
    html = admin.get("/nota/2").text
    assert "FULANA DE TAL ME" in html and "R$ 1.300,00" in html and "Paga em parte" in html
    assert "1/-1" in html and "pago em 26/06/2026" in html and "1/-2" in html and "em aberto" in html
    assert "Lançamento no ERP" in html and "31/05/2026" in html
    k = nfe_key(numero=316)
    d = admin.get("/nota/1").text
    assert " ".join(k[i:i + 4] for i in range(0, 44, 4)) in d and f'data-copy="{k}"' in d
    assert 'href="/admin/nota/1"' in d  # admin vê "Editar"
    assert admin.get("/nota/999", headers={"accept": "text/html"}).status_code == 404
    assert admin.get("/nota/1.pdf").content.startswith(b"%PDF-")  # rota do PDF continua funcionando


def test_detail_hides_edit_for_visitors_and_back_link_is_safe(client, admin):
    upload(admin, admin.csrf, ("d.pdf", danfe_pdf(numero=316)))
    from fastapi.testclient import TestClient
    visitor = TestClient(client.app, base_url="https://testserver")
    html = visitor.get("/nota/1", headers={"referer": "https://testserver/?tipo=danfe"}).text
    assert 'href="/admin/nota/1"' not in html and 'href="/?tipo=danfe"' in html
    html = visitor.get("/nota/1", headers={"referer": "https://evil.example/phish"}).text
    assert 'class="back" href="/"' in html  # nunca volta para outro site


def test_rows_link_to_detail_and_chart_has_month_data(admin):
    upload(admin, admin.csrf, ("d.pdf", danfe_pdf(numero=316)), ("n.pdf", nfse_pdf(numero=1)))
    html = admin.get("/").text
    assert 'data-href="/nota/1"' in html and 'class="rowlink strong" href="/nota/2"' in html
    assert 'data-mes="Março de 2026"' in html and 'data-mes="Maio de 2026"' in html
    assert 'class="chart-info"' in html and 'data-tip-mes>Maio de 2026<' in html  # começa no mês mais recente
    m = re.search(r'data-mes="Maio de 2026" data-val="([^"]+)" data-n="(\d+)" data-url="([^"]+)"', html)
    assert m.group(1) == "R$ 1.300,00" and m.group(2) == "1" and "de=2026-05-01" in m.group(3) and "ate=2026-05-31" in m.group(3)
    filtered = admin.get(m.group(3).replace("&amp;", "&")).text
    assert "R$ 1.300,00" in filtered and "LONA IMPRESSA" not in filtered


def test_show_more_returns_only_rows(admin):
    files = [(f"d{i}.pdf", danfe_pdf(numero=i)) for i in range(1, 56)]
    for i in range(0, 55, 20):
        upload(admin, admin.csrf, *files[i:i + 20])
    page1 = admin.get("/").text
    assert page1.count("data-href=") == 50 and "data-more" in page1 and "5</span> restantes" in page1
    frag = admin.get("/", params={"parcial": "1", "p": "2"}).text
    assert frag.count("data-href=") == 5 and "<html" not in frag and "<tr" in frag
