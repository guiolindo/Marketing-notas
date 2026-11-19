"""Sessão, login do administrador, trava de tentativas, CSRF e cabeçalhos de segurança."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, LoginAttempt, utcnow


class NeedsAdmin(Exception):
    pass


class NeedsViewer(Exception):
    pass


class Forbidden(Exception):
    pass


# ---------------------------------------------------------------- credenciais

class AdminAuth:
    """Guarda só o hash (scrypt) da senha do administrador, calculado na subida a partir da variável de ambiente."""

    def __init__(self, username: str, password: str):
        self.username = username.encode()
        self._salt = secrets.token_bytes(16)
        self.enabled = bool(password)
        self._hash = self._kdf(password) if password else b""

    def _kdf(self, password: str) -> bytes:
        return hashlib.scrypt(password.encode(), salt=self._salt, n=2**14, r=8, p=1, dklen=32)

    def check(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(username.encode()[:200], self.username)
        pass_ok = hmac.compare_digest(self._kdf(password[:200]), self._hash or b"\0" * 32)
        return bool(self.enabled and user_ok and pass_ok)


def check_viewer_password(settings: Settings, given: str) -> bool:
    if not settings.viewer_password:
        return True
    a = hashlib.sha256(settings.viewer_password.encode()).digest()
    b = hashlib.sha256(given[:200].encode()).digest()
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------- IP e trava

def client_ip(request: Request, trusted_proxies: int) -> str:
    """IP real do visitante. Atrás do Railway há 1 proxy: vale o endereço que ele anexou ao X-Forwarded-For."""
    if trusted_proxies > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= trusted_proxies:
            return parts[-trusted_proxies][:64]
    return (request.client.host if request.client else "?")[:64]


def is_locked(db: Session, ip: str, scope: str, settings: Settings) -> bool:
    since = utcnow() - timedelta(minutes=settings.login_window_minutes)
    n = db.scalar(select(func.count()).select_from(LoginAttempt).where(
        LoginAttempt.ip == ip, LoginAttempt.scope == scope, LoginAttempt.at >= since))
    return (n or 0) >= settings.login_max_failures


def record_failure(db: Session, ip: str, scope: str) -> None:
    db.add(LoginAttempt(ip=ip, scope=scope))
    db.execute(delete(LoginAttempt).where(LoginAttempt.at < utcnow() - timedelta(days=2)))
    db.commit()


def clear_failures(db: Session, ip: str, scope: str) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.ip == ip, LoginAttempt.scope == scope))
    db.commit()


def audit(db: Session, request: Request, settings: Settings, event: str, detail: str = "") -> None:
    db.add(AuditLog(event=event, detail=detail[:400], ip=client_ip(request, settings.trusted_proxy_count)))
    db.commit()


# ---------------------------------------------------------------- sessão

def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = secrets.token_urlsafe(32)
    return tok


def _fresh(session: dict, key: str, settings: Settings) -> bool:
    now = time.time()
    at, last = session.get(key), session.get("last")
    if not isinstance(at, (int, float)) or not isinstance(last, (int, float)):
        return False
    return now - at < settings.session_hours * 3600 and now - last < settings.session_idle_minutes * 60


def session_roles(request: Request, settings: Settings) -> tuple[bool, bool]:
    """(é administrador, passou pela senha de visualização). Expira por tempo total e por inatividade."""
    s = request.session
    admin = bool(s.get("admin")) and _fresh(s, "admin_at", settings)
    viewer = admin or (bool(s.get("viewer")) and _fresh(s, "viewer_at", settings))
    if (s.get("admin") or s.get("viewer")) and not (admin or viewer):
        s.clear()
    elif admin or viewer:
        if time.time() - s.get("last", 0) > 60:  # evita reescrever o cookie a cada clique
            s["last"] = int(time.time())
    return admin, viewer


def login_session(request: Request, role: str) -> None:
    """Sessão nova a cada login (evita fixação de sessão) e token CSRF novo."""
    request.session.clear()
    now = int(time.time())
    request.session.update({role: True, f"{role}_at": now, "last": now, "csrf": secrets.token_urlsafe(32)})


def verify_csrf(request: Request, token: str) -> None:
    expected = request.session.get("csrf", "")
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise Forbidden("Sessão expirada. Recarregue a página e tente de novo.")
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        if urlsplit(origin).netloc != host:
            raise Forbidden("Origem da requisição não permitida.")
    elif origin == "null":
        raise Forbidden("Origem da requisição não permitida.")


# ---------------------------------------------------------------- cabeçalhos

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def apply_security_headers(response, request: Request, settings: Settings) -> None:
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if not settings.debug:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.url.path.startswith("/static/"):
        h.setdefault("Cache-Control", "private, no-store")
