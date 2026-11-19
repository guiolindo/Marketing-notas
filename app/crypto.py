from __future__ import annotations

from cryptography.fernet import Fernet


class Vault:
    """Criptografia opcional dos PDFs no banco. Sem chave configurada, grava o PDF como veio."""

    def __init__(self, key: str = ""):
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def seal(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data) if self._fernet else data

    def open(self, data: bytes, encrypted: bool) -> bytes:
        if not encrypted:
            return data
        if not self._fernet:
            raise RuntimeError("PDF criptografado, mas DOCUMENT_ENCRYPTION_KEY não está configurada.")
        return self._fernet.decrypt(data)
