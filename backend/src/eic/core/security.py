from __future__ import annotations

import base64
import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.fernet import Fernet, InvalidToken

from eic.core.config import get_settings


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    """Verify current scrypt hashes and legacy lowercase SHA-256 hashes."""
    if _is_legacy_sha256(encoded):
        actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, encoded.lower())
    try:
        algorithm, salt_b64, digest_b64 = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _is_legacy_sha256(encoded: str) -> bool:
    return len(encoded) == 64 and all(char in "0123456789abcdefABCDEF" for char in encoded)


def verify_and_upgrade_password(password: str, encoded: str) -> tuple[bool, str]:
    """Return validity and replace a successful legacy hash with scrypt."""
    valid = verify_password(password, encoded)
    if valid and _is_legacy_sha256(encoded):
        return True, hash_password(password)
    return valid, encoded


def credential_stamp(password_hash: str) -> str:
    return hmac.new(get_settings().jwt_secret.encode(),
                    ("eic-auth-v1:" + password_hash).encode(), hashlib.sha256).hexdigest()


def valid_credential_stamp(payload: dict, password_hash: str) -> bool:
    stamp = payload.get("auth", "")
    return isinstance(stamp, str) and hmac.compare_digest(stamp, credential_stamp(password_hash))


def create_access_token(user_id: int, tenant_id: int, username: str, *, auth_stamp: str = "",
                        login_session_id: str = "", session_expires_at: int | None = None) -> str:
    settings = get_settings()
    expires = int((datetime.now(UTC) + timedelta(minutes=settings.access_token_minutes)).timestamp())
    session_expires_at = session_expires_at if session_expires_at is not None else expires
    expires = min(expires, session_expires_at)
    return jwt.encode(
        {"sub": str(user_id), "tenant_id": tenant_id, "username": username, "exp": expires,
         "auth": auth_stamp, "sid": login_session_id or uuid.uuid4().hex,
         "session_exp": session_expires_at, "iat": datetime.now(UTC)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def _fernet() -> Fernet:
    settings = get_settings()
    material = hashlib.sha256(settings.secret_encryption_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def encrypt_secret_if_changed(value: str, previous: str | None) -> str:
    """相同凭据保留原密文，避免已有索引与任务快照因随机加密失配。"""
    if previous:
        try:
            if hmac.compare_digest(decrypt_secret(previous).encode("utf-8"), value.encode("utf-8")):
                return previous
        except (InvalidToken, ValueError, TypeError):
            pass  # 显式提交凭据时允许修复无法解密的旧配置。
    return encrypt_secret(value)


def secret_hint(value: str) -> str:
    """Return a compact masked suffix that always fits ai_models.secret_hint."""
    if not value:
        return ""
    visible = 4
    suffix = value[-visible:]
    hidden = max(0, len(value) - len(suffix))
    return ("*" * min(visible, hidden) + suffix)[:24]
