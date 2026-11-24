"""Exportações: planilha Excel (.xlsx) e o arquivo de dados para a conciliação (admin)."""
from __future__ import annotations

import io
import re
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import Note
from app.services.conciliacao import COLUMNS, ORPHAN_COLUMNS, SITUACOES

NAVY, ORANGE, SOFT = "1B4F8A", "F47920", "EAF2FA"
MONEY = 'R$ #,##0.00'
DATE = "DD/MM/YYYY"


def chave_formatada(chave: str) -> str:
    """Chave em grupos de 4 dígitos, como no DANFE: nunca vira número no Excel."""
    return " ".join(chave[i:i + 4] for i in range(0, len(chave), 4))


def tipo(n: Note) -> str:
    return "NFS-e" if n.kind == "nfse" else "DANFE"


def numero_curto(numero: str) -> str:
    """NFS-e nacional 2026100000083 -> 83 (o ERP costuma ter só o fim do número)."""
    if len(numero) >= 11 and re.fullmatch(r"20\d{2}\d+", numero):
        return str(int(numero[5:]) if numero[4] in "01" else int(numero[4:]))
    return numero


def _style_header(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"


def _fit(ws, widths: dict[int, int]) -> None:
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def _no_formula(ws) -> None:
    """Texto que começa com = vira fórmula no openpyxl: um nome de fornecedor ou categoria "=HYPERLINK(...)" viraria
    fórmula ativa na planilha. Força todas as células de texto a serem texto."""
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if cell.data_type == "f":
                cell.data_type = "s"


def _write(ws, headers: list[str], rows: list[list], formats: dict[int, str], widths: dict[int, int]) -> None:
    ws.append(headers)
    for r in rows:
        ws.append(r)
    _no_formula(ws)
    for c, fmt in formats.items():
        for row in ws.iter_rows(min_row=2, min_col=c, max_col=c):
            for cell in row:
                cell.number_format = fmt
    _style_header(ws, len(headers))
    _fit(ws, widths)
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"


def _to_bytes(wb: Workbook) -> bytes:
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------------------------------------------------------- planilha para todos

NOTE_HEADERS = ["Situação", "Tipo", "Número", "Série", "Fornecedor", "CNPJ/CPF", "Emissão", "Lançamento",
                "Próximo vencimento", "Último pagamento", "Valor da nota", "Pago", "Em aberto", "Parcelas", "Descrição",
                "Categoria", "Chave de acesso"]


def _note_row(n: Note) -> list:
    return [SITUACOES.get(n.situacao, ""), tipo(n), n.numero, n.serie, n.emit_nome, n.emit_doc, n.data_emissao,
            n.data_lancamento, n.proximo_vencimento, n.ultimo_pagamento, n.valor_cents / 100, (n.pago_cents or 0) / 100,
            n.aberto_cents / 100, len(n.titulos), n.descricao, n.categoria,
            chave_formatada(n.chave) if n.tem_chave else "não informada (PDF escaneado)"]


def build_xlsx(notes: list[Note]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Notas"
    rows = [_note_row(n) for n in notes]
    _write(ws, NOTE_HEADERS, rows, {3: "@", 4: "@", 7: DATE, 8: DATE, 9: DATE, 10: DATE, 11: MONEY, 12: MONEY, 13: MONEY, 17: "@"},
           {1: 15, 2: 8, 3: 16, 4: 7, 5: 38, 6: 20, 7: 12, 8: 12, 9: 12, 10: 12, 11: 15, 12: 15, 13: 15, 14: 9, 15: 45,
            16: 16, 17: 60})
    if rows:
        last = len(rows) + 1
        ws.append([])
        ws.append(["Total", None, None, None, f"{len(rows)} notas"] + [None] * 5
                  + [f"=SUM(K2:K{last})", f"=SUM(L2:L{last})", f"=SUM(M2:M{last})"])
        total = ws.max_row
        for c in range(1, 18):
            ws.cell(row=total, column=c).font = Font(bold=True)
            ws.cell(row=total, column=c).fill = PatternFill("solid", fgColor=SOFT)
        for c in (11, 12, 13):
            ws.cell(row=total, column=c).number_format = MONEY

    wp = wb.create_sheet("Parcelas")
    prow = [[tipo(n), n.numero, n.emit_nome, n.emit_doc, t.titulo, t.parcela, t.vencimento,
             t.data_pagamento if t.valor_pago_cents else None, t.valor_nominal_cents / 100, t.valor_pago_cents / 100,
             "Paga" if t.pago else ("Pagamento previsto" if t.data_pagamento else "Em aberto"),
             chave_formatada(n.chave) if n.tem_chave else ""]
            for n in notes for t in n.titulos]
    _write(wp, ["Tipo", "Número", "Fornecedor", "CNPJ/CPF", "Título", "Parcela", "Vencimento", "Pagamento", "Valor",
                "Pago", "Situação", "Chave de acesso"], prow, {2: "@", 5: "@", 7: DATE, 8: DATE, 9: MONEY, 10: MONEY, 12: "@"},
           {1: 8, 2: 16, 3: 38, 4: 20, 5: 16, 6: 8, 7: 12, 8: 12, 9: 14, 10: 14, 11: 18, 12: 60})
    return _to_bytes(wb)


# ---------------------------------------------------------------- dados para a conciliação (admin)

README = [
    ["Dados do sistema para a conciliação com o contas a pagar do ERP"],
    [""],
    ["Como usar"],
    ["1. Envie este arquivo e a planilha do ERP (contas a pagar) para o Claude."],
    ["2. O Claude cruza as duas (CNPJ, valor, data de emissão e número, mesmo com zeros cortados ou número que virou data)"],
    ["   e devolve o ARQUIVO DE CONCILIAÇÃO no formato abaixo."],
    ["3. No sistema: /admin → Conciliação → Importar. Confira a prévia e clique em Aplicar."],
    [""],
    ["Formato do arquivo de conciliação (.xlsx)"],
    ["Aba 'conciliacao': uma linha por título/parcela do ERP. Colunas:"],
    ["  id_sistema       obrigatório: coluna id_sistema da aba notas_sistema"],
    ["  chave_acesso     obrigatório: chave de 44/50 dígitos COMO TEXTO (confere com o id_sistema)"],
    ["  data_lancamento  DATALANÇAMENTO do ERP"],
    ["  titulo           TÍTULO como no ERP, ex. 316/1-1 (corrigido se o Excel tiver transformado em data)"],
    ["  parcela          número da parcela (1, 2, ...)"],
    ["  vencimento       DTAVENCIMENTO"],
    ["  data_pagamento   DTAQUITAÇÃO"],
    ["  valor_nominal    VLRNOMINAL"],
    ["  valor_pago       VLRPAGO (0 = ainda não pago)"],
    ["  especie          CODESPÉCIE (opcional)"],
    ["  confianca        alta / media (opcional, mostrado na conferência)"],
    ["  observacao       como a linha foi casada (opcional, guardado na nota)"],
    ["Aba 'sem_pdf' (opcional): títulos do ERP sem nota no sistema. Colunas: " + ", ".join(ORPHAN_COLUMNS)],
    [""],
    ["Regras da importação"],
    ["- Cada nota presente no arquivo tem as parcelas SUBSTITUÍDAS pelas do arquivo (reimportar não duplica)."],
    ["- Notas ausentes no arquivo não mudam. Datas: dd/mm/aaaa ou células de data. Nada é gravado antes da conferência."],
]


def build_system_export(notes: list[Note]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "notas_sistema"
    rows = [[n.id, n.chave if n.tem_chave else "", tipo(n), n.numero, numero_curto(n.numero), n.serie, n.emit_nome, n.emit_doc,
             re.sub(r"\D", "", n.emit_doc), n.data_emissao, n.vencimento, n.valor_cents / 100, (n.desconto_cents or 0) / 100,
             n.descricao, n.categoria, n.data_lancamento, n.situacao, len(n.titulos), n.original_name] for n in notes]
    _write(ws, ["id_sistema", "chave_acesso", "tipo", "numero", "numero_curto", "serie", "emitente", "cnpj_emitente",
                "cnpj_digitos", "data_emissao", "vencimento_pdf", "valor", "desconto", "descricao", "categoria",
                "data_lancamento_atual", "situacao_atual", "parcelas_atuais", "arquivo_pdf"], rows,
           {2: "@", 4: "@", 5: "@", 6: "@", 9: "@", 10: DATE, 11: DATE, 12: MONEY, 13: MONEY, 16: DATE},
           {1: 11, 2: 52, 3: 8, 4: 16, 5: 13, 6: 7, 7: 38, 8: 20, 9: 16, 10: 12, 11: 12, 12: 14, 13: 12, 14: 40, 15: 14,
            16: 14, 17: 15, 18: 9, 19: 30})

    wt = wb.create_sheet("titulos_atuais")
    trows = [[n.id, n.chave if n.tem_chave else "", n.data_lancamento, t.titulo, t.parcela, t.vencimento, t.data_pagamento,
              t.valor_nominal_cents / 100, t.valor_pago_cents / 100, t.especie] for n in notes for t in n.titulos]
    _write(wt, list(COLUMNS[:10]), trows, {2: "@", 3: DATE, 4: "@", 6: DATE, 7: DATE, 8: MONEY, 9: MONEY},
           {1: 11, 2: 52, 3: 14, 4: 16, 5: 8, 6: 12, 7: 14, 8: 14, 9: 14, 10: 10})

    wm = wb.create_sheet("modelo_conciliacao")
    _write(wm, list(COLUMNS), [], {2: "@", 4: "@"}, {i + 1: 16 for i in range(len(COLUMNS))})
    wr = wb.create_sheet("LEIA-ME")
    for line in README:
        wr.append(line)
    wr["A1"].font = Font(bold=True, size=14, color=NAVY)
    for r in (3, 9, 25):
        wr.cell(row=r, column=1).font = Font(bold=True, color=ORANGE)
    wr.column_dimensions["A"].width = 120
    wb.move_sheet("LEIA-ME", offset=-3)
    wb.active = 0
    return _to_bytes(wb)


def stamp() -> str:
    return date.today().strftime("%Y-%m-%d")
