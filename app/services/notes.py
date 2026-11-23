"""Lançamento (com bloqueio de duplicidade), filtros, totais, CSV e PDF único."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import Vault
from app.models import Note
from app.parsing import ParseError, PdfRejected, parse_pdf

MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def brl(cents: int | None) -> str:
    cents = cents or 0
    sign = "-" if cents < 0 else ""
    reais, cs = divmod(abs(int(cents)), 100)
    return f"{sign}R$ {reais:,}".replace(",", ".") + f",{cs:02d}"


# ---------------------------------------------------------------- lançamento

@dataclass
class IngestResult:
    filename: str
    status: str  # ok | duplicate | invalid
    message: str
    note: Note | None = None


def _clean_filename(name: str) -> str:
    base = (name or "nota.pdf").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in base if c.isprintable())[:255] or "nota.pdf"


def ingest(db: Session, vault: Vault, filename: str, data: bytes, max_bytes: int) -> IngestResult:
    filename = _clean_filename(filename)
    if not data:
        return IngestResult(filename, "invalid", "Arquivo vazio.")
    if len(data) > max_bytes:
        return IngestResult(filename, "invalid", f"Arquivo maior que {max_bytes // (1024 * 1024)} MB.")

    sha = hashlib.sha256(data).hexdigest()
    same_file = db.scalar(select(Note).where(Note.file_sha256 == sha))
    if same_file:
        return IngestResult(filename, "duplicate", f"Este arquivo já foi lançado ({same_file.label}).", same_file)

    try:
        p = parse_pdf(data)
    except (PdfRejected, ParseError) as exc:
        return IngestResult(filename, "invalid", str(exc))

    same_doc = db.scalar(select(Note).where(or_(
        Note.chave == p.chave,
        (Note.kind == p.kind) & (Note.emit_doc == p.emit_doc) & (Note.serie == p.serie) & (Note.numero == p.numero))))
    if same_doc:
        return IngestResult(filename, "duplicate",
                            f"Esta nota já foi lançada ({same_doc.label} de {same_doc.emit_nome}).", same_doc)

    note = Note(
        kind=p.kind, chave=p.chave, numero=p.numero, serie=p.serie, emit_nome=p.emit_nome, emit_doc=p.emit_doc,
        tom_nome=p.tom_nome, tom_doc=p.tom_doc, data_emissao=p.data_emissao, vencimento=p.vencimento,
        descricao=p.descricao, valor_cents=p.valor_cents, desconto_cents=p.desconto_cents,
        original_name=filename, file_sha256=sha, file_size=len(data), pages=p.pages,
        encrypted=vault.enabled, pdf_data=vault.seal(data))
    db.add(note)
    try:
        db.commit()
    except IntegrityError:  # dois envios simultâneos da mesma nota: o banco barra o segundo
        db.rollback()
        return IngestResult(filename, "duplicate", "Esta nota já foi lançada.")
    return IngestResult(filename, "ok", f"{note.label} · {brl(note.valor_cents)}", note)


# ---------------------------------------------------------------- filtros

ORDENS = {
    "data_desc": (Note.data_emissao.desc(), Note.id.desc()),
    "data_asc": (Note.data_emissao.asc(), Note.id.asc()),
    "valor_desc": (Note.valor_cents.desc(), Note.id.desc()),
    "valor_asc": (Note.valor_cents.asc(), Note.id.asc()),
    "fornecedor": (func.lower(Note.emit_nome).asc(), Note.data_emissao.desc()),
    "pagamento": (Note.ultimo_pagamento.desc().nulls_last(), Note.id.desc()),
    "vencimento": (Note.proximo_vencimento.asc().nulls_last(), Note.id.asc()),
}
SITUACAO_FILTRO = ("paga", "parcial", "a_pagar", "sem_lancamento", "em_aberto")


def _date(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def _money(s: str | None) -> int | None:
    s = (s or "").strip().replace("R$", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    return int(v * 100) if v.is_finite() and 0 <= v < Decimal("1e12") else None


@dataclass
class Filters:
    q: str = ""
    kind: str = ""
    fornecedor: str = ""
    categoria: str = ""
    de: date | None = None
    ate: date | None = None
    vmin: int | None = None
    vmax: int | None = None
    situacao: str = ""
    ordem: str = "data_desc"
    raw: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> "Filters":
        g = lambda k: (params.get(k) or "").strip()  # noqa: E731
        f = cls(q=g("q")[:100], kind=g("tipo") if g("tipo") in ("nfse", "danfe") else "",
                fornecedor=g("fornecedor")[:20], categoria=g("categoria")[:60],
                de=_date(g("de")), ate=_date(g("ate")), vmin=_money(g("vmin")), vmax=_money(g("vmax")),
                situacao=g("situacao") if g("situacao") in SITUACAO_FILTRO else "",
                ordem=g("ordem") if g("ordem") in ORDENS else "data_desc")
        f.raw = {k: v for k, v in {
            "q": f.q, "tipo": f.kind, "fornecedor": f.fornecedor, "categoria": f.categoria,
            "de": f.de.isoformat() if f.de else "", "ate": f.ate.isoformat() if f.ate else "",
            "vmin": g("vmin") if f.vmin is not None else "", "vmax": g("vmax") if f.vmax is not None else "", "situacao": f.situacao,
            "ordem": f.ordem if f.ordem != "data_desc" else ""}.items() if v}
        return f

    @property
    def active(self) -> bool:
        return any(k != "ordem" for k in self.raw)

    def where(self) -> list:
        w = []
        if self.q:
            like = "%" + self.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            cols = (Note.emit_nome, Note.tom_nome, Note.descricao, Note.numero, Note.chave, Note.categoria, Note.obs,
                    Note.emit_doc)
            w.append(or_(*[c.ilike(like, escape="\\") for c in cols]))
        if self.kind:
            w.append(Note.kind == self.kind)
        if self.fornecedor:
            w.append(Note.emit_doc == self.fornecedor)
        if self.categoria:
            w.append(Note.categoria == self.categoria)
        if self.de:
            w.append(Note.data_emissao >= self.de)
        if self.ate:
            w.append(Note.data_emissao <= self.ate)
        if self.vmin is not None:
            w.append(Note.valor_cents >= self.vmin)
        if self.vmax is not None:
            w.append(Note.valor_cents <= self.vmax)
        if self.situacao == "em_aberto":
            w.append(Note.situacao.in_(("a_pagar", "parcial")))
        elif self.situacao:
            w.append(Note.situacao == self.situacao)
        return w


def query_notes(f: Filters):
    return select(Note).where(*f.where()).order_by(*ORDENS[f.ordem])


# ---------------------------------------------------------------- totais e gráficos

@dataclass
class Stats:
    count: int = 0
    total: int = 0
    maior: int = 0
    desconto: int = 0
    months: list[dict] = field(default_factory=list)
    top: list[dict] = field(default_factory=list)
    by_kind: dict[str, int] = field(default_factory=dict)
    view: tuple[int, int] = (560, 150)
    pago: int = 0
    aberto: int = 0
    sem_lanc: int = 0
    n_pagas: int = 0
    n_aberto: int = 0
    n_sem_lanc: int = 0
    proximo: object = None  # data do próximo vencimento em aberto

    @property
    def media(self) -> int:
        return round(self.total / self.count) if self.count else 0


def compute_stats(db: Session, f: Filters) -> Stats:
    rows = db.execute(select(Note.data_emissao, Note.valor_cents, Note.desconto_cents, Note.emit_doc, Note.emit_nome,
                             Note.kind, Note.situacao, Note.nominal_cents, Note.pago_cents, Note.proximo_vencimento)
                      .where(*f.where())).all()
    st = Stats(count=len(rows))
    by_month: dict[tuple[int, int], int] = defaultdict(int)
    by_forn: dict[str, list] = {}
    for d, v, desc, doc, nome, kind, sit, nominal, pago, prox in rows:
        if sit == "sem_lancamento":
            st.sem_lanc += v
            st.n_sem_lanc += 1
        else:
            st.pago += pago or 0
            st.aberto += max((nominal or v) - (pago or 0), 0)
            st.n_pagas += sit == "paga"
            st.n_aberto += sit in ("a_pagar", "parcial")
            if prox and sit != "paga" and (st.proximo is None or prox < st.proximo):
                st.proximo = prox
        st.total += v
        st.desconto += desc or 0
        st.maior = max(st.maior, v)
        by_month[(d.year, d.month)] += v
        by_forn.setdefault(doc, [nome, 0, 0])
        by_forn[doc][1] += v
        by_forn[doc][2] += 1
        st.by_kind[kind] = st.by_kind.get(kind, 0) + v

    months = sorted(by_month.items())[-12:]
    top_m = max((v for _, v in months), default=0) or 1
    n = len(months)
    W, H = 560, 150
    slot = W / max(n, 1)
    bw = min(40, slot * 0.62)
    for i, ((y, m), v) in enumerate(months):
        h = max(3, round((H - 22) * v / top_m)) if v else 0
        st.months.append({"x": round(i * slot + (slot - bw) / 2, 1), "w": round(bw, 1), "h": h, "y": H - 18 - h,
                          "cx": round(i * slot + slot / 2, 1), "label": f"{MESES[m - 1]}/{str(y)[2:]}",
                          "value": brl(v), "last": i == n - 1})
    st.view = (W, H)

    ranked = sorted(by_forn.items(), key=lambda kv: kv[1][1], reverse=True)[:6]
    peak = ranked[0][1][1] if ranked else 1
    for doc, (nome, v, c) in ranked:
        st.top.append({"doc": doc, "nome": nome, "total": brl(v), "count": c, "pct": round(100 * v / peak),
                       "share": round(100 * v / st.total) if st.total else 0})
    return st


def filter_options(db: Session) -> tuple[list[tuple[str, str]], list[str]]:
    forn = db.execute(select(Note.emit_doc, func.max(Note.emit_nome)).group_by(Note.emit_doc)
                      .order_by(func.lower(func.max(Note.emit_nome)))).all()
    cats = db.scalars(select(Note.categoria).where(Note.categoria != "").distinct().order_by(Note.categoria)).all()
    return [(d, n) for d, n in forn], list(cats)
