"""Gera o ARQUIVO DE CONCILIAÇÃO a partir de:
  1) a exportação "Dados do sistema" (/admin → Conciliação → passo 1)
  2) a planilha do contas a pagar do ERP (PESSOA, CNPJ/CPF, TÍTULO, DTAVENCIMENTO, DTAQUITAÇÃO, VLRNOMINAL, VLRPAGO,
     DATAEMISSÃO, DATALANÇAMENTO...)

Uso:  python -I scripts/conciliar.py dados-sistema.xlsx contas-a-pagar.xlsx -o conciliacao.xlsx

Casamento (por documento do ERP = mesmo CNPJ + número + série, somando as parcelas):
  CNPJ igual é obrigatório; depois pontua  valor total (3) · data de emissão (3) · número (3, ou 2 se só "parecido":
  zeros cortados, só o fim, ano na frente). alta = 9, média >= 6. O resto vai para a aba "revisar" (conferência manual).
Títulos que o Excel transformou em data (3082/1-1 -> 01/01/3082) são reconstruídos como ANO/MÊS-DIA.
O arquivo gerado é revisado por quem roda o script antes de ir para o sistema.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill


def norm(h) -> str:
    s = unicodedata.normalize("NFKD", str(h or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def d(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and re.fullmatch(r"\d{2}/\d{2}/\d{4}", v.strip()):
        dd, mm, yy = v.strip().split("/")
        return date(int(yy), int(mm), int(dd))
    return None


def cents(v) -> int:
    if v is None or v == "":
        return 0
    if isinstance(v, str):
        v = v.replace("R$", "").replace(".", "").replace(",", ".").strip()
    return int((Decimal(str(v)) * 100).quantize(Decimal("1")))


def digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def parse_titulo(v) -> tuple[str, str, int, bool]:
    """(numero, serie, parcela, era_data). '316/1-1' -> ('316','1',1); '2026145/-1' -> ('2026145','',1)."""
    if isinstance(v, (datetime, date)):  # Excel leu "3082/1-1" como 01/01/3082
        return str(v.year), str(v.month), v.day, True
    s = str(v or "").strip()
    m = re.fullmatch(r"(\d+)\s*/\s*(\d*)\s*-\s*(\d+)", s)
    if m:
        return m.group(1).lstrip("0") or "0", m.group(2), int(m.group(3)), False
    m = re.fullmatch(r"(\d+)", s)
    return ((m.group(1).lstrip("0") or "0"), "", 1, False) if m else (digits(s), "", 1, False)


def numero_score(erp: str, sist: str, curto: str) -> tuple[int, str]:
    if not erp:
        return 0, ""
    if erp in (sist.lstrip("0"), curto):
        return 3, "número igual"
    z = lambda x: x.replace("0", "")  # noqa: E731
    if z(erp) and z(erp) == z(sist):
        return 2, "número igual sem os zeros"
    if len(erp) >= 2 and (sist.endswith(erp) or erp.endswith(curto)):
        return 2, "número igual no final"
    if len(sist) >= 11 and sist[:4] == erp[:4] and z(sist[4:]) == z(erp[4:]):
        return 2, "ano + sequência iguais"
    return 0, ""


@dataclass
class Doc:
    pessoa: str
    cnpj: str
    numero: str
    serie: str
    emissao: date | None
    lancamento: date | None
    era_data: bool
    parcelas: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(p["nominal"] for p in self.parcelas)


def read_erp(path: str) -> list[Doc]:
    ws = load_workbook(path, data_only=True, read_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [norm(h) for h in rows[0]]
    col = lambda *names: next(head.index(n) for n in names if n in head)  # noqa: E731
    c = {k: col(*v) for k, v in {
        "pessoa": ("pessoa",), "cnpj": ("cnpjcpf", "cnpj"), "titulo": ("titulo",), "especie": ("codespecie",),
        "venc": ("dtavencimento",), "quit": ("dtaquitacao",), "nominal": ("vlrnominal",), "pago": ("vlrpago",),
        "emissao": ("dataemissao",), "lanc": ("datalancamento",)}.items()}
    docs: dict[tuple, Doc] = {}
    for r in rows[1:]:
        if not r or all(v in (None, "") for v in r):
            continue
        num, serie, parc, era = parse_titulo(r[c["titulo"]])
        cnpj = digits(r[c["cnpj"]])
        em = d(r[c["emissao"]])
        key = (cnpj, num, serie, em)
        doc = docs.setdefault(key, Doc(str(r[c["pessoa"]] or ""), cnpj, num, serie, em, d(r[c["lanc"]]), era))
        doc.parcelas.append({"titulo": f"{num}/{serie}-{parc}", "parcela": parc, "venc": d(r[c["venc"]]),
                             "quit": d(r[c["quit"]]), "nominal": cents(r[c["nominal"]]), "pago": cents(r[c["pago"]]),
                             "especie": str(r[c["especie"]] or ""), "original": r[c["titulo"]]})
    return list(docs.values())


def read_system(path: str) -> list[dict]:
    ws = load_workbook(path, data_only=True, read_only=True)["notas_sistema"]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(h) for h in rows[0]]
    out = []
    for r in rows[1:]:
        if r and r[0] is not None:
            rec = dict(zip(head, r))
            rec["valor_c"] = cents(rec["valor"])
            rec["emissao"] = d(rec["data_emissao"])
            out.append(rec)
    return out


def score(doc: Doc, n: dict) -> tuple[int, list[str]]:
    if doc.cnpj != str(n["cnpj_digitos"]):
        return 0, []
    pts, why = 0, ["CNPJ"]
    if abs(doc.total - n["valor_c"]) <= 1:
        pts, why = pts + 3, why + ["valor"]
    if doc.emissao and doc.emissao == n["emissao"]:
        pts, why = pts + 3, why + ["emissão"]
    s, w = numero_score(doc.numero, str(n["numero"]), str(n["numero_curto"]))
    if s:
        pts, why = pts + s, why + [w]
    return pts, why


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sistema")
    ap.add_argument("erp")
    ap.add_argument("-o", "--out", default="conciliacao.xlsx")
    a = ap.parse_args()
    notes, docs = read_system(a.sistema), read_erp(a.erp)

    pairs = sorted(((score(doc, n), di, n["id_sistema"]) for di, doc in enumerate(docs) for n in notes), key=lambda x: -x[0][0])
    used_doc, used_note, match = set(), set(), {}
    for (pts, why), di, nid in pairs:
        if pts >= 6 and di not in used_doc and nid not in used_note:
            match[di] = (nid, pts, why)
            used_doc.add(di)
            used_note.add(nid)
    by_id = {n["id_sistema"]: n for n in notes}

    wb = Workbook()
    ws = wb.active
    ws.title = "conciliacao"
    ws.append(["id_sistema", "chave_acesso", "data_lancamento", "titulo", "parcela", "vencimento", "data_pagamento",
               "valor_nominal", "valor_pago", "especie", "confianca", "observacao"])
    for di, (nid, pts, why) in sorted(match.items(), key=lambda x: x[1][0]):
        doc, n = docs[di], by_id[nid]
        conf = "alta" if pts >= 9 else "media"
        obs = "casado por " + " + ".join(why) + (f"; título era data no Excel ({doc.parcelas[0]['original']:%d/%m/%Y})"
                                                  if doc.era_data else "")
        for p in sorted(doc.parcelas, key=lambda p: p["parcela"]):
            ws.append([nid, str(n["chave_acesso"]), doc.lancamento, p["titulo"], p["parcela"], p["venc"], p["quit"],
                       p["nominal"] / 100, p["pago"] / 100, p["especie"], conf, obs[:150]])
    for row in ws.iter_rows(min_row=2):
        row[1].number_format = row[3].number_format = "@"
        for i in (2, 5, 6):
            row[i].number_format = "DD/MM/YYYY"

    wo = wb.create_sheet("sem_pdf")
    wo.append(["fornecedor", "cnpj", "titulo", "data_emissao", "vencimento", "data_pagamento", "valor", "motivo"])
    rv = wb.create_sheet("revisar")
    rv.append(["fornecedor", "titulo_erp", "emissao_erp", "valor_erp", "candidato_id", "candidato", "pontos", "motivo"])
    sist_cnpjs = {str(n["cnpj_digitos"]) for n in notes}
    for di, doc in enumerate(docs):
        if di in match:
            continue
        best = max(((score(doc, n), n) for n in notes), key=lambda x: x[0][0], default=((0, []), None))
        motivo = "nenhuma nota com este CNPJ no sistema" if doc.cnpj not in sist_cnpjs else (
            "há nota do fornecedor, mas valor/data/número não conferem")
        for p in doc.parcelas:
            wo.append([doc.pessoa, doc.cnpj, p["titulo"], doc.emissao, p["venc"], p["quit"] if p["pago"] else None,
                       p["nominal"] / 100, motivo])
        if best[1] is not None and best[0][0] >= 3:
            n = best[1]
            rv.append([doc.pessoa, doc.parcelas[0]["titulo"], doc.emissao, doc.total / 100, n["id_sistema"],
                       f"{n['tipo']} {n['numero']} · {n['valor']} · {n['emissao']}", best[0][0], " + ".join(best[0][1])])
    ns = wb.create_sheet("sistema_sem_erp")
    ns.append(["id_sistema", "tipo", "numero", "emitente", "data_emissao", "valor"])
    for n in notes:
        if n["id_sistema"] not in used_note:
            ns.append([n["id_sistema"], n["tipo"], str(n["numero"]), n["emitente"], n["emissao"], n["valor"]])
    for w in wb.worksheets:
        for c in w[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1B4F8A")
    wb.save(a.out)

    alta = sum(1 for v in match.values() if v[1] >= 9)
    print(f"ERP: {len(docs)} documentos ({sum(len(x.parcelas) for x in docs)} títulos) · sistema: {len(notes)} notas")
    print(f"casados: {len(match)} (alta {alta}, média {len(match) - alta}) · ERP sem PDF: {len(docs) - len(match)} documentos"
          f" · revisar: {rv.max_row - 1} · sistema sem ERP: {ns.max_row - 1}")
    print(f"gerado: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
