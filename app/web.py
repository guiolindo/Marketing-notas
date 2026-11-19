"""Templates, filtros de formatação e utilitários de resposta."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.security import csrf_token, session_roles
from app.services.notes import brl

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["brl"] = brl
templates.env.filters["dmy"] = lambda d: d.strftime("%d/%m/%Y") if d else "—"
templates.env.filters["doc"] = lambda s: s or "—"


def _asset_version() -> str:
    """Versão = hash do CONTEÚDO dos arquivos estáticos (mtime muda a cada deploy mesmo sem mudança real)."""
    h = hashlib.sha1()
    for p in sorted((BASE / "static").rglob("*")):
        if p.is_file() and p.suffix in (".css", ".js", ".svg"):
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


ASSET_V = _asset_version()


def build_url(path: str, params: dict[str, str] | None = None, **extra) -> str:
    q = {**(params or {}), **{k: v for k, v in extra.items() if v not in (None, "")}}
    for k, v in extra.items():
        if v in (None, ""):
            q.pop(k, None)
    return path + ("?" + urlencode(q) if q else "")


def render(request: Request, name: str, status: int = 200, **ctx):
    settings = request.app.state.settings
    admin, viewer = session_roles(request, settings)
    flash = request.session.pop("flash", None) if request.session else None
    ctx.update(request=request, is_admin=admin, is_viewer=viewer, csrf=lambda: csrf_token(request), flash=flash,
               asset_v=ASSET_V, build_url=build_url, banner=request.app.state.banner.get(), gate=bool(settings.viewer_password))
    return templates.TemplateResponse(request, name, ctx, status_code=status)
