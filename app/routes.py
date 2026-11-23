from __future__ import annotations

import math
import re
import time
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditLog, ErpOrfao, Note
from app.security import (
    NeedsAdmin,
    NeedsViewer,
    audit,
    check_viewer_password,
    clear_failures,
    client_ip,
    is_locked,
    login_session,
    record_failure,
    session_roles,
    verify_csrf,
)
from app.services.conciliacao import (
    ImportError_,
    apply_plan,
    build_plan,
    clear_note,
    read_file,
)
from app.services.devices import DeviceTracker, self_status
from app.services.merge import MergeError, merge_key, merge_pdfs
from app.services.notes import (
    Filters,
    brl,
    compute_stats,
    filter_options,
    ingest,
    query_notes,
)
from app.services.sheets import build_system_export, build_xlsx
from app.services.site import MAX_TEXT, PRESETS, build_banner
from app.web import render

router = APIRouter()
PER_PAGE = 25
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------- acesso

def require_viewer(request: Request) -> None:
    settings = request.app.state.settings
    admin, viewer = session_roles(request, settings)
    if settings.viewer_password and not viewer:
        raise NeedsViewer()


def require_admin(request: Request) -> None:
    admin, _ = session_roles(request, request.app.state.settings)
    if not admin:
        raise NeedsAdmin()


def _post_guard(request: Request, token: str) -> None:
    verify_csrf(request, token)


def _flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


def _get_note(db: Session, note_id: int) -> Note:
    note = db.get(Note, note_id)
    if note is None:
        from starlette.exceptions import HTTPException
        raise HTTPException(404)
    return note


# ---------------------------------------------------------------- páginas públicas

def list_view(request: Request, db: Session, admin: bool, results=None):
    f = Filters.from_params(request.query_params)
    stats = compute_stats(db, f)
    try:
        page = max(1, int(request.query_params.get("p", "1")))
    except ValueError:
        page = 1
    pages = max(1, math.ceil(stats.count / PER_PAGE))
    page = min(page, pages)
    notes = db.scalars(query_notes(f).limit(PER_PAGE).offset((page - 1) * PER_PAGE)).all()
    fornecedores, categorias = filter_options(db)
    base = "/admin" if admin else "/"
    return render(request, "list.html", f=f, stats=stats, notes=notes, page=page, pages=pages, base=base,
                  fornecedores=fornecedores, categorias=categorias, results=results, per_page=PER_PAGE,
                  devices=DeviceTracker.summary(db) if admin else None,
                  me=self_status(db, request.cookies.get("nm_dev" if request.app.state.settings.debug else "__Host-nm_dev", ""),
                                 request.headers.get("user-agent", ""), request.headers.get("sec-gpc") == "1") if admin else None, presets=PRESETS, max_banner=MAX_TEXT, max_mb=request.app.state.settings.max_upload_mb,
                  max_files=request.app.state.settings.max_files_per_upload,
                  recent=db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(12)).all() if admin else [],
                  conc=_conciliation_info(db) if admin else None)


def _conciliation_info(db: Session) -> dict:
    last = db.scalar(select(func.max(Note.conciliado_em)))
    return {"last": last, "conciliadas": db.scalar(select(func.count()).select_from(Note).where(Note.situacao != "sem_lancamento")),
            "total": db.scalar(select(func.count()).select_from(Note)),
            "orfaos": db.scalars(select(ErpOrfao).order_by(ErpOrfao.data_emissao.desc().nulls_last())).all()}


@router.get("/")
def home(request: Request, db: Session = Depends(get_db), _=Depends(require_viewer)):
    return list_view(request, db, admin=False)


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    db.execute(select(1))
    return PlainTextResponse("ok")


@router.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@router.get("/entrar")
def viewer_form(request: Request):
    if not request.app.state.settings.viewer_password:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", mode="viewer", action="/entrar", error=None)


@router.post("/entrar")
def viewer_login(request: Request, senha: str = Form("", max_length=200), csrf: str = Form(""),
                 db: Session = Depends(get_db)):
    settings = request.app.state.settings
    _post_guard(request, csrf)
    ip = client_ip(request, settings.trusted_proxy_count)
    if is_locked(db, ip, "viewer", settings):
        return render(request, "login.html", status=429, mode="viewer", action="/entrar",
                      error=f"Muitas tentativas. Aguarde {settings.login_window_minutes} minutos.")
    if not check_viewer_password(settings, senha):
        record_failure(db, ip, "viewer")
        time.sleep(0.6)
        return render(request, "login.html", status=401, mode="viewer", action="/entrar", error="Senha incorreta.")
    clear_failures(db, ip, "viewer")
    login_session(request, "viewer")
    return RedirectResponse("/", status_code=303)


def _pdf_response(db: Session, request: Request, note: Note, disposition: str) -> Response:
    raw = db.scalar(select(Note.pdf_data).where(Note.id == note.id))
    data = request.app.state.vault.open(raw, note.encrypted)
    name = f"{'nfse' if note.kind == 'nfse' else 'danfe'}-{note.numero}-{note.data_emissao:%Y%m%d}.pdf"
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'{disposition}; filename="{name}"',
        "Content-Security-Policy": "frame-ancestors 'none'", "Cache-Control": "private, no-store",
        "Content-Encoding": "identity"})


@router.get("/nota/{note_id}.pdf")
def view_pdf(note_id: int, request: Request, db: Session = Depends(get_db), _=Depends(require_viewer)):
    return _pdf_response(db, request, _get_note(db, note_id), "inline")


@router.get("/nota/{note_id}/baixar")
def download_pdf(note_id: int, request: Request, db: Session = Depends(get_db), _=Depends(require_viewer)):
    return _pdf_response(db, request, _get_note(db, note_id), "attachment")


def _heavy_guard(request: Request) -> None:
    """Rotas caras e públicas: limite por IP, para uma pessoa só não esgotar o servidor."""
    from starlette.exceptions import HTTPException
    s = request.app.state
    if not s.heavy_limiter.allow(client_ip(request, s.settings.trusted_proxy_count)):
        raise HTTPException(429, "Muitos downloads em pouco tempo. Aguarde um minuto.")


@router.get("/baixar-tudo.pdf")
def download_all(request: Request, db: Session = Depends(get_db), _=Depends(require_viewer)):
    from starlette.exceptions import HTTPException
    state = request.app.state
    settings = state.settings
    _heavy_guard(request)
    f = Filters.from_params(request.query_params)
    notes = list(db.scalars(query_notes(f).limit(settings.max_merge_notes + 1)).all())
    if not notes:
        raise HTTPException(404, "Nenhuma nota para baixar com esses filtros.")
    if len(notes) > settings.max_merge_notes:
        raise HTTPException(413, f"Mais de {settings.max_merge_notes} notas. Use os filtros para reduzir o período.")
    pages = sum(n.pages for n in notes)
    if pages > settings.max_merged_pages:  # recusa em vez de entregar um pacote cortado em silêncio
        raise HTTPException(413, f"O PDF teria {pages} páginas (máximo {settings.max_merged_pages}). Use os filtros.")

    key = merge_key(notes)
    data = state.merge_cache.get(key)
    if data is None:
        if not state.merge_slots.acquire(blocking=False):
            raise HTTPException(503, "Já há PDFs sendo gerados. Tente de novo em instantes.")
        try:
            data = merge_pdfs(db, state.vault, notes)
        except MergeError as exc:
            raise HTTPException(500, str(exc)) from exc
        finally:
            state.merge_slots.release()
        state.merge_cache.put(key, data)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="notas-{date.today():%Y-%m-%d}.pdf"',
        "Content-Security-Policy": "frame-ancestors 'none'", "Cache-Control": "private, no-store",
        "Content-Encoding": "identity"})  # PDF já é comprimido: evita gastar CPU com gzip


def _export_notes(request: Request, db: Session) -> list[Note]:
    _heavy_guard(request)
    return list(db.scalars(query_notes(Filters.from_params(request.query_params)).limit(20000)).all())


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/exportar.xlsx")
def export_xlsx(request: Request, db: Session = Depends(get_db), _=Depends(require_viewer)):
    return Response(build_xlsx(_export_notes(request, db)), media_type=XLSX, headers={
        "Content-Disposition": f'attachment; filename="notas-{date.today():%Y-%m-%d}.xlsx"'})


# ---------------------------------------------------------------- administrador

@router.get("/admin/login")
def admin_login_form(request: Request):
    admin, _ = session_roles(request, request.app.state.settings)
    if admin:
        return RedirectResponse("/admin", status_code=303)
    return render(request, "login.html", mode="admin", action="/admin/login", error=None)


@router.post("/admin/login")
def admin_login(request: Request, usuario: str = Form("", max_length=200), senha: str = Form("", max_length=200),
                csrf: str = Form(""), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    _post_guard(request, csrf)
    ip = client_ip(request, settings.trusted_proxy_count)
    if is_locked(db, ip, "admin", settings):
        audit(db, request, settings, "login_bloqueado", f"usuário digitado: {_CTRL.sub('', usuario)[:40]}")
        return render(request, "login.html", status=429, mode="admin", action="/admin/login",
                      error=f"Muitas tentativas. Aguarde {settings.login_window_minutes} minutos e tente de novo.")
    if not request.app.state.admin_auth.check(usuario, senha):
        record_failure(db, ip, "admin")
        audit(db, request, settings, "login_falhou", f"usuário digitado: {_CTRL.sub('', usuario)[:40]}")
        time.sleep(0.6)
        return render(request, "login.html", status=401, mode="admin", action="/admin/login",
                      error="Usuário ou senha incorretos.")
    clear_failures(db, ip, "admin")
    login_session(request, "admin")
    audit(db, request, settings, "login")
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/sair")
def admin_logout(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    _post_guard(request, csrf)
    audit(db, request, request.app.state.settings, "logout")
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/admin")
def admin_home(request: Request, db: Session = Depends(get_db), _=Depends(require_admin)):
    return list_view(request, db, admin=True)


@router.post("/admin/enviar")
def admin_upload(request: Request, files: list[UploadFile] = File(...), csrf: str = Form(""),
                 db: Session = Depends(get_db), _=Depends(require_admin)):
    settings = request.app.state.settings
    _post_guard(request, csrf)
    files = [f for f in files if f.filename]
    if not files:
        _flash(request, "error", "Escolha ao menos um PDF.")
        return RedirectResponse("/admin", status_code=303)
    if len(files) > settings.max_files_per_upload:
        _flash(request, "error", f"Envie no máximo {settings.max_files_per_upload} arquivos por vez.")
        return RedirectResponse("/admin", status_code=303)
    results = []
    for up in files:
        data = up.file.read(settings.max_upload_bytes + 1)
        results.append(ingest(db, request.app.state.vault, up.filename or "nota.pdf", data, settings.max_upload_bytes))
    count = {s: sum(1 for r in results if r.status == s) for s in ("ok", "duplicate", "invalid")}
    audit(db, request, settings, "upload",
          f"{count['ok']} lançada(s), {count['duplicate']} duplicada(s), {count['invalid']} recusada(s)")
    if "application/json" in request.headers.get("accept", ""):  # envio em lotes feito pelo JavaScript da página
        return JSONResponse({"results": [{"filename": r.filename, "status": r.status, "message": r.message}
                                         for r in results]})
    return list_view(request, db, admin=True, results=results)


@router.get("/admin/nota/{note_id}")
def admin_edit_form(note_id: int, request: Request, db: Session = Depends(get_db), _=Depends(require_admin)):
    return render(request, "edit.html", note=_get_note(db, note_id))


@router.post("/admin/nota/{note_id}")
def admin_edit(note_id: int, request: Request, categoria: str = Form("", max_length=60),
               obs: str = Form("", max_length=1000), csrf: str = Form(""), db: Session = Depends(get_db),
               _=Depends(require_admin)):
    _post_guard(request, csrf)
    note = _get_note(db, note_id)
    note.categoria = _CTRL.sub(" ", categoria).strip()
    note.obs = obs.replace("\r", "").strip()
    db.commit()
    audit(db, request, request.app.state.settings, "editou", note.label)
    _flash(request, "ok", f"{note.label} atualizada.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/nota/{note_id}/excluir")
def admin_delete(note_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db),
                 _=Depends(require_admin)):
    _post_guard(request, csrf)
    note = _get_note(db, note_id)
    label = f"{note.label} · {note.emit_nome[:40]} · {brl(note.valor_cents)}"
    db.delete(note)
    db.commit()
    audit(db, request, request.app.state.settings, "excluiu", label)
    _flash(request, "ok", f"{label} excluída.")
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/aviso")
def admin_banner(request: Request, preset: str = Form("", max_length=20), texto: str = Form("", max_length=1000),
                 tipo: str = Form("info", max_length=10), csrf: str = Form(""), db: Session = Depends(get_db),
                 _=Depends(require_admin)):
    _post_guard(request, csrf)
    banner, error = build_banner(preset, texto, tipo)
    if error:
        _flash(request, "error", error)
        return RedirectResponse("/admin", status_code=303)
    request.app.state.banner.set(banner)
    audit(db, request, request.app.state.settings, "aviso", banner.text[:80] if banner else "desligado")
    _flash(request, "ok", "Aviso do site atualizado." if banner else "Aviso do site desligado.")
    return RedirectResponse("/admin", status_code=303)


# ---------------------------------------------------------------- conciliação com o ERP (admin)

@router.get("/admin/exportar-dados.xlsx")
def admin_export_data(request: Request, db: Session = Depends(get_db), _=Depends(require_admin)):
    notes = list(db.scalars(select(Note).order_by(Note.data_emissao, Note.id)).all())
    audit(db, request, request.app.state.settings, "exportou_dados", f"{len(notes)} notas")
    return Response(build_system_export(notes), media_type=XLSX, headers={
        "Content-Disposition": f'attachment; filename="dados-sistema-notas-{date.today():%Y-%m-%d}.xlsx"'})


@router.post("/admin/conciliacao")
def admin_conciliation_preview(request: Request, arquivo: UploadFile = File(...), csrf: str = Form(""),
                               db: Session = Depends(get_db), _=Depends(require_admin)):
    from app.services.conciliacao import MAX_BYTES
    _post_guard(request, csrf)
    data = arquivo.file.read(MAX_BYTES + 1)
    try:
        rows, orphans = read_file(data, arquivo.filename or "")
    except ImportError_ as exc:
        _flash(request, "error", str(exc))
        return RedirectResponse("/admin#conciliacao", status_code=303)
    if not rows:
        _flash(request, "error", "O arquivo não tem linhas de conciliação.")
        return RedirectResponse("/admin#conciliacao", status_code=303)
    plan = build_plan(db, rows, orphans, filename=(arquivo.filename or "")[:120])
    token = request.app.state.pending_imports.put(plan)
    return render(request, "conciliacao.html", plan=plan, token=token)


@router.post("/admin/conciliacao/aplicar")
def admin_conciliation_apply(request: Request, token: str = Form("", max_length=100), csrf: str = Form(""),
                             db: Session = Depends(get_db), _=Depends(require_admin)):
    _post_guard(request, csrf)
    plan = request.app.state.pending_imports.pop(token)
    if plan is None:
        _flash(request, "error", "A conferência expirou (30 min). Importe o arquivo de novo.")
        return RedirectResponse("/admin#conciliacao", status_code=303)
    if plan.errors:
        _flash(request, "error", "O arquivo tem erros. Corrija e importe de novo.")
        return RedirectResponse("/admin#conciliacao", status_code=303)
    n = apply_plan(db, plan)
    request.app.state.merge_cache = type(request.app.state.merge_cache)(request.app.state.merge_cache.max_bytes)
    orf = f", {len(plan.orphans)} título(s) do ERP sem PDF" if plan.orphans is not None else ""
    audit(db, request, request.app.state.settings, "conciliacao", f"{n} nota(s) atualizada(s){orf} · {plan.filename}")
    _flash(request, "ok", f"Conciliação aplicada: {n} nota(s) atualizada(s){orf}.")
    return RedirectResponse("/admin#conciliacao", status_code=303)


@router.post("/admin/nota/{note_id}/limpar-conciliacao")
def admin_clear_conciliation(note_id: int, request: Request, csrf: str = Form(""), db: Session = Depends(get_db),
                             _=Depends(require_admin)):
    _post_guard(request, csrf)
    note = _get_note(db, note_id)
    clear_note(db, note)
    audit(db, request, request.app.state.settings, "limpou_conciliacao", note.label)
    _flash(request, "ok", f"Lançamento e pagamentos de {note.label} removidos.")
    return RedirectResponse(f"/admin/nota/{note_id}", status_code=303)
