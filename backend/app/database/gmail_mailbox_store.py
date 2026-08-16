from __future__ import annotations

import json
import re
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import GmailMailbox

_ENVELOPE_MARKER_FIELD = "__support_credentials__"
_ENVELOPE_MARKER = "gmail_oauth_credentials"
_ENVELOPE_VERSION_FIELD = "version"
_ENVELOPE_VERSION = 1
_CIPHERTEXT_FIELD = "ciphertext"
_ENVELOPE_FIELDS = {
    _ENVELOPE_MARKER_FIELD,
    _ENVELOPE_VERSION_FIELD,
    _CIPHERTEXT_FIELD,
}
_HISTORY_ID_PATTERN = re.compile(r"[0-9]+")
_MAX_HISTORY_ID_LENGTH = 64


class GmailCredentialEncryptionError(RuntimeError):
    """Raised when persisted Gmail credentials cannot be protected or opened."""


class GmailMailboxStore:
    """Persist encrypted Gmail credentials and monotonic History API cursors."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        encryption_key: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self._fernet = self._build_fernet(encryption_key)

    def get(self, email_address: str) -> str | None:
        with self.session_factory() as session:
            mailbox = self._find(session, email_address)
            return mailbox.history_id if mailbox else None

    def set(self, email_address: str, history_id: str) -> None:
        normalized_history_id = self._normalize_history_id(history_id)
        with self.session_factory.begin() as session:
            statement = self._insert_for(
                session,
                email_address=email_address,
                credentials=None,
                history_id=normalized_history_id,
            )
            current_history_id = GmailMailbox.__table__.c.history_id
            candidate_history_id = statement.excluded.history_id
            current_canonical = func.coalesce(
                func.nullif(func.ltrim(current_history_id, "0"), ""),
                "0",
            )
            candidate_is_newer = or_(
                current_history_id.is_(None),
                func.length(current_canonical) < func.length(candidate_history_id),
                and_(
                    func.length(current_canonical) == func.length(candidate_history_id),
                    current_canonical < candidate_history_id,
                ),
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[GmailMailbox.email_address],
                    set_={
                        "history_id": candidate_history_id,
                        "updated_at": func.now(),
                    },
                    where=candidate_is_newer,
                )
            )

    def get_credentials(self, email_address: str) -> dict[str, Any] | None:
        with self.session_factory.begin() as session:
            mailbox = self._find(session, email_address, for_update=True)
            if mailbox is None or mailbox.credentials is None:
                return None

            stored = mailbox.credentials
            if not isinstance(stored, dict):
                raise GmailCredentialEncryptionError(
                    "Stored Gmail credentials have an invalid format"
                )
            if self._looks_like_envelope(stored):
                return self._decrypt_credentials(email_address, stored)

            # Rows written before credential encryption was introduced are
            # migrated in the same transaction as their first protected read.
            credentials = dict(stored)
            mailbox.credentials = self._encrypt_credentials(
                email_address,
                credentials,
            )
            return dict(credentials)

    def delete(self, email_address: str) -> bool:
        """Delete one mailbox so credentials can be safely reauthorized."""
        with self.session_factory.begin() as session:
            result = session.execute(
                delete(GmailMailbox).where(
                    GmailMailbox.email_address == email_address,
                )
            )
            return bool(result.rowcount)

    def set_credentials(self, email_address: str, credentials: dict[str, Any]) -> None:
        envelope = self._encrypt_credentials(email_address, credentials)
        with self.session_factory.begin() as session:
            statement = self._insert_for(
                session,
                email_address=email_address,
                credentials=envelope,
                history_id=None,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[GmailMailbox.email_address],
                    set_={
                        "credentials": statement.excluded.credentials,
                        "updated_at": func.now(),
                    },
                )
            )

    def _encrypt_credentials(
        self,
        email_address: str,
        credentials: dict[str, Any],
    ) -> dict[str, Any]:
        fernet = self._require_fernet()
        try:
            plaintext = json.dumps(
                {
                    "email_address": email_address,
                    "credentials": credentials,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise GmailCredentialEncryptionError(
                "Gmail credentials must be JSON serializable"
            ) from exc

        return {
            _ENVELOPE_MARKER_FIELD: _ENVELOPE_MARKER,
            _ENVELOPE_VERSION_FIELD: _ENVELOPE_VERSION,
            _CIPHERTEXT_FIELD: fernet.encrypt(plaintext).decode("ascii"),
        }

    def _decrypt_credentials(
        self,
        email_address: str,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        if set(envelope) != _ENVELOPE_FIELDS:
            raise GmailCredentialEncryptionError(
                "Stored Gmail credential envelope is malformed"
            )
        if envelope.get(_ENVELOPE_MARKER_FIELD) != _ENVELOPE_MARKER:
            raise GmailCredentialEncryptionError(
                "Stored Gmail credential envelope has an unknown type"
            )
        if envelope.get(_ENVELOPE_VERSION_FIELD) != _ENVELOPE_VERSION:
            raise GmailCredentialEncryptionError(
                "Stored Gmail credential envelope has an unsupported version"
            )

        ciphertext = envelope.get(_CIPHERTEXT_FIELD)
        if not isinstance(ciphertext, str) or not ciphertext:
            raise GmailCredentialEncryptionError(
                "Stored Gmail credential ciphertext is malformed"
            )

        try:
            plaintext = self._require_fernet().decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise GmailCredentialEncryptionError(
                "Unable to decrypt Gmail credentials; verify the encryption key "
                "and stored ciphertext"
            ) from exc

        try:
            payload = json.loads(plaintext)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GmailCredentialEncryptionError(
                "Decrypted Gmail credential payload is malformed"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "email_address",
            "credentials",
        }:
            raise GmailCredentialEncryptionError(
                "Decrypted Gmail credential payload has an invalid format"
            )
        if payload["email_address"] != email_address:
            raise GmailCredentialEncryptionError(
                "Stored Gmail credentials are bound to a different mailbox"
            )
        credentials = payload["credentials"]
        if not isinstance(credentials, dict):
            raise GmailCredentialEncryptionError(
                "Decrypted Gmail credentials have an invalid format"
            )
        return dict(credentials)

    def _require_fernet(self) -> Fernet:
        if self._fernet is None:
            raise GmailCredentialEncryptionError(
                "GMAIL_CREDENTIALS_ENCRYPTION_KEY is required to access Gmail "
                "credentials"
            )
        return self._fernet

    @staticmethod
    def _build_fernet(encryption_key: str | None) -> Fernet | None:
        if encryption_key is None or not encryption_key.strip():
            return None
        try:
            return Fernet(encryption_key.strip().encode("ascii"))
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError(
                "GMAIL_CREDENTIALS_ENCRYPTION_KEY must be a URL-safe base64-encoded "
                "32-byte Fernet key"
            ) from exc

    @staticmethod
    def _looks_like_envelope(stored: dict[str, Any]) -> bool:
        # Any reserved envelope field means this row must be validated as an
        # envelope. A damaged envelope must never fall back to the legacy
        # plaintext migration path.
        return bool(_ENVELOPE_FIELDS.intersection(stored))

    @staticmethod
    def _normalize_history_id(history_id: str) -> str:
        if not isinstance(history_id, str) or _HISTORY_ID_PATTERN.fullmatch(history_id) is None:
            raise ValueError("Gmail history_id must contain only ASCII digits")
        normalized = history_id.lstrip("0") or "0"
        if len(normalized) > _MAX_HISTORY_ID_LENGTH:
            raise ValueError(
                f"Gmail history_id must not exceed {_MAX_HISTORY_ID_LENGTH} digits"
            )
        return normalized

    @staticmethod
    def _insert_for(session: Session, **values: Any) -> Any:
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            return postgresql_insert(GmailMailbox).values(**values)
        if dialect_name == "sqlite":
            return sqlite_insert(GmailMailbox).values(**values)
        raise RuntimeError(
            "Atomic Gmail mailbox updates require PostgreSQL or SQLite"
        )

    @staticmethod
    def _find(
        session: Session,
        email_address: str,
        *,
        for_update: bool = False,
    ) -> GmailMailbox | None:
        statement = select(GmailMailbox).where(
            GmailMailbox.email_address == email_address
        )
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)
