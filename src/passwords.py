"""Password hashing shared with the dependency-free deployment setup script."""

import hashlib
import hmac
import secrets

HASH_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Пароль должен содержать от 12 до 1024 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ITERATIONS)
    return ":".join(("pbkdf2_sha256", str(HASH_ITERATIONS), salt.hex(), digest.hex()))


def valid_password_hash(value: str) -> bool:
    try:
        algorithm, iterations, salt, digest = value.split(":")
        return (algorithm == "pbkdf2_sha256" and int(iterations) == HASH_ITERATIONS
                and len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32)
    except (ValueError, TypeError):
        return False


def verify_password(password: str, encoded: str) -> bool:
    if not valid_password_hash(encoded) or len(password) > 1024:
        return False
    _, iterations, salt, expected = encoded.split(":")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(actual, bytes.fromhex(expected))
