from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Note(Base):
    """Uma NFS-e ou DANFE lançado. O PDF fica no banco (o disco do Railway some a cada deploy)."""

    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("chave", name="uq_notes_chave"),
        UniqueConstraint("file_sha256", name="uq_notes_sha256"),
        UniqueConstraint("kind", "emit_doc", "serie", "numero", name="uq_notes_documento"),
        Index("ix_notes_data", "data_emissao"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # nfse | danfe
    chave: Mapped[str] = mapped_column(String(50))
    numero: Mapped[str] = mapped_column(String(20))
    serie: Mapped[str] = mapped_column(String(10), default="")
    emit_nome: Mapped[str] = mapped_column(String(300))
    emit_doc: Mapped[str] = mapped_column(String(20))
    tom_nome: Mapped[str] = mapped_column(String(300), default="")
    tom_doc: Mapped[str] = mapped_column(String(20), default="")
    data_emissao: Mapped[date] = mapped_column(Date)
    vencimento: Mapped[date | None] = mapped_column(Date, nullable=True)
    descricao: Mapped[str] = mapped_column(Text, default="")
    valor_cents: Mapped[int] = mapped_column(BigInteger)
    desconto_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    categoria: Mapped[str] = mapped_column(String(60), default="")
    obs: Mapped[str] = mapped_column(Text, default="")

    original_name: Mapped[str] = mapped_column(String(255), default="")
    file_sha256: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int] = mapped_column(Integer)
    pages: Mapped[int] = mapped_column(Integer, default=1)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    pdf_data = deferred(mapped_column(LargeBinary))  # só é lido quando alguém abre/baixa o PDF

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def label(self) -> str:
        return ("NFS-e" if self.kind == "nfse" else "DANFE") + f" {self.numero}"


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_ip_at", "ip", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(10))  # admin | viewer
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    event: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(400), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class SiteSetting(Base):
    """Configurações editáveis pelo administrador (hoje: o aviso no topo do site)."""

    __tablename__ = "site_settings"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Device(Base):
    """Um aparelho que abriu o site. Sem IP e sem o texto bruto do navegador: só uma identidade anônima (hash de um
    número aleatório guardado no cookie do próprio aparelho) e o tipo/navegador/sistema."""

    __tablename__ = "devices"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    visits: Mapped[int] = mapped_column(Integer, default=1)
    browser: Mapped[str] = mapped_column(String(30), default="")
    os: Mapped[str] = mapped_column(String(30), default="")
    kind: Mapped[str] = mapped_column(String(15), default="")  # Celular | Tablet | Computador
