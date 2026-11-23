import io
from datetime import date, datetime

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import text

from tests.conftest import upload
from tests.helpers import danfe_pdf, nfe_key, nfse_key, nfse_pdf

HEAD = ["id_sistema", "chave_acesso", "data_lancamento", "titulo", "parcela", "vencimento", "data_pagamento",
        "valor_nominal", "valor_pago", "especie", "confianca", "observacao"]


def xlsx(rows, head=HEAD, orphans=None, sheet="conciliacao") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(head)
    for r in rows:
        ws.append(r)
    if orphans is not None:
        wo = wb.create_sheet("sem_pdf")
        wo.append(["fornecedor", "cnpj", "titulo", "data_emissao", "vencimento", "data_pagamento", "valor", "motivo"])
        for o in orphans:
            wo.append(o)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


DANFE_KEY, NFSE_KEY = nfe_key(numero=316), nfse_key(numero=1)


def good_rows():
    return [
        [1, DANFE_KEY, "11/03/2026", "316/1-1", 1, "31/03/2026", "30/03/2026", 1400, 1400, "DESPMK", "alta", "CNPJ+valor+número"],
        [2, NFSE_KEY, datetime(2026, 5, 31), "1/-1", 1, datetime(2026, 6, 26), datetime(2026, 6, 26), 650, 650, "MDESMK", "alta", ""],
        [2, NFSE_KEY, datetime(2026, 5, 31), "1/-2", 2, datetime(2026, 7, 26), None, 650, 0, "MDESMK", "alta", ""],
    ]


@pytest.fixture
def two(admin):
    upload(admin, admin.csrf, ("d.pdf", danfe_pdf(numero=316)), ("n.pdf", nfse_pdf(numero=1)))
    return admin


def preview(c, data, name="conciliacao.xlsx"):
    return c.post("/admin/conciliacao", data={"csrf": c.csrf}, files={"arquivo": (name, data, "application/octet-stream")})


def apply(c, html):
    import re
    token = re.search(r'name="token" value="([^"]+)"', html).group(1)
    return c.post("/admin/conciliacao/aplicar", data={"csrf": c.csrf, "token": token}, follow_redirects=True)


def situacoes(c):
    with c.app.state.engine.connect() as conn:
        return dict(conn.execute(text("select id, situacao from notes")).all())


def test_preview_changes_nothing_until_apply(two):
    r = preview(two, xlsx(good_rows()))
    assert r.status_code == 200 and "Conferir antes de aplicar" in r.text and "Aplicar 2 alterações" in r.text
    assert situacoes(two) == {1: "sem_lancamento", 2: "sem_lancamento"}
    r = apply(two, r.text)
    assert "Conciliação aplicada: 2 nota(s)" in r.text
    assert situacoes(two) == {1: "paga", 2: "parcial"}


def test_public_page_shows_payment_status_and_strip(two):
    apply(two, preview(two, xlsx(good_rows())).text)
    home = two.get("/").text
    assert "Paga · 30/03/2026" in home and "Pago R$ 650,00" in home and "lanç. 11/03/2026" in home
    assert 'class="st st-paga' in home and "R$ 2.050,00" in home  # pago: 1400 + 650
    assert "2 parcelas" in home
    assert "R$ 1.400,00" in two.get("/", params={"situacao": "paga"}).text
    assert "Nenhuma nota com esses filtros" in two.get("/", params={"situacao": "sem_lancamento"}).text
    em_aberto = two.get("/", params={"situacao": "em_aberto"}).text
    assert "Pago R$ 650,00" in em_aberto and "Paga · 30/03" not in em_aberto


def test_reimport_is_idempotent(two):
    apply(two, preview(two, xlsx(good_rows())).text)
    r = preview(two, xlsx(good_rows()))
    assert "Nada para mudar" in r.text
    with two.app.state.engine.connect() as conn:
        assert conn.execute(text("select count(*) from titulos")).scalar() == 3


def test_reimport_replaces_parcels(two):
    apply(two, preview(two, xlsx(good_rows())).text)
    rows = good_rows()
    rows[2][6], rows[2][8] = datetime(2026, 7, 25), 650  # 2ª parcela agora paga
    r = preview(two, xlsx(rows))
    assert "Aplicar 1 alteração" in r.text
    apply(two, r.text)
    assert situacoes(two)[2] == "paga"
    with two.app.state.engine.connect() as conn:
        assert conn.execute(text("select count(*) from titulos where note_id = 2")).scalar() == 2


@pytest.mark.parametrize("change,msg", [
    (lambda r: r[0].__setitem__(1, NFSE_KEY), "não são da mesma nota"),
    (lambda r: r[0].__setitem__(1, 29260311222333000181550010000003161082938816), "virou número"),
    (lambda r: r[0].__setitem__(2, "31/02/2026"), "data inválida"),
    (lambda r: r[0].__setitem__(5, datetime(3082, 1, 1)), "fora do razoável"),
    (lambda r: r[0].__setitem__(8, 9999), "maior que valor_nominal"),
    (lambda r: r[0].__setitem__(0, 999), "não encontrada"),
    (lambda r: r[2].__setitem__(3, "1/-1") or r[2].__setitem__(4, 1), "título repetido"),
    (lambda r: r[0].__setitem__(7, None), "sem valor_nominal"),
])
def test_errors_block_apply(two, change, msg):
    rows = good_rows()
    change(rows)
    r = preview(two, xlsx(rows))
    assert msg in r.text and "impedem a aplicação" in r.text and 'action="/admin/conciliacao/aplicar"' not in r.text
    assert situacoes(two) == {1: "sem_lancamento", 2: "sem_lancamento"}


def test_warnings_do_not_block(two):
    rows = good_rows()
    rows[0][7] = rows[0][8] = 1300  # parcela não bate com o valor da nota
    r = preview(two, xlsx(rows))
    assert "parcelas somam R$ 1.300,00, a nota vale R$ 1.400,00" in r.text and "Aplicar" in r.text


def test_orphans_sheet_is_stored_and_listed(two):
    orf = [["FORNECEDOR SEM PDF LTDA", "11.111.111/0001-11", "2026145/-1", datetime(2026, 5, 25), datetime(2026, 6, 23),
            datetime(2026, 6, 22), 12900, "não há PDF com esse CNPJ"]]
    r = preview(two, xlsx(good_rows(), orphans=orf))
    apply(two, r.text)
    adm = two.get("/admin").text
    assert "1 título do ERP sem PDF no sistema" in adm and "FORNECEDOR SEM PDF LTDA" in adm and "R$ 12.900,00" in adm


def test_csv_import_and_missing_columns(two):
    csv_body = ("id_sistema;chave_acesso;data_lancamento;titulo;parcela;vencimento;data_pagamento;valor_nominal;valor_pago\n"
                f"1;{' '.join(DANFE_KEY[i:i + 4] for i in range(0, 44, 4))};11/03/2026;316/1-1;1;31/03/2026;30/03/2026;1.400,00;1.400,00\n")
    r = preview(two, csv_body.encode("utf-8-sig"), "c.csv")
    apply(two, r.text)
    assert situacoes(two)[1] == "paga"
    r = two.post("/admin/conciliacao", data={"csrf": two.csrf}, follow_redirects=True,
                 files={"arquivo": ("x.xlsx", xlsx([[1]], head=["id_sistema"]), "application/octet-stream")})
    assert "Faltam colunas no arquivo: chave_acesso" in r.text


def test_broken_file_and_expired_token(two):
    r = two.post("/admin/conciliacao", data={"csrf": two.csrf}, follow_redirects=True,
                 files={"arquivo": ("x.xlsx", b"PK\x03\x04 lixo", "application/octet-stream")})
    assert "Não foi possível abrir a planilha" in r.text
    r = two.post("/admin/conciliacao/aplicar", data={"csrf": two.csrf, "token": "inventado"}, follow_redirects=True)
    assert "A conferência expirou" in r.text


def test_only_admin(client):
    r = client.post("/admin/conciliacao", data={"csrf": "x"}, files={"arquivo": ("a.xlsx", b"x", "x")}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert client.get("/admin/exportar-dados.xlsx", follow_redirects=False).status_code == 303


def test_clear_conciliation(two):
    apply(two, preview(two, xlsx(good_rows())).text)
    r = two.post("/admin/nota/1/limpar-conciliacao", data={"csrf": two.csrf}, follow_redirects=True)
    assert "removidos" in r.text and situacoes(two)[1] == "sem_lancamento"


def test_note_page_shows_parcels(two):
    apply(two, preview(two, xlsx(good_rows())).text)
    html = two.get("/admin/nota/2").text
    assert "Lançamento e pagamento (ERP)" in html and "1/-1" in html and "pago em 26/06/2026" in html and "em aberto" in html


# ---------------------------------------------------------------- exportações

def test_public_xlsx_export(two):
    apply(two, preview(two, xlsx(good_rows())).text)
    r = two.get("/exportar.xlsx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb["Notas"]
    head = [c.value for c in ws[1]]
    rows = {ws.cell(row=i, column=3).value: [c.value for c in ws[i]] for i in range(2, 4)}
    danfe = dict(zip(head, rows["316"]))
    assert danfe["Chave de acesso"].replace(" ", "") == DANFE_KEY and isinstance(danfe["Chave de acesso"], str)
    assert danfe["Situação"] == "Paga" and danfe["Valor da nota"] == 1400 and danfe["Emissão"].date() == date(2026, 3, 4)
    assert ws.cell(row=ws.max_row, column=11).value.startswith("=SUM(")
    assert [c.value for c in wb["Parcelas"][1]][:5] == ["Tipo", "Número", "Fornecedor", "CNPJ/CPF", "Título"]
    assert wb["Parcelas"].max_row == 4


def test_admin_data_export_for_conciliation(two):
    r = two.get("/admin/exportar-dados.xlsx")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["LEIA-ME", "notas_sistema", "titulos_atuais", "modelo_conciliacao"]
    ws = wb["notas_sistema"]
    head = [c.value for c in ws[1]]
    assert head[:5] == ["id_sistema", "chave_acesso", "tipo", "numero", "numero_curto"]
    chaves = {ws.cell(row=i, column=2).value for i in range(2, ws.max_row + 1)}
    assert chaves == {DANFE_KEY, NFSE_KEY}  # texto, 44/50 dígitos inteiros
    assert [c.value for c in wb["modelo_conciliacao"][1]] == HEAD


def test_numero_curto():
    from app.services.sheets import numero_curto
    assert numero_curto("2026100000083") == "83" and numero_curto("316") == "316" and numero_curto("1") == "1"


def test_new_columns_are_added_to_existing_notes_table(tmp_path):
    from sqlalchemy import create_engine, inspect

    from app.database import init_db
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(text("create table notes (id integer primary key, kind varchar(8), chave varchar(50), numero varchar(20),"
                          " serie varchar(10), emit_nome varchar(300), emit_doc varchar(20), tom_nome varchar(300), tom_doc varchar(20),"
                          " data_emissao date, vencimento date, descricao text, valor_cents bigint, desconto_cents bigint,"
                          " categoria varchar(60), obs text, original_name varchar(255), file_sha256 varchar(64), file_size integer,"
                          " pages integer, encrypted boolean, pdf_data blob, created_at datetime)"))
        conn.execute(text("insert into notes (id, kind, chave, numero, emit_nome, emit_doc, data_emissao, valor_cents, file_sha256, file_size)"
                          " values (1, 'danfe', 'x', '1', 'A', 'B', '2026-01-01', 100, 'h', 1)"))
    init_db(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("notes")}
    assert {"data_lancamento", "situacao", "pago_cents", "conciliado_em"} <= cols
    with eng.connect() as conn:
        assert conn.execute(text("select situacao from notes")).scalar() == "sem_lancamento"


def test_sem_lancamento_card_only_when_there_are_unreconciled_notes(two):
    rows = good_rows()
    apply(two, preview(two, xlsx(rows[:1])).text)  # só a DANFE conciliada
    home = two.get("/").text
    assert 'class="st st-sem' in home and "1 nota ainda não conciliada" in home
    apply(two, preview(two, xlsx(rows)).text)  # todas conciliadas
    home = two.get("/").text
    assert 'class="st st-sem' not in home and 'class="status-strip two"' in home


def test_parcels_with_same_number_are_one_note():
    """Mesmo número/série no ERP = mesma nota; o que vem depois do último traço é a parcela."""
    import sys
    sys.path.insert(0, "scripts")
    from conciliar import parse_titulo
    assert parse_titulo("20264/-1")[:3] == ("20264", "", 1) and parse_titulo("20264/-2")[:3] == ("20264", "", 2)
    assert parse_titulo("1226/1-2")[:3] == ("1226", "1", 2)
    assert parse_titulo(datetime(3558, 1, 2)) == ("3558", "1", 2, True)  # Excel leu 3558/1-2 como data
