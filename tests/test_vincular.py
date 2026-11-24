import re
from datetime import datetime

from sqlalchemy import text

from tests.conftest import upload
from tests.helpers import danfe_pdf, make_pdf, nfe_key
from tests.test_conciliacao import apply, good_rows, preview, xlsx

ORF_COLS = ["fornecedor", "cnpj", "titulo", "data_emissao", "vencimento", "data_pagamento", "valor", "motivo", "data_lancamento", "especie"]
ORFAOS = [
    ["BWP COMERCIO LTDA - 175233", "23439537000158", "1616/-1", datetime(2026, 6, 1), datetime(2026, 6, 29), datetime(2026, 6, 26), 2378.88, "", datetime(2026, 6, 5), "MDESMK"],
    ["GRAFICA X - 1", "11222333000181", "500/1-1", datetime(2026, 7, 1), datetime(2026, 7, 30), datetime(2026, 7, 29), 1000, "", datetime(2026, 7, 2), "DESPMK"],
    ["GRAFICA X - 1", "11222333000181", "500/1-2", datetime(2026, 7, 1), datetime(2026, 8, 30), None, 1000, "", datetime(2026, 7, 2), "DESPMK"],
]


def scanned_pdf(tag: str = "a") -> bytes:
    """PDF "escaneado": sem nenhum texto que o sistema consiga ler."""
    return make_pdf(" ", extra=f"% {tag}\n".encode())


def with_orphans(c):
    upload(c, c.csrf, ("d.pdf", danfe_pdf(numero=316)))
    from openpyxl import Workbook
    import io
    wb = Workbook()
    ws = wb.active
    ws.title = "conciliacao"
    ws.append(["id_sistema", "chave_acesso", "data_lancamento", "titulo", "parcela", "vencimento", "data_pagamento", "valor_nominal", "valor_pago"])
    ws.append(good_rows()[0][:9])
    wo = wb.create_sheet("sem_pdf")
    wo.append(ORF_COLS)
    for o in ORFAOS:
        wo.append(o)
    b = io.BytesIO()
    wb.save(b)
    apply(c, preview(c, b.getvalue()).text)
    with c.app.state.engine.connect() as conn:
        return {r[1]: r[0] for r in conn.execute(text("select id, titulo from erp_orfaos")).all()}


def link(c, oid, pdf, **form):
    data = {"csrf": c.csrf, "tipo": "nfse", "numero": "", **form}
    return c.post(f"/admin/faltando/{oid}/vincular", data=data, files={"arquivo": ("scan.pdf", pdf, "application/pdf")},
                  follow_redirects=True)


def test_link_scanned_pdf_creates_note_from_erp(admin):
    ids = with_orphans(admin)
    adm = admin.get("/admin").text
    assert "Vincular PDF" in adm and f'action="/admin/faltando/{ids["1616/-1"]}/vincular"' in adm
    assert 'value="1616"' in adm  # número já preenchido a partir do título
    r = link(admin, ids["1616/-1"], scanned_pdf(), numero="1616")
    assert "PDF vinculado: NFS-e 1616 de BWP COMERCIO LTDA" in r.text and "R$ 2.378,88" in r.text
    assert "Paga" in r.text and "pago em 26/06/2026" in r.text and "05/06/2026" in r.text  # pagamento e lançamento do ERP
    assert "não informada" in r.text and "PDF escaneado" in r.text and "manual-" not in r.text
    assert "título 1616/-1" not in admin.get("/").text  # saiu da lista de faltantes
    home = admin.get("/").text
    assert "2 notas aguardando PDF" in home  # sobraram as 2 parcelas da GRAFICA X (mesma nota)
    assert 'chip-scan' in home
    nid = int(re.search(r"/nota/(\d+)", r.url.path if hasattr(r.url, "path") else str(r.url)).group(1))
    assert admin.get(f"/nota/{nid}.pdf").content.startswith(b"%PDF-")


def test_parcels_of_same_note_are_linked_together(admin):
    ids = with_orphans(admin)
    r = link(admin, ids["500/1-1"], scanned_pdf(), tipo="danfe", numero="500")
    assert "DANFE 500" in r.text and "R$ 2.000,00" in r.text and "Paga em parte" in r.text
    assert "500/1-1" in r.text and "500/1-2" in r.text
    assert "aguardando PDF" in admin.get("/").text and "GRAFICA X" not in admin.get("/").text.split("aguardando PDF")[1].split("</details>")[0]


def test_typed_access_key_is_validated_and_kept(admin):
    ids = with_orphans(admin)
    k = nfe_key(numero=500)
    bad = k[:-1] + str((int(k[-1]) + 1) % 10)
    assert "dígito verificador" in link(admin, ids["500/1-1"], scanned_pdf(), tipo="danfe", numero="500", chave=bad).text
    assert "dígitos" in link(admin, ids["500/1-1"], scanned_pdf(), tipo="danfe", numero="500", chave="123").text
    r = link(admin, ids["500/1-1"], scanned_pdf(), tipo="danfe", numero="500", chave=" ".join(k[i:i + 4] for i in range(0, 44, 4)))
    assert " ".join(k[i:i + 4] for i in range(0, 44, 4)) in r.text and f'data-copy="{k}"' in r.text


def test_duplicates_and_unsafe_pdf_are_refused(admin):
    ids = with_orphans(admin)
    pdf = scanned_pdf("x")
    link(admin, ids["1616/-1"], pdf, numero="1616")
    r = link(admin, ids["500/1-1"], pdf, tipo="danfe", numero="500")
    assert "Este PDF já está no sistema" in r.text
    r = link(admin, ids["500/1-1"], make_pdf(" ", catalog_extra=b"/OpenAction << /S /JavaScript /JS (x) >>"), tipo="danfe", numero="500")
    assert "script" in r.text
    link(admin, ids["500/1-1"], scanned_pdf("y"), tipo="danfe", numero="500")
    with admin.app.state.engine.begin() as conn:  # o mesmo título volta para a lista (ex.: nova importação)
        conn.execute(text("insert into erp_orfaos (fornecedor, cnpj, titulo, data_emissao, valor_cents, motivo, especie) "
                          "values ('GRAFICA X - 1', '11222333000181', '500/1-1', '2026-07-01', 100000, '', '')"))
        oid = conn.execute(text("select max(id) from erp_orfaos")).scalar()
    assert "Já existe esta nota no sistema" in link(admin, oid, scanned_pdf("v"), tipo="danfe", numero="500").text
    r = link(admin, 99999, scanned_pdf("z"), numero="1")
    assert "não está mais na lista" in r.text
    assert "não está mais na lista" in link(admin, ids["500/1-2"], scanned_pdf("w"), tipo="danfe", numero="500").text  # foi junto como parcela
    assert "Informe o número" in link(admin, oid, scanned_pdf("u"), tipo="danfe", numero="abc").text


def test_only_admin_can_link(client):
    r = client.post("/admin/faltando/1/vincular", data={"csrf": "x", "numero": "1"},
                    files={"arquivo": ("a.pdf", scanned_pdf(), "application/pdf")}, follow_redirects=False)
    assert r.status_code in (303, 403)


def test_linked_note_is_exported_without_fake_key_and_reconciles(admin):
    ids = with_orphans(admin)
    link(admin, ids["1616/-1"], scanned_pdf(), numero="1616")
    import io

    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(admin.get("/admin/exportar-dados.xlsx").content))["notas_sistema"]
    row = next(r for r in ws.iter_rows(min_row=2, values_only=True) if r[3] == "1616")
    assert row[1] in (None, "")  # nota escaneada sai sem chave (o arquivo de conciliação casa pelo id)
    same = [row[0], "", datetime(2026, 6, 5), "1616/-1", 1, datetime(2026, 6, 29), datetime(2026, 6, 26), 2378.88, 2378.88, "MDESMK", "alta", ""]
    r = preview(admin, xlsx([same]))
    assert "Nada para mudar" in r.text and "impedem a aplicação" not in r.text  # chave vazia + id: casa pela id
    novo = list(same)
    novo[2] = datetime(2026, 6, 8)  # lançamento corrigido no ERP
    r = preview(admin, xlsx([novo]))
    assert "Aplicar 1 alteração" in r.text
