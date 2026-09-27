from datetime import datetime, timedelta, timezone

import pytest

from app.core.http.errors import AppError
from app.modules.loyalty.account import service


def test_loyalty_qr_token_is_signed_and_decodable():
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    token = service._encode_qr_token(
        {
            "typ": "loyalty_qr",
            "tenant": "test",
            "sub": 123,
            "exp": int(expires_at.timestamp()),
            "nonce": "abc",
        }
    )

    payload = service._decode_qr_token(token)

    assert payload["typ"] == "loyalty_qr"
    assert payload["tenant"] == "test"
    assert payload["sub"] == 123


def test_loyalty_qr_token_payload_contains_no_personal_data():
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    token = service._encode_qr_token(
        {
            "typ": "loyalty_qr",
            "tenant": "test",
            "sub": 123,
            "exp": int(expires_at.timestamp()),
            "nonce": "abc",
        }
    )

    payload = service._decode_qr_token(token)

    assert set(payload) == {"typ", "tenant", "sub", "exp", "nonce"}
    assert "phone" not in payload
    assert "email" not in payload
    assert "full_name" not in payload


def test_loyalty_qr_token_rejects_tampering():
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    token = service._encode_qr_token(
        {
            "typ": "loyalty_qr",
            "tenant": "test",
            "sub": 123,
            "exp": int(expires_at.timestamp()),
            "nonce": "abc",
        }
    )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(AppError) as exc:
        service._decode_qr_token(tampered)

    assert exc.value.code == "INVALID_LOYALTY_QR"


def test_loyalty_qr_token_rejects_expired_payload():
    expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    token = service._encode_qr_token(
        {
            "typ": "loyalty_qr",
            "tenant": "test",
            "sub": 123,
            "exp": int(expires_at.timestamp()),
            "nonce": "abc",
        }
    )

    with pytest.raises(AppError) as exc:
        service._decode_qr_token(token)

    assert exc.value.code == "LOYALTY_QR_EXPIRED"
