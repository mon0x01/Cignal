"""Cryptographic helpers: Argon2id passwords, JWTs, opaque refresh tokens and RFC 6238 TOTP."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return _password_hasher.verify(encoded, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def create_access_token(user_id: str, role: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"], options={"require": ["sub", "exp", "type"]})



def _mfa_fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret_key.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_mfa_secret(secret: str) -> str:
    """Encrypt TOTP secrets at rest; APP_SECRET_KEY must be backed up securely."""
    return _mfa_fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_mfa_secret(ciphertext: str) -> str:
    try:
        return _mfa_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Unable to decrypt the stored MFA secret; verify APP_SECRET_KEY") from exc


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_at(secret: str, counter: int, digits: int = 6) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def verify_totp(secret: str, code: str) -> bool:
    supplied = str(code).strip()
    if not supplied.isdigit() or len(supplied) != 6:
        return False
    counter = int(time.time() // 30)
    return any(hmac.compare_digest(_totp_at(secret, counter + offset), supplied) for offset in (-1, 0, 1))


def provisioning_uri(secret: str, email: str) -> str:
    label = quote(f"Cignal:{email}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer=Cignal&algorithm=SHA1&digits=6&period=30"
