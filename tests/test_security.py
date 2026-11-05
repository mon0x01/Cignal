from __future__ import annotations

import time

from app.core.security import (
    _totp_at,
    create_access_token,
    decode_token,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    hash_password,
    hash_token,
    new_totp_secret,
    verify_password,
    verify_totp,
)


def test_password_hashing_and_verification():
    encoded = hash_password("correct horse battery staple")
    assert encoded != "correct horse battery staple"
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("incorrect password", encoded)


def test_totp_accepts_current_code_and_rejects_invalid_code(monkeypatch):
    secret = new_totp_secret()
    counter = int(time.time() // 30)
    monkeypatch.setattr("app.core.security.time.time", lambda: counter * 30)
    code = _totp_at(secret, counter)
    assert verify_totp(secret, code)
    assert not verify_totp(secret, "000000" if code != "000000" else "999999")
    assert not verify_totp(secret, "12ab56")


def test_access_token_contains_expected_claims():
    token = create_access_token("user-123", "analyst")
    claims = decode_token(token)
    assert claims["sub"] == "user-123"
    assert claims["role"] == "analyst"
    assert claims["type"] == "access"
    assert claims["jti"]


def test_refresh_token_hash_is_deterministic_and_one_way():
    token = "random-refresh-token-value"
    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != token


def test_mfa_secret_is_encrypted_at_rest():
    secret = new_totp_secret()
    ciphertext = encrypt_mfa_secret(secret)
    assert ciphertext != secret
    assert decrypt_mfa_secret(ciphertext) == secret
