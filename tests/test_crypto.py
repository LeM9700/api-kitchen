"""Tests pour le chiffrement Fernet des secrets stockes en base (tokens OAuth POS)."""
import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.services import crypto


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "pos_token_encryption_key", Fernet.generate_key().decode())


def test_encrypt_then_decrypt_roundtrips():
    ciphertext = crypto.encrypt_secret("super-secret-token")
    assert ciphertext != "super-secret-token"
    assert crypto.decrypt_secret(ciphertext) == "super-secret-token"


def test_encrypt_raises_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "pos_token_encryption_key", "")
    with pytest.raises(crypto.CryptoNotConfigured):
        crypto.encrypt_secret("x")


def test_decrypt_raises_when_key_not_configured(monkeypatch):
    ciphertext = crypto.encrypt_secret("x")
    monkeypatch.setattr(settings, "pos_token_encryption_key", "")
    with pytest.raises(crypto.CryptoNotConfigured):
        crypto.decrypt_secret(ciphertext)


def test_decrypt_raises_invalid_token_for_tampered_ciphertext():
    ciphertext = crypto.encrypt_secret("value")
    tampered = ciphertext[:-4] + ("aaaa" if ciphertext[-4:] != "aaaa" else "bbbb")
    with pytest.raises(InvalidToken):
        crypto.decrypt_secret(tampered)
