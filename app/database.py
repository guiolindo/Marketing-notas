from __future__ import annotations

from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Base


def make_engine(settings: Settings) -> Engine:
    url = settings.sqlalchemy_url
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)


# colunas adicionadas depois da primeira versão: create_all não altera tabela que já existe
_NEW_COLUMNS = [
    ("devices", "model", "VARCHAR(40) DEFAULT ''"),
    ("notes", "data_lancamento", "DATE"),
    ("notes", "situacao", "VARCHAR(16) DEFAULT 'sem_lancamento'"),
    ("notes", "nominal_cents", "BIGINT DEFAULT 0"),
    ("notes", "pago_cents", "BIGINT DEFAULT 0"),
    ("notes", "ultimo_pagamento", "DATE"),
    ("notes", "proximo_vencimento", "DATE"),
    ("notes", "conciliado_em", "TIMESTAMP WITH TIME ZONE"),
    ("notes", "conciliacao_obs", "VARCHAR(300) DEFAULT ''"),
]


def _add_missing_columns(engine: Engine) -> None:
    insp = inspect(engine)
    for table, column, ddl in _NEW_COLUMNS:
        if insp.has_table(table) and column not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db(engine: Engine) -> sessionmaker[Session]:
    _add_missing_columns(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def get_db(request: Request):
    db = request.app.state.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sanitize_descriptions(session_factory: sessionmaker[Session]) -> int:
    """Notas lançadas antes do filtro atual podem ter agência/conta/Pix na descrição: limpa no banco."""
    from app.models import Note
    from app.parsing import cut_sensitive
    changed = 0
    with session_factory() as db:
        for nid, desc in db.execute(select(Note.id, Note.descricao)).all():
            clean = cut_sensitive(desc or "")
            if clean != (desc or ""):
                db.get(Note, nid).descricao = clean or "Serviço prestado"
                changed += 1
        if changed:
            db.commit()
    return changed
