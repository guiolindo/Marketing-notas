from __future__ import annotations

import logging
import secrets
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
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
    apply_security_headers,
)
from app.web import BASE, render

log = logging.getLogger("notas")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.validate_security()

    app = FastAPI(title="Notas de Marketing", docs_url=None, redoc_url=None, openapi_url=None)
    engine = make_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.SessionLocal = init_db(engine)
    app.state.vault = Vault(settings.document_encryption_key)
    app.state.admin_auth = AdminAuth(settings.admin_username, settings.admin_password)
    if not app.state.admin_auth.enabled:
        log.warning("ADMIN_PASSWORD não definida: o login de administrador está desligado.")

    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key or secrets.token_hex(32),
        session_cookie="nm_session" if settings.debug else "__Host-nm_session",
        max_age=settings.session_hours * 3600, same_site="lax", https_only=not settings.debug)

    @app.middleware("http")
    async def guard(request: Request, call_next):
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
        return response

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

