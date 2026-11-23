from __future__ import annotations

import logging
import secrets
import threading
import uuid

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, get_settings
from app.crypto import Vault
from app.database import init_db, make_engine
from app.routes import router
from app.security import (
    AdminAuth,
    Forbidden,
    NeedsAdmin,
    NeedsViewer,
    RateLimiter,
    apply_security_headers,
)
from app.services.conciliacao import PendingImports
from app.services.devices import COOKIE_RE, DeviceTracker, new_visitor_id
from app.services.merge import MergeCache
from app.services.site import BannerStore
from app.web import BASE, render

log = logging.getLogger("notas")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_security()

    app = FastAPI(title="Notas do Marketing de Porto Seguro", docs_url=None, redoc_url=None, openapi_url=None)
    engine = make_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.SessionLocal = init_db(engine)
    app.state.pending_imports = PendingImports()
    app.state.devices = DeviceTracker(app.state.SessionLocal)
    app.state.banner = BannerStore(app.state.SessionLocal)
    app.state.vault = Vault(settings.document_encryption_key)
    app.state.merge_cache = MergeCache(settings.merge_cache_mb * 1024 * 1024)
    app.state.merge_slots = threading.BoundedSemaphore(settings.merge_concurrency)
    app.state.heavy_limiter = RateLimiter(settings.merge_rate_per_minute)
    app.state.admin_auth = AdminAuth(settings.admin_username, settings.admin_password)
    if not app.state.admin_auth.enabled:
        log.warning("ADMIN_PASSWORD não definida: o login de administrador está desligado.")

    app.add_middleware(GZipMiddleware, minimum_size=1024)  # HTML/CSS/JS/CSV; PDFs saem com Content-Encoding: identity
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key or secrets.token_hex(32),
        session_cookie="nm_session" if settings.debug else "__Host-nm_session",
        max_age=settings.session_hours * 3600, same_site="lax", https_only=not settings.debug)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Atrás do Railway o TLS termina no proxy; quem abrir por http:// é mandado para https:// (o navegador marca
        # "não seguro" em http e o cookie de sessão, que é Secure, nem chega a ser gravado).
        if (not settings.debug and settings.trusted_proxy_count > 0 and request.url.path != "/health"
                and request.headers.get("x-forwarded-proto", "https").split(",")[0].strip() == "http"):
            host = request.headers.get("host", "")
            query = ("?" + request.url.query) if request.url.query else ""
            return RedirectResponse(f"https://{host}{request.url.path}{query}", status_code=308)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                size = 0
            limit = settings.max_upload_bytes * settings.max_files_per_upload + 2 * 1024 * 1024
            if size > limit:
                return PlainTextResponse("Envio grande demais.", status_code=413)
        response = await call_next(request)
        apply_security_headers(response, request, settings)
        if "text/html" in response.headers.get("content-type", ""):
            response.headers["Accept-CH"] = "Sec-CH-UA-Model"  # pede ao Chrome/Edge o modelo do aparelho (Android)
        await _count_device(request, response)
        return response

    async def _count_device(request: Request, response) -> None:
        """Conta o aparelho (anônimo, sem IP) quando ele abre uma página. Respeita o sinal Sec-GPC (o antigo DNT foi descontinuado)."""
        path = request.url.path
        if (request.method != "GET" or response.status_code != 200 or path.startswith(("/static/", "/nota/"))
                or path in ("/health", "/robots.txt", "/baixar-tudo.pdf", "/exportar.csv")
                or "text/html" not in response.headers.get("content-type", "")
                or request.headers.get("sec-gpc") == "1"):
            return
        name = "nm_dev" if settings.debug else "__Host-nm_dev"
        vid = request.cookies.get(name, "")
        if not COOKIE_RE.match(vid):
            vid = new_visitor_id()
            response.set_cookie(name, vid, max_age=365 * 24 * 3600, httponly=True, secure=not settings.debug,
                                samesite="lax", path="/")
        try:
            await run_in_threadpool(app.state.devices.touch, vid, request.headers.get("user-agent", ""),
                                    request.headers.get("sec-ch-ua-model", ""))
        except Exception:  # noqa: BLE001 - a contagem nunca pode derrubar uma página
            log.warning("falha ao contar dispositivo", exc_info=True)

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(NeedsAdmin)
    async def _needs_admin(request: Request, exc: NeedsAdmin):
        return RedirectResponse("/admin/login", status_code=303)

    @app.exception_handler(NeedsViewer)
    async def _needs_viewer(request: Request, exc: NeedsViewer):
        return RedirectResponse("/entrar", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return render(request, "error.html", status=403, code=403, message=str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        generic = {404: "Esta página não existe ou foi removida.", 405: "Ação indisponível por aqui."}
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail not in ("Not Found", "Method Not Allowed") else ""
        if "text/html" in request.headers.get("accept", ""):
            return render(request, "error.html", status=exc.status_code, code=exc.status_code,
                          message=detail or generic.get(exc.status_code, "Algo deu errado."))
        return PlainTextResponse(detail or generic.get(exc.status_code, "Erro."), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        rid = uuid.uuid4().hex[:8]
        log.exception("Erro não tratado [%s] %s %s", rid, request.method, request.url.path)
        return render(request, "error.html", status=500, code=500, rid=rid,
                      message="Erro inesperado. Tente de novo; se persistir, avise o administrador.")

    return app

