"""Contagem anônima de dispositivos. Não guarda IP nem o User-Agent: só navegador, sistema e tipo de aparelho."""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Device, utcnow

TZ = ZoneInfo("America/Bahia")
COOKIE_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_BOT = re.compile(r"bot|crawl|spider|slurp|curl|wget|python|httpx|requests|monitor|uptime|headless|preview|lighthouse|"
                  r"go-http|scrapy|facebookexternalhit|pingdom|statuscake|railway", re.I)


def _aware(dt: datetime) -> datetime:
    """SQLite devolve datas sem fuso; no banco elas são sempre UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_MODEL_OK = re.compile(r"^[\w .+/()-]{2,40}$")


def clean_model(raw: str) -> str:
    """Modelo informado pelo navegador (cabeçalho Sec-CH-UA-Model, só Chrome/Edge no Android). Vazio se não for útil."""
    m = (raw or "").strip().strip('"').strip()
    if not _MODEL_OK.match(m) or m.lower() in {"k", "generic android", "android", "unknown", "undefined"}:
        return ""
    if re.match(r"^(SM|GT|SC|SCV|SGH)-", m, re.I):
        return f"Samsung {m.upper()}"
    return m


def new_visitor_id() -> str:
    return secrets.token_urlsafe(32)


def hash_id(visitor_id: str) -> str:
    return hashlib.sha256(visitor_id.encode()).hexdigest()


def parse_ua(ua: str) -> tuple[str, str, str] | None:
    """(navegador, sistema, tipo) a partir do User-Agent; None para robô/ferramenta. O texto original não é guardado."""
    if not ua or _BOT.search(ua):
        return None
    if re.search(r"EdgA?/|EdgiOS/|Edge/", ua):
        browser = "Edge"
    elif re.search(r"OPR/|Opera|OPiOS", ua):
        browser = "Opera"
    elif "SamsungBrowser" in ua:
        browser = "Samsung Internet"
    elif re.search(r"Firefox/|FxiOS/", ua):
        browser = "Firefox"
    elif "Instagram" in ua:
        browser = "Instagram (app)"
    elif re.search(r"FBAN|FBAV", ua):
        browser = "Facebook (app)"
    elif "WhatsApp" in ua:
        browser = "WhatsApp (app)"
    elif re.search(r"CriOS/|Chrome/", ua):
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Outro"

    if re.search(r"iPhone|iPad|iPod", ua):
        os_ = "iOS"
    elif "Android" in ua:
        os_ = "Android"
    elif "Windows" in ua:
        os_ = "Windows"
    elif re.search(r"Macintosh|Mac OS X", ua):
        os_ = "macOS"
    elif "CrOS" in ua:
        os_ = "ChromeOS"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = "Outro"

    if "iPad" in ua or "Tablet" in ua or (os_ == "Android" and "Mobile" not in ua):
        kind = "Tablet"
    elif re.search(r"iPhone|iPod|Mobile", ua):
        kind = "Celular"
    else:
        kind = "Computador"
    return browser, os_, kind


@dataclass
class DeviceSummary:
    total: int = 0
    today: int = 0
    week: int = 0
    kinds: list[tuple[str, int]] = field(default_factory=list)
    browsers: list[tuple[str, int]] = field(default_factory=list)
    systems: list[tuple[str, int]] = field(default_factory=list)
    models: list[tuple[str, int]] = field(default_factory=list)
    recent: list[dict] = field(default_factory=list)


class DeviceTracker:
    WRITE_EVERY = 600.0  # no máximo 1 gravação a cada 10 min por aparelho

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sf = session_factory
        self._last: dict[str, float] = {}
        self._with_model: set[str] = set()
        self._lock = threading.Lock()

    def touch(self, visitor_id: str, ua: str, model: str = "") -> None:
        parsed = parse_ua(ua)
        if parsed is None or not COOKIE_RE.match(visitor_id):
            return
        model = clean_model(model)
        key, now = hash_id(visitor_id), time.monotonic()
        with self._lock:
            fresh_model = bool(model) and key not in self._with_model  # o modelo chega no 2º acesso: não esperar 10 min
            if now - self._last.get(key, -1e9) < self.WRITE_EVERY and not fresh_model:
                return
            self._last[key] = now
            if model:
                self._with_model.add(key)
            if len(self._with_model) > 20000:
                self._with_model.clear()
            if len(self._last) > 20000:
                self._last = {k: v for k, v in self._last.items() if now - v < self.WRITE_EVERY}
        browser, os_, kind = parsed
        with self._sf() as db:
            row = db.get(Device, key)
            if row is None:
                db.add(Device(id_hash=key, browser=browser, os=os_, kind=kind, model=model))
            else:
                row.last_seen, row.visits = utcnow(), row.visits + 1
                row.browser, row.os, row.kind = browser, os_, kind
                row.model = model or row.model or ""
            db.commit()

    @staticmethod
    def summary(db: Session) -> DeviceSummary:
        rows = db.execute(select(Device.first_seen, Device.last_seen, Device.visits, Device.browser, Device.os,
                                 Device.kind, Device.model).order_by(Device.last_seen.desc())).all()
        now = utcnow()
        today = now.astimezone(TZ).date()
        s = DeviceSummary(total=len(rows))
        for first, last, visits, browser, os_, kind, _model in rows:
            first, last = _aware(first).astimezone(TZ), _aware(last).astimezone(TZ)
            s.today += last.date() == today
            s.week += last >= (now - timedelta(days=7)).astimezone(TZ)
        s.kinds = Counter(r[5] for r in rows).most_common()
        s.browsers = Counter(r[3] for r in rows).most_common(6)
        s.systems = Counter(r[4] for r in rows).most_common(6)
        s.models = Counter(r[6] for r in rows if r[6]).most_common(6)
        s.recent = [{"browser": r[3], "os": r[4], "kind": r[5], "model": r[6] or "", "visits": r[2],
                     "first": _aware(r[0]).astimezone(TZ).strftime("%d/%m %H:%M"),
                     "last": _aware(r[1]).astimezone(TZ).strftime("%d/%m %H:%M")}
                    for r in rows[:15]]
        return s


def self_status(db: Session, cookie_value: str, ua: str, gpc: bool) -> tuple[bool, str]:
    """Para o administrador: este aparelho foi contado? Se não, por quê. (ok, mensagem)"""
    parsed = parse_ua(ua)
    if gpc:
        return False, "Não contado: o navegador está com o sinal de privacidade (Sec-GPC) ligado."
    if parsed is None:
        return False, "Não contado: o navegador foi reconhecido como robô/ferramenta."
    if not COOKIE_RE.match(cookie_value or ""):
        return False, "Ainda não contado: o navegador não guardou o cookie. Recarregue a página."
    row = db.get(Device, hash_id(cookie_value))
    if row is None:
        return False, "Ainda não contado. Recarregue a página em alguns segundos."
    extra = f" · {row.model}" if row.model else ""
    return True, f"Contado: {row.browser} · {row.os}{extra} ({row.kind})."
