"""Vincular um PDF escaneado (sem texto) a um título do ERP que estava sem PDF.

Sem OCR: os dados da nota vêm do título do ERP (fornecedor, CNPJ, emissão, valor, vencimento, pagamento). O PDF só é
validado (segurança, duplicidade) e guardado. Títulos irmãos (mesmo CNPJ + número + série, parcelas diferentes) entram
juntos como parcelas da mesma nota.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import Vault
from app.models import ErpIgnorado, ErpOrfao, Note, Titulo, utcnow
from app.parsing import PdfRejected, nfe_key_is_valid, open_pdf
from app.services.conciliacao import recompute


def parse_titulo(titulo: str) -> tuple[str, str, int]:
    """'1616/-1' -> ('1616', '', 1); '316/1-2' -> ('316', '1', 2)."""
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d*)\s*-\s*(\d+)\s*", titulo or "")
    if m:
        return m.group(1).lstrip("0") or "0", m.group(2), int(m.group(3))
    return re.sub(r"\D", "", titulo or "").lstrip("0"), "", 1


def guess_kind(orfao: ErpOrfao) -> str:
    """DESPMK no ERP e título com série (316/1-1) costumam ser DANFE; o resto, NFS-e."""
    _, serie, _ = parse_titulo(orfao.titulo)
    return "danfe" if (orfao.especie or "").upper().startswith("DESP") or serie else "nfse"


def nome_erp(nome: str) -> str:
    return re.sub(r"\s+-\s+\d+$", "", nome or "").strip()


def cnpj_mask(doc: str) -> str:
    d = re.sub(r"\D", "", doc or "")
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return doc or ""


def siblings(db: Session, orfao: ErpOrfao) -> list[ErpOrfao]:
    """Parcelas da mesma nota que também estão sem PDF."""
    num, serie, _ = parse_titulo(orfao.titulo)
    digits = re.sub(r"\D", "", orfao.cnpj)
    out = [o for o in db.scalars(select(ErpOrfao)).all()
           if re.sub(r"\D", "", o.cnpj) == digits and parse_titulo(o.titulo)[:2] == (num, serie)]
    return sorted(out, key=lambda o: parse_titulo(o.titulo)[2]) or [orfao]


@dataclass
class LinkResult:
    note: Note | None = None
    error: str = ""


def link_pdf(db: Session, vault: Vault, orfao_id: int, data: bytes, filename: str, max_bytes: int, *,
             kind: str, numero: str, chave: str = "", descricao: str = "") -> LinkResult:
    orfao = db.get(ErpOrfao, orfao_id)
    if orfao is None:
        return LinkResult(error="Este título não está mais na lista de faltantes (já vinculado ou reconciliado).")
    if not data:
        return LinkResult(error="Escolha o PDF.")
    if len(data) > max_bytes:
        return LinkResult(error=f"PDF maior que {max_bytes // (1024 * 1024)} MB.")
    if kind not in ("nfse", "danfe"):
        return LinkResult(error="Tipo inválido.")
    numero = re.sub(r"\D", "", numero or "").lstrip("0")
    if not numero or len(numero) > 20:
        return LinkResult(error="Informe o número da nota (só dígitos).")
    chave = re.sub(r"\D", "", chave or "")
    if chave:
        if len(chave) not in (44, 50):
            return LinkResult(error=f"A chave tem {len(chave)} dígitos (esperado 44 no DANFE ou 50 na NFS-e).")
        if len(chave) == 44 and not nfe_key_is_valid(chave):
            return LinkResult(error="Chave de acesso inválida (dígito verificador não confere).")
    if orfao.data_emissao is None:
        return LinkResult(error="O título do ERP não tem data de emissão; não dá para criar a nota.")
    try:
        reader = open_pdf(data)  # mesmas travas de segurança do envio normal (script, anexo, protegido, páginas)
    except PdfRejected as exc:
        return LinkResult(error=str(exc))

    sha = hashlib.sha256(data).hexdigest()
    emit_doc = cnpj_mask(orfao.cnpj)
    serie = parse_titulo(orfao.titulo)[1]
    dup = db.scalar(select(Note).where(or_(
        Note.file_sha256 == sha, Note.chave == (chave or "-"),
        (Note.kind == kind) & (Note.emit_doc == emit_doc) & (Note.serie == serie) & (Note.numero == numero))))
    if dup:
        motivo = "Este PDF já está no sistema" if dup.file_sha256 == sha else "Já existe esta nota no sistema"
        return LinkResult(error=f"{motivo} ({dup.label} de {dup.emit_nome}).")

    grupo = siblings(db, orfao)
    note = Note(
        kind=kind, chave=chave or f"manual-{sha[:24]}", numero=numero, serie=serie, emit_nome=nome_erp(orfao.fornecedor)[:300],
        emit_doc=emit_doc, tom_nome="", tom_doc="", data_emissao=orfao.data_emissao, vencimento=None,
        descricao=(re.sub(r"[\x00-\x1f\x7f]", " ", descricao or "").strip()[:500] or "PDF escaneado vinculado ao título do ERP"),
        valor_cents=sum(o.valor_cents for o in grupo), desconto_cents=0, original_name=(filename or "escaneado.pdf")[:255],
        file_sha256=sha, file_size=len(data), pages=len(reader.pages), encrypted=vault.enabled, pdf_data=vault.seal(data),
        origem="manual", data_lancamento=next((o.data_lancamento for o in grupo if o.data_lancamento), None),
        conciliado_em=utcnow(), conciliacao_obs="PDF escaneado vinculado ao título do ERP " + ", ".join(o.titulo for o in grupo))
    for o in grupo:
        _, _, parc = parse_titulo(o.titulo)
        note.titulos.append(Titulo(titulo=o.titulo, parcela=parc, vencimento=o.vencimento, data_pagamento=o.data_pagamento,
                                   valor_nominal_cents=o.valor_cents, valor_pago_cents=o.valor_cents if o.data_pagamento else 0,
                                   especie=o.especie or ""))
    recompute(note)
    db.add(note)
    for o in grupo:
        db.delete(o)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return LinkResult(error="Esta nota já está no sistema.")
    return LinkResult(note=note)


FIELDS = ("fornecedor", "titulo", "data_emissao", "vencimento", "data_pagamento", "data_lancamento", "valor_cents", "especie")


def ignore_orphan(db: Session, orfao_id: int, motivo: str) -> list[ErpIgnorado]:
    """Tira o título (e as parcelas da mesma nota) da lista de faltantes e lembra disso nas próximas importações."""
    orfao = db.get(ErpOrfao, orfao_id)
    if orfao is None:
        return []
    motivo = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", motivo or "")).strip()[:200] or "Não é nota da loja"
    out = []
    for o in siblings(db, orfao):
        ig = ErpIgnorado(cnpj=re.sub(r"\D", "", o.cnpj), motivo=motivo, **{f: getattr(o, f) for f in FIELDS})
        db.add(ig)
        db.delete(o)
        out.append(ig)
    db.commit()
    return out


def restore_ignored(db: Session, ignorado_id: int) -> ErpIgnorado | None:
    ig = db.get(ErpIgnorado, ignorado_id)
    if ig is None:
        return None
    db.add(ErpOrfao(cnpj=ig.cnpj, motivo="restaurado pelo admin", **{f: getattr(ig, f) for f in FIELDS}))
    db.delete(ig)
    db.commit()
    return ig
