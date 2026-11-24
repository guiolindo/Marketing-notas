"""Conciliação com o contas a pagar do ERP.

O arquivo de conciliação (gerado pelo Claude a partir da exportação do sistema + planilha do ERP) tem uma linha por
título/parcela. Para cada nota que aparece no arquivo, as parcelas da nota são SUBSTITUÍDAS pelas do arquivo (reimportar
o mesmo arquivo não duplica nada). Notas que não aparecem ficam como estão.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ErpOrfao, Note, Titulo, utcnow

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
SITUACOES = {"paga": "Paga", "parcial": "Paga em parte", "a_pagar": "A pagar", "sem_lancamento": "Sem lançamento"}

REQUIRED = ("id_sistema", "chave_acesso")
COLUMNS = ("id_sistema", "chave_acesso", "data_lancamento", "titulo", "parcela", "vencimento", "data_pagamento",
           "valor_nominal", "valor_pago", "especie", "confianca", "observacao")
ORPHAN_COLUMNS = ("fornecedor", "cnpj", "titulo", "data_emissao", "vencimento", "data_pagamento", "valor", "motivo",
                  "data_lancamento", "especie")


class ImportError_(Exception):
    """Arquivo que não dá para ler (formato, cabeçalho, tamanho)."""


# ---------------------------------------------------------------- situação da nota

def recompute(note: Note) -> None:
    """Atualiza os totais e a situação da nota a partir das parcelas."""
    ts = list(note.titulos)
    note.nominal_cents = sum(t.valor_nominal_cents for t in ts)
    note.pago_cents = sum(t.valor_pago_cents for t in ts)
    pagos = [t.data_pagamento for t in ts if t.valor_pago_cents > 0 and t.data_pagamento]
    note.ultimo_pagamento = max(pagos) if pagos else None
    abertos = sorted(t.vencimento for t in ts if not t.pago and t.vencimento)
    note.proximo_vencimento = abertos[0] if abertos else None
    if not ts and not note.data_lancamento:
        note.situacao = "sem_lancamento"
    elif ts and note.pago_cents >= note.nominal_cents > 0:
        note.situacao = "paga"
    elif note.pago_cents > 0:
        note.situacao = "parcial"
    else:
        note.situacao = "a_pagar"


# ---------------------------------------------------------------- leitura das células

def _norm_header(h) -> str:
    s = unicodedata.normalize("NFKD", str(h or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _date(v, where: str) -> date | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    else:
        s = str(v).strip()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s) or None
        try:
            if m:
                d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}( .*)?", s):
                d = date.fromisoformat(s[:10])
            else:
                raise ValueError
        except ValueError:
            raise ValueError(f"{where}: data inválida ({s!r}). Use dd/mm/aaaa.") from None
    if not date(2000, 1, 1) <= d <= date.today() + timedelta(days=730):
        raise ValueError(f"{where}: data fora do razoável ({d:%d/%m/%Y}).")
    return d


def _money(v, where: str) -> int | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise ValueError(f"{where}: valor inválido.")
    if isinstance(v, (int, float)):
        d = Decimal(str(v))
    else:
        s = str(v).strip().replace("R$", "").replace(" ", "")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"{where}: valor inválido ({v!r}).") from None
    if not d.is_finite() or d < 0 or d > Decimal("1e10"):
        raise ValueError(f"{where}: valor fora do razoável ({v!r}).")
    return int((d * 100).quantize(Decimal("1")))


def _int(v, where: str) -> int | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        f = float(str(v).strip().replace(",", "."))
    except ValueError:
        raise ValueError(f"{where}: número inválido ({v!r}).") from None
    if f != int(f) or f < 0:
        raise ValueError(f"{where}: número inválido ({v!r}).")
    return int(f)


def _chave(v, where: str) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        raise ValueError(f"{where}: a chave virou número no Excel e perdeu dígitos. Formate a coluna como texto.")
    digits = re.sub(r"\D", "", str(v))
    if len(digits) not in (44, 50):
        raise ValueError(f"{where}: chave de acesso com {len(digits)} dígitos (esperado 44 ou 50).")
    return digits


def _text(v, limit: int) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v)).strip()[:limit]


# ---------------------------------------------------------------- leitura do arquivo

def _table_from_rows(rows: list[tuple], wanted: tuple[str, ...], required: tuple[str, ...]) -> list[dict]:
    rows = [r for r in rows if r and any(c not in (None, "") for c in r)]
    if not rows:
        return []
    header = [_norm_header(h) for h in rows[0]]
    aliases = {"id": "id_sistema", "chave": "chave_acesso", "chave_de_acesso": "chave_acesso", "lancamento": "data_lancamento",
               "pagamento": "data_pagamento", "dtaquitacao": "data_pagamento", "dtavencimento": "vencimento"}
    header = [aliases.get(h, h) for h in header]
    missing = [c for c in required if c not in header]
    if missing:
        raise ImportError_("Faltam colunas no arquivo: " + ", ".join(missing) + ".")
    idx = {c: header.index(c) for c in wanted if c in header}
    if len(rows) - 1 > MAX_ROWS:
        raise ImportError_(f"Arquivo com mais de {MAX_ROWS} linhas.")
    out = []
    for n, r in enumerate(rows[1:], start=2):
        rec = {c: (r[i] if i < len(r) else None) for c, i in idx.items()}
        rec["_linha"] = n
        out.append(rec)
    return out


def read_file(data: bytes, filename: str) -> tuple[list[dict], list[dict] | None]:
    """(linhas da conciliação, linhas de títulos sem PDF ou None se o arquivo não trouxer essa aba)."""
    if len(data) > MAX_BYTES:
        raise ImportError_("Arquivo maior que 5 MB.")
    name = (filename or "").lower()
    if data[:2] == b"PK" or name.endswith(".xlsx"):
        try:
            import openpyxl  # defusedxml instalado: openpyxl passa a usá-lo automaticamente
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise ImportError_("Não foi possível abrir a planilha (.xlsx corrompido?).") from exc
        try:
            names = {_norm_header(n): n for n in wb.sheetnames}
            main = wb[names["conciliacao"]] if "conciliacao" in names else wb.worksheets[0]
            rows = _table_from_rows(list(main.iter_rows(values_only=True, max_row=MAX_ROWS + 2)), COLUMNS, REQUIRED)
            orphans = None
            if "sem_pdf" in names:
                orphans = _table_from_rows(list(wb[names["sem_pdf"]].iter_rows(values_only=True, max_row=MAX_ROWS + 2)),
                                           ORPHAN_COLUMNS, ())
            return rows, orphans
        finally:
            wb.close()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    if "\x00" in text:
        raise ImportError_("Envie um arquivo .xlsx ou .csv.")
    first = text.splitlines()[0] if text else ""
    delim = max(";,\t", key=first.count)
    return _table_from_rows([tuple(r) for r in csv.reader(io.StringIO(text), delimiter=delim)], COLUMNS, REQUIRED), None


# ---------------------------------------------------------------- conferência (antes de aplicar)

@dataclass
class TituloIn:
    titulo: str
    parcela: int
    vencimento: date | None
    data_pagamento: date | None
    valor_nominal_cents: int
    valor_pago_cents: int
    especie: str

    def key(self) -> tuple:
        return (self.titulo, self.parcela, self.vencimento, self.data_pagamento, self.valor_nominal_cents,
                self.valor_pago_cents, self.especie)


@dataclass
class NotePlan:
    note_id: int
    label: str
    fornecedor: str
    valor_cents: int
    lanc_antes: date | None
    lanc_depois: date | None
    titulos_antes: int
    titulos: list[TituloIn]
    pago_depois: int = 0
    nominal_depois: int = 0
    situacao_antes: str = ""
    situacao_depois: str = ""
    confianca: str = ""
    obs: str = ""
    avisos: list[str] = field(default_factory=list)


@dataclass
class Plan:
    notes: list[NotePlan] = field(default_factory=list)
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    orphans: list[dict] | None = None
    filename: str = ""

    @property
    def warnings(self) -> int:
        return sum(len(n.avisos) for n in self.notes)


def build_plan(db: Session, rows: list[dict], orphan_rows: list[dict] | None, filename: str = "") -> Plan:
    plan = Plan(filename=filename)
    by_note: dict[int, list[tuple[dict, TituloIn | None, date | None]]] = {}
    notes: dict[int, Note] = {}
    for r in rows:
        where = f"Linha {r['_linha']}"
        try:
            nid = _int(r.get("id_sistema"), where + " (id_sistema)")
            chave = _chave(r.get("chave_acesso"), where + " (chave_acesso)")
            if nid is None and not chave:
                raise ValueError(f"{where}: sem id_sistema e sem chave_acesso.")
            note = db.get(Note, nid) if nid is not None else db.scalar(select(Note).where(Note.chave == chave))
            if note is None:
                raise ValueError(f"{where}: nota não encontrada no sistema (id {nid}).")
            if chave and note.chave != chave:
                raise ValueError(f"{where}: id_sistema {note.id} e chave_acesso não são da mesma nota.")
            lanc = _date(r.get("data_lancamento"), where + " (data_lancamento)")
            titulo_txt = _text(r.get("titulo"), 40)
            nominal = _money(r.get("valor_nominal"), where + " (valor_nominal)")
            t = None
            if titulo_txt or nominal is not None:
                if not nominal:
                    raise ValueError(f"{where}: título sem valor_nominal.")
                t = TituloIn(titulo=titulo_txt, parcela=_int(r.get("parcela"), where + " (parcela)") or 1,
                             vencimento=_date(r.get("vencimento"), where + " (vencimento)"),
                             data_pagamento=_date(r.get("data_pagamento"), where + " (data_pagamento)"),
                             valor_nominal_cents=nominal, valor_pago_cents=_money(r.get("valor_pago"), where + " (valor_pago)") or 0,
                             especie=_text(r.get("especie"), 20))
                if t.valor_pago_cents > t.valor_nominal_cents:
                    raise ValueError(f"{where}: valor_pago maior que valor_nominal.")
            notes[note.id] = note
            by_note.setdefault(note.id, []).append((r, t, lanc))
        except ValueError as exc:
            plan.errors.append(str(exc))

    for nid, items in by_note.items():
        note = notes[nid]
        avisos: list[str] = []
        lancs = sorted({lanc for _, _, lanc in items if lanc})
        if len(lancs) > 1:
            avisos.append(f"datas de lançamento diferentes nas linhas; usada a primeira ({lancs[0]:%d/%m/%Y}).")
        lanc = lancs[0] if lancs else None
        titulos = [t for _, t, _ in items if t]
        keys = [(t.titulo, t.parcela) for t in titulos]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            plan.errors.append(f"{note.label}: título repetido no arquivo ({', '.join(f'{a} parcela {b}' for a, b in dup)}).")
            continue
        nominal = sum(t.valor_nominal_cents for t in titulos)
        if titulos and abs(nominal - note.valor_cents) > 1:
            avisos.append(f"parcelas somam {_brl(nominal)}, a nota vale {_brl(note.valor_cents)}.")
        if lanc and lanc < note.data_emissao:
            avisos.append(f"lançamento ({lanc:%d/%m/%Y}) antes da emissão ({note.data_emissao:%d/%m/%Y}).")
        for t in titulos:
            if t.valor_pago_cents and t.data_pagamento and t.data_pagamento < note.data_emissao:
                avisos.append(f"pagamento ({t.data_pagamento:%d/%m/%Y}) antes da emissão.")
            if t.valor_pago_cents and not t.data_pagamento:
                avisos.append(f"título {t.titulo or t.parcela} pago sem data de pagamento.")
        conf = {_text(r.get("confianca"), 20).lower() for r, _, _ in items} - {""}
        obs = " · ".join(sorted({_text(r.get("observacao"), 150) for r, _, _ in items} - {""}))[:300]

        antes = sorted(TituloIn(t.titulo, t.parcela, t.vencimento, t.data_pagamento, t.valor_nominal_cents,
                                t.valor_pago_cents, t.especie).key() for t in note.titulos)
        depois = sorted(t.key() for t in titulos)
        if antes == depois and note.data_lancamento == lanc:
            plan.unchanged += 1
            continue
        pago = sum(t.valor_pago_cents for t in titulos)
        if not titulos and not lanc:
            sit = "sem_lancamento"
        elif titulos and pago >= nominal > 0:
            sit = "paga"
        elif pago > 0:
            sit = "parcial"
        else:
            sit = "a_pagar"
        plan.notes.append(NotePlan(
            note_id=nid, label=note.label, fornecedor=note.emit_nome, valor_cents=note.valor_cents, lanc_antes=note.data_lancamento,
            lanc_depois=lanc, titulos_antes=len(note.titulos), titulos=sorted(titulos, key=lambda t: (t.vencimento or date.max, t.parcela)),
            pago_depois=pago, nominal_depois=nominal, situacao_antes=note.situacao, situacao_depois=sit,
            confianca=", ".join(sorted(conf)), obs=obs, avisos=avisos))

    if orphan_rows is not None:
        plan.orphans = []
        for r in orphan_rows:
            where = f"Aba sem_pdf, linha {r['_linha']}"
            try:
                plan.orphans.append({
                    "fornecedor": _text(r.get("fornecedor"), 300), "cnpj": _text(r.get("cnpj"), 20),
                    "titulo": _text(r.get("titulo"), 40), "data_emissao": _date(r.get("data_emissao"), where),
                    "vencimento": _date(r.get("vencimento"), where), "data_pagamento": _date(r.get("data_pagamento"), where),
                    "valor_cents": _money(r.get("valor"), where) or 0, "motivo": _text(r.get("motivo"), 200),
                    "data_lancamento": _date(r.get("data_lancamento"), where), "especie": _text(r.get("especie"), 20)})
            except ValueError as exc:
                plan.errors.append(str(exc))
    plan.notes.sort(key=lambda n: (n.fornecedor.lower(), n.label))
    return plan


def apply_plan(db: Session, plan: Plan) -> int:
    now = utcnow()
    applied = 0
    for np_ in plan.notes:
        note = db.get(Note, np_.note_id)
        if note is None:  # excluída entre a conferência e a aplicação
            continue
        note.titulos.clear()
        db.flush()
        for t in np_.titulos:
            note.titulos.append(Titulo(titulo=t.titulo, parcela=t.parcela, vencimento=t.vencimento,
                                       data_pagamento=t.data_pagamento, valor_nominal_cents=t.valor_nominal_cents,
                                       valor_pago_cents=t.valor_pago_cents, especie=t.especie))
        note.data_lancamento = np_.lanc_depois
        note.conciliado_em = now
        note.conciliacao_obs = np_.obs
        recompute(note)
        applied += 1
    if plan.orphans is not None:
        db.execute(delete(ErpOrfao))
        db.add_all(ErpOrfao(**o) for o in plan.orphans)
    db.commit()
    return applied


def clear_note(db: Session, note: Note) -> None:
    note.titulos.clear()
    note.data_lancamento = None
    note.conciliado_em = None
    note.conciliacao_obs = ""
    recompute(note)
    db.commit()


def _brl(cents: int) -> str:
    from app.services.notes import brl
    return brl(cents)


class PendingImports:
    """Conferências aguardando o "Aplicar" (memória do processo, 30 min)."""

    TTL = 1800

    def __init__(self):
        self._d: dict[str, tuple[float, Plan]] = {}
        self._lock = threading.Lock()

    def put(self, plan: Plan) -> str:
        token = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._d = {k: v for k, v in self._d.items() if now - v[0] < self.TTL}
            while len(self._d) >= 5:
                self._d.pop(next(iter(self._d)))
            self._d[token] = (now, plan)
        return token

    def get(self, token: str) -> Plan | None:
        with self._lock:
            item = self._d.get(token)
        return item[1] if item and time.monotonic() - item[0] < self.TTL else None

    def pop(self, token: str) -> Plan | None:
        with self._lock:
            item = self._d.pop(token, None)
        return item[1] if item and time.monotonic() - item[0] < self.TTL else None
