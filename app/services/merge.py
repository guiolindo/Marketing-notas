"""PDF único com todas as notas: teto de páginas, falha explícita, cache e remoção de objetos repetidos."""
from __future__ import annotations

import hashlib
import io
import logging
import threading
from collections import OrderedDict

from pypdf import PdfReader, PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import Vault
from app.models import Note
from app.services.notes import brl

log = logging.getLogger("notas.merge")


class MergeError(Exception):
    """Alguma nota não pôde entrar no PDF. Melhor avisar do que entregar um pacote incompleto."""


def ordered(notes: list[Note]) -> list[Note]:
    return sorted(notes, key=lambda n: (n.data_emissao, n.emit_nome.lower(), n.numero.zfill(12), n.id))


def merge_key(notes: list[Note]) -> str:
    """Muda se entrar/sair nota ou se um PDF for trocado. Editar categoria/observação não invalida o cache."""
    h = hashlib.sha256()
    for n in ordered(notes):
        h.update(f"{n.id}:{n.file_sha256};".encode())
    return h.hexdigest()


class MergeCache:
    """LRU em memória limitado por tamanho: o mesmo pacote pedido várias vezes é gerado uma vez só."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._d: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: str) -> bytes | None:
        with self._lock:
            data = self._d.get(key)
            if data is None:
                self.misses += 1
                return None
            self._d.move_to_end(key)
            self.hits += 1
            return data

    def put(self, key: str, data: bytes) -> None:
        if len(data) > self.max_bytes:
            return
        with self._lock:
            if key in self._d:
                self._size -= len(self._d.pop(key))
            self._d[key] = data
            self._size += len(data)
            while self._size > self.max_bytes:
                _, old = self._d.popitem(last=False)
                self._size -= len(old)


def _dedupe(writer: PdfWriter) -> None:
    """Junta fontes/imagens idênticas repetidas entre notas. É só otimização: se falhar, segue sem ela."""
    for kwargs in ({"remove_duplicates": True, "remove_unreferenced": True},
                   {"remove_identicals": True, "remove_orphans": True}):  # nome mudou entre versões do pypdf
        try:
            writer.compress_identical_objects(**kwargs)
            return
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("não foi possível otimizar o PDF único: %s", exc)
            return


def merge_pdfs(db: Session, vault: Vault, notes: list[Note]) -> bytes:
    writer = PdfWriter()
    failed: list[str] = []
    for n in ordered(notes):
        try:
            raw = vault.open(db.scalar(select(Note.pdf_data).where(Note.id == n.id)), n.encrypted)
            reader = PdfReader(io.BytesIO(raw), strict=False)
            first = len(writer.pages)
            for page in reader.pages:
                if "/Annots" in page:  # links/ações do PDF original não vão para o arquivo final
                    del page["/Annots"]
                writer.add_page(page)
            writer.add_outline_item(title=f"{n.label} · {n.emit_nome[:40]} · {brl(n.valor_cents)}", page_number=first)
        except Exception as exc:  # noqa: BLE001 - qualquer falha numa nota vira aviso explícito
            log.warning("falha ao juntar %s (id=%s): %s", n.label, n.id, exc)
            failed.append(n.label)
    if failed:
        raise MergeError("Não foi possível juntar: " + ", ".join(failed[:5]) + ("…" if len(failed) > 5 else "") + ".")
    _dedupe(writer)
    writer.add_metadata({"/Title": "Notas fiscais", "/Producer": "Notas do Marketing de Porto Seguro"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
