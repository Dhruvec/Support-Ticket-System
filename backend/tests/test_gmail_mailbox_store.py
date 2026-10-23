from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.database.gmail_mailbox_store import (
    GmailCredentialEncryptionError,
    GmailMailboxStore,
)
from app.database.models import GmailMailbox


def encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


def encrypted_store(session_factory, key: str | None = None) -> GmailMailboxStore:
    return GmailMailboxStore(
        session_factory,
        encryption_key=key or encryption_key(),
    )


def stored_credentials(session_factory, email_address: str) -> dict[str, Any]:
    with session_factory() as session:
        mailbox = session.scalar(
            select(GmailMailbox).where(
                GmailMailbox.email_address == email_address,
            )
        )
        assert mailbox is not None
        assert isinstance(mailbox.credentials, dict)
        return dict(mailbox.credentials)


def test_mailbox_store_initializes_cursor_without_credentials(session_factory) -> None:
    store = encrypted_store(session_factory)

    assert store.get("support@example.com") is None
    store.set("support@example.com", "100")

    assert store.get("support@example.com") == "100"
    assert store.get_credentials("support@example.com") is None


def test_mailbox_store_encrypts_credentials_and_preserves_cursor(session_factory) -> None:
    store = encrypted_store(session_factory)
    credentials: dict[str, Any] = {
        "token": "access-token",
        "refresh_token": "refresh-token",
        "scopes": ["gmail.modify", "gmail.send"],
    }

    store.set_credentials("support@example.com", credentials)
    store.set("support@example.com", "101")

    persisted = stored_credentials(session_factory, "support@example.com")
    assert persisted["__support_credentials__"] == "gmail_oauth_credentials"
    assert persisted["version"] == 1
    assert "access-token" not in str(persisted)
    assert "refresh-token" not in str(persisted)

    loaded = store.get_credentials("support@example.com")
    assert loaded == credentials
    assert loaded is not credentials
    assert store.get("support@example.com") == "101"

    loaded["token"] = "changed"
    assert store.get_credentials("support@example.com")["token"] == "access-token"


def test_mailbox_store_updates_credentials_without_losing_cursor(session_factory) -> None:
    store = encrypted_store(session_factory)
    store.set_credentials("support@example.com", {"token": "old"})
    store.set("support@example.com", "102")

    store.set_credentials("support@example.com", {"token": "new"})

    assert store.get_credentials("support@example.com") == {"token": "new"}
    assert store.get("support@example.com") == "102"


def test_mailbox_store_deletes_only_selected_mailbox(session_factory) -> None:
    store = encrypted_store(session_factory)
    store.set_credentials("stale@example.com", {"token": "stale"})
    store.set_credentials("active@example.com", {"token": "active"})

    assert store.delete("stale@example.com") is True
    assert store.delete("stale@example.com") is False
    assert store.get_credentials("stale@example.com") is None
    assert store.get_credentials("active@example.com") == {"token": "active"}


def test_mailbox_store_keeps_mailboxes_isolated(session_factory) -> None:
    store = encrypted_store(session_factory)
    store.set_credentials("one@example.com", {"token": "one"})
    store.set_credentials("two@example.com", {"token": "two"})
    store.set("one@example.com", "201")
    store.set("two@example.com", "202")

    assert store.get_credentials("one@example.com") == {"token": "one"}
    assert store.get_credentials("two@example.com") == {"token": "two"}
    assert store.get("one@example.com") == "201"
    assert store.get("two@example.com") == "202"


def test_mailbox_store_rejects_missing_key_for_credentials(session_factory) -> None:
    store = GmailMailboxStore(session_factory)

    with pytest.raises(
        GmailCredentialEncryptionError,
        match="GMAIL_CREDENTIALS_ENCRYPTION_KEY is required",
    ):
        store.set_credentials("support@example.com", {"token": "secret"})


def test_mailbox_store_rejects_invalid_encryption_key(session_factory) -> None:
    with pytest.raises(ValueError, match="Fernet key"):
        GmailMailboxStore(
            session_factory,
            encryption_key="not-a-valid-fernet-key",
        )


def test_mailbox_store_wrong_key_fails_clearly(session_factory) -> None:
    encrypted_store(session_factory).set_credentials(
        "support@example.com",
        {"refresh_token": "secret"},
    )
    wrong_key_store = encrypted_store(session_factory)

    with pytest.raises(
        GmailCredentialEncryptionError,
        match="Unable to decrypt Gmail credentials",
    ):
        wrong_key_store.get_credentials("support@example.com")


def test_mailbox_store_rejects_malformed_ciphertext(session_factory) -> None:
    store = encrypted_store(session_factory)
    with session_factory.begin() as session:
        session.add(
            GmailMailbox(
                email_address="support@example.com",
                credentials={
                    "__support_credentials__": "gmail_oauth_credentials",
                    "version": 1,
                    "ciphertext": "not-valid-ciphertext",
                },
            )
        )

    with pytest.raises(
        GmailCredentialEncryptionError,
        match="Unable to decrypt Gmail credentials",
    ):
        store.get_credentials("support@example.com")


def test_mailbox_store_does_not_treat_partial_envelope_as_plaintext(
    session_factory,
) -> None:
    store = encrypted_store(session_factory)
    with session_factory.begin() as session:
        session.add(
            GmailMailbox(
                email_address="support@example.com",
                credentials={"version": 1},
            )
        )

    with pytest.raises(
        GmailCredentialEncryptionError,
        match="envelope is malformed",
    ):
        store.get_credentials("support@example.com")


def test_mailbox_store_migrates_legacy_plaintext_on_read(session_factory) -> None:
    key = encryption_key()
    store = encrypted_store(session_factory, key)
    legacy = {
        "token": "legacy-token",
        "refresh_token": "legacy-refresh-token",
    }
    with session_factory.begin() as session:
        session.add(
            GmailMailbox(
                email_address="support@example.com",
                credentials=legacy,
                history_id="301",
            )
        )

    assert store.get_credentials("support@example.com") == legacy
    persisted = stored_credentials(session_factory, "support@example.com")
    assert persisted != legacy
    assert "legacy-token" not in str(persisted)
    assert store.get("support@example.com") == "301"


def test_mailbox_store_requires_key_to_migrate_legacy_plaintext(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        session.add(
            GmailMailbox(
                email_address="support@example.com",
                credentials={"token": "legacy-token"},
            )
        )

    with pytest.raises(
        GmailCredentialEncryptionError,
        match="GMAIL_CREDENTIALS_ENCRYPTION_KEY is required",
    ):
        GmailMailboxStore(session_factory).get_credentials("support@example.com")


def test_mailbox_store_cursor_only_advances_monotonically(session_factory) -> None:
    store = encrypted_store(session_factory)

    store.set("support@example.com", "900")
    store.set("support@example.com", "899")
    store.set("support@example.com", "0900")
    store.set("support@example.com", "1000")
    store.set("support@example.com", "999")

    assert store.get("support@example.com") == "1000"


def test_mailbox_store_cursor_update_does_not_replace_credentials(
    session_factory,
) -> None:
    store = encrypted_store(session_factory)
    store.set_credentials("support@example.com", {"token": "secret"})

    store.set("support@example.com", "401")
    store.set("support@example.com", "400")

    assert store.get("support@example.com") == "401"
    assert store.get_credentials("support@example.com") == {"token": "secret"}


@pytest.mark.parametrize("history_id", ["", "-1", "1.5", "history-1", "１２３"])
def test_mailbox_store_rejects_non_numeric_history_ids(
    session_factory,
    history_id: str,
) -> None:
    store = encrypted_store(session_factory)

    with pytest.raises(ValueError, match="ASCII digits"):
        store.set("support@example.com", history_id)
