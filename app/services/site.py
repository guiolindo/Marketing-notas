"""Aviso no topo do site: o administrador escolhe uma mensagem pronta ou escreve a sua."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.models import SiteSetting

KINDS = ("ok", "warn", "info")
MAX_TEXT = 200
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

# chave -> (rótulo no painel, tipo/cor, texto mostrado)
PRESETS: dict[str, tuple[str, str, str]] = {
    "procurando": ("Procurando as notas", "warn", "Estamos procurando as notas e colocando para download."),
    "disponivel": ("Todas disponíveis", "ok", "Todas as notas estão disponíveis."),
    "atualizando": ("Novas notas a caminho", "info", "Novas notas estão sendo adicionadas. Volte em breve."),
}


@dataclass(frozen=True)
class Banner:
    text: str
    kind: str  # ok | warn | info
    preset: str  # chave de PRESETS ou "custom"


class BannerStore:
    """Lê/grava o aviso no banco, com cache curto para não consultar o banco a cada página."""

    TTL = 15.0

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sf = session_factory
        self._lock = threading.Lock()
        self._cache: tuple[float, Banner | None] | None = None

    def get(self) -> Banner | None:
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < self.TTL:
                return self._cache[1]
        try:
            banner = self._load()
        except Exception:  # noqa: BLE001 - o aviso é enfeite: nunca pode derrubar uma página (nem a de erro)
            return self._cache[1] if self._cache else None
        with self._lock:
            self._cache = (time.monotonic(), banner)
        return banner

    def _load(self) -> Banner | None:
        with self._sf() as db:
            rows = {r.key: r.value for r in db.query(SiteSetting).filter(SiteSetting.key.like("banner_%"))}
        if rows.get("banner_on") != "1" or not rows.get("banner_text"):
            return None
        kind = rows.get("banner_kind", "info")
        return Banner(rows["banner_text"], kind if kind in KINDS else "info", rows.get("banner_preset", "custom"))

    def set(self, banner: Banner | None) -> None:
        values = {"banner_on": "0"} if banner is None else {
            "banner_on": "1", "banner_text": banner.text, "banner_kind": banner.kind, "banner_preset": banner.preset}
        with self._sf() as db:
            for k, v in values.items():
                row = db.get(SiteSetting, k)
                if row:
                    row.value = v
                else:
                    db.add(SiteSetting(key=k, value=v))
            db.commit()
        with self._lock:
            self._cache = (time.monotonic(), banner)


def build_banner(preset: str, text: str, kind: str) -> tuple[Banner | None, str | None]:
    """Valida o formulário do painel. Retorna (aviso, erro). Aviso None = desligar."""
    if preset == "off":
        return None, None
    if preset in PRESETS:
        _, k, t = PRESETS[preset]
        return Banner(t, k, preset), None
    if preset == "custom":
        clean = _CTRL.sub(" ", text or "").strip()
        clean = re.sub(r"\s+", " ", clean)
        if not clean:
            return None, "Escreva a mensagem do aviso."
        if len(clean) > MAX_TEXT:
            return None, f"A mensagem tem {len(clean)} caracteres (máximo {MAX_TEXT})."
        return Banner(clean, kind if kind in KINDS else "info", "custom"), None
    return None, "Opção de aviso inválida."
