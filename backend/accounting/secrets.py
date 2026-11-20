"""Authenticated encryption for accounting-system connection credentials.

AES-256-GCM with a random 96-bit nonce stored as the ciphertext prefix. The
connection UUID is bound as AAD, so a sealed secret copied to another
connection row cannot be opened. Keys are versioned to allow rotation: new
secrets are sealed with the active version, old ones still open with the key
version recorded next to them.

Nothing here ever puts plaintext, ciphertext or key material into exception
messages or ``repr`` output.
"""

import base64
import binascii
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables

KEY_LENGTH = 32
NONCE_LENGTH = 12
TAG_LENGTH = 16


class SecretError(Exception):
    """A sealed secret could not be opened. The message never contains secret data."""


@dataclass(frozen=True)
class SealedSecret:
    ciphertext: bytes = field(repr=False)
    key_version: int

    def __repr__(self) -> str:
        return f"SealedSecret(key_version={self.key_version}, ciphertext=<{len(self.ciphertext)} bytes hidden>)"

    __str__ = __repr__


@sensitive_variables()
def _decode_key(version: int, raw: object) -> bytes:
    if not isinstance(raw, str):
        raise ImproperlyConfigured(f"Ключ шифрования версии {version} должен быть строкой base64.")
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ImproperlyConfigured(f"Ключ шифрования версии {version} не является корректной строкой base64.") from None
    if len(key) != KEY_LENGTH:
        raise ImproperlyConfigured(f"Ключ шифрования версии {version} должен содержать ровно {KEY_LENGTH} байта.")
    return key


class SecretBox:
    # sensitive_variables(): production error reports (mail_admins, DEBUG off) print frame locals, which hold
    # key material. Django's filter is inactive under DEBUG=True, so DEBUG pages still show them.
    @sensitive_variables()
    def __init__(self, keys: Mapping[int, str], active_version: int):
        if not keys:
            raise ImproperlyConfigured("Не заданы ключи шифрования секретов учётной системы (ACCOUNTING_SECRET_KEYS).")
        if active_version not in keys:
            raise ImproperlyConfigured(
                f"Активная версия ключа шифрования ({active_version}) отсутствует в ACCOUNTING_SECRET_KEYS."
            )
        self._ciphers = {int(version): AESGCM(_decode_key(version, raw)) for version, raw in keys.items()}
        self._active_version = int(active_version)

    @sensitive_variables()
    def seal(self, plaintext: str, *, aad: uuid.UUID) -> SealedSecret:
        try:
            data = plaintext.encode("utf-8")
        except UnicodeEncodeError:
            # Not chained: the original error text names the offending character.
            raise SecretError("Секрет содержит символы, которые нельзя зашифровать.") from None
        nonce = os.urandom(NONCE_LENGTH)
        encrypted = self._ciphers[self._active_version].encrypt(nonce, data, aad.bytes)
        return SealedSecret(ciphertext=nonce + encrypted, key_version=self._active_version)

    def open(self, sealed: SealedSecret, *, aad: uuid.UUID) -> str:
        cipher = self._ciphers.get(sealed.key_version)
        if cipher is None:
            raise SecretError("Ключ шифрования для этого секрета недоступен.")
        data = bytes(sealed.ciphertext)
        if len(data) < NONCE_LENGTH + TAG_LENGTH:
            raise SecretError("Не удалось расшифровать секрет.")
        nonce, encrypted = data[:NONCE_LENGTH], data[NONCE_LENGTH:]
        try:
            return cipher.decrypt(nonce, encrypted, aad.bytes).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            raise SecretError("Не удалось расшифровать секрет.") from None


def get_secret_box() -> SecretBox:
    return SecretBox(settings.ACCOUNTING_SECRET_KEYS, settings.ACCOUNTING_SECRET_ACTIVE_KEY_VERSION)
