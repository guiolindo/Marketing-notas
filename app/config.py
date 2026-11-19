"""Configuração por variáveis de ambiente. Em produção (DEBUG=false) o servidor se recusa a subir com segredo fraco."""
from __future__ import annotations

import base64
import secrets
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_COMMON = {"123456789012", "senha1234567", "password1234", "admin1234567", "123456123456", "qwertyuiop12"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    debug: bool = False
    secret_key: str = ""
    database_url: str = "sqlite:///./data/notas.db"

    admin_username: str = "admin"
    admin_password: str = ""
    viewer_password: str = ""  # opcional: se definida, quem só quer ver as notas também precisa dela
    document_encryption_key: str = ""  # opcional: criptografa os PDFs no banco (Fernet)

    max_upload_mb: int = Field(10, ge=1, le=50)
    max_files_per_upload: int = Field(20, ge=1, le=100)
    max_merge_notes: int = Field(1000, ge=1)
    trusted_proxy_count: int = Field(1, ge=0, le=5)

    session_hours: int = Field(8, ge=1, le=72)
    session_idle_minutes: int = Field(60, ge=5, le=1440)
    login_max_failures: int = Field(5, ge=3, le=50)
    login_window_minutes: int = Field(15, ge=1, le=240)

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url.strip()
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def validate_security(self) -> None:
        problems: list[str] = []
        if not self.debug:
            if len(self.secret_key) < 32:
                problems.append("SECRET_KEY ausente ou curta (mínimo 32 caracteres)")
            pw = self.admin_password
            if len(pw) < 12 or pw.lower() in _COMMON or not (any(c.isalpha() for c in pw) and any(not c.isalpha() for c in pw)):
                problems.append("ADMIN_PASSWORD ausente ou fraca (mínimo 12 caracteres, letras e números/símbolos)")
            if self.viewer_password and len(self.viewer_password) < 8:
                problems.append("VIEWER_PASSWORD muito curta (mínimo 8 caracteres)")
        if self.document_encryption_key:
            try:
                if len(base64.urlsafe_b64decode(self.document_encryption_key.encode())) != 32:
                    raise ValueError
            except Exception:
                problems.append("DOCUMENT_ENCRYPTION_KEY inválida (32 bytes em base64 url-safe)")
        if problems:
            raise RuntimeError("Configuração insegura: " + "; ".join(problems) + ".")

    def effective_secret(self) -> str:
        return self.secret_key or secrets.token_hex(32)  # só em DEBUG: sessões caem ao reiniciar


@lru_cache
def get_settings() -> Settings:
    return Settings()
