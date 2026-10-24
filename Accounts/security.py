"""
Security helpers: TOTP two-factor authentication and secret encryption.

TOTP follows RFC 6238 (HMAC-SHA1, 30 second step, 6 digits) so it works with
Google Authenticator, Microsoft Authenticator, Authy, 1Password, etc.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # accept one step either side to tolerate clock drift
ISSUER = 'Willy POS'


def _fernet() -> Fernet:
    digest = hashlib.sha256(f'willypos-2fa:{settings.SECRET_KEY}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ''


def generate_totp_secret() -> str:
    """160-bit random secret, base32 encoded without padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def _hotp(secret: str, counter: int) -> str:
    padded = secret + '=' * (-len(secret) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, last_step: int = 0, now: float | None = None):
    """
    Return the matched time-step, or ``None`` when the code is invalid or has
    already been used (replay protection via ``last_step``).
    """
    code = (code or '').strip().replace(' ', '')
    if not secret or len(code) != TOTP_DIGITS or not code.isdigit():
        return None

    current_step = int((time.time() if now is None else now) // TOTP_STEP_SECONDS)
    for step in range(current_step - TOTP_WINDOW, current_step + TOTP_WINDOW + 1):
        if step <= last_step:
            continue
        if hmac.compare_digest(_hotp(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str) -> str:
    label = quote(f'{ISSUER}:{account}')
    return (
        f'otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}'
        f'&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}'
    )


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

RECOVERY_CODE_COUNT = 8
_RECOVERY_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'  # no look-alike characters


def _normalise_recovery_code(code: str) -> str:
    return (code or '').strip().lower().replace('-', '').replace(' ', '')


def hash_recovery_code(code: str) -> str:
    """Keyed hash; codes carry ~50 bits of entropy so a fast hash is adequate."""
    key = f'willypos-recovery:{settings.SECRET_KEY}'.encode()
    return hmac.new(key, _normalise_recovery_code(code).encode(), hashlib.sha256).hexdigest()


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes = []
    for _ in range(count):
        raw = ''.join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f'{raw[:5]}-{raw[5:]}')
    return codes


def consume_recovery_code(hashes: list[str], code: str):
    """Return the remaining hashes if ``code`` matched one of them, else ``None``."""
    normalised = _normalise_recovery_code(code)
    if len(normalised) != 10:
        return None
    candidate = hash_recovery_code(normalised)
    matched = None
    for stored in hashes:
        if hmac.compare_digest(stored, candidate):
            matched = stored
    if matched is None:
        return None
    remaining = list(hashes)
    remaining.remove(matched)
    return remaining


def verify_second_factor(security, code: str) -> bool:
    """
    Check a TOTP code or a recovery code against ``security`` and record its use
    (TOTP step for replay protection, or removal of the consumed recovery code).
    The caller is responsible for running this inside a transaction/row lock.
    """
    step = verify_totp(decrypt_secret(security.totp_secret), code, last_step=security.last_totp_step)
    if step is not None:
        security.last_totp_step = step
        security.save(update_fields=['last_totp_step'])
        return True

    remaining = consume_recovery_code(security.recovery_code_hashes or [], code)
    if remaining is not None:
        security.recovery_code_hashes = remaining
        security.save(update_fields=['recovery_code_hashes'])
        return True

    return False
