"""Шифрование ключей источников.

Ключ TMM и секрет Binance лежат в базе зашифрованными: пока мы не можем
проверить, что ключ read-only (Архитектура 4.8), надо исходить из того,
что он полный. Мастер-ключ живёт вне базы — в переменной окружения локально
и в файле с правами 600 на сервере.
"""

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from eds.platform.config import settings
from eds.platform.errors import AppError

NONCE_BYTES = 12
KEY_VERSION = 1


class SecretKeyMissing(RuntimeError):
    """Мастер-ключ не задан. Подключать источники нельзя."""


def _master_key() -> bytes:
    raw = settings().secret_key.strip()
    if not raw:
        raise SecretKeyMissing(
            "EDS_SECRET_KEY не задан: без него ключи источников хранить нельзя"
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # noqa: BLE001 — любая проблема декодирования равнозначна
        raise SecretKeyMissing("EDS_SECRET_KEY не в base64") from exc
    if len(key) != 32:
        raise SecretKeyMissing("EDS_SECRET_KEY должен быть 32 байта в base64")
    return key


def available() -> bool:
    try:
        _master_key()
    except SecretKeyMissing:
        return False
    return True


def encrypt(plaintext: str) -> bytes:
    """Зашифровать секрет. Нонс кладётся в начало — расшифровке хватит самих байтов."""
    nonce = os.urandom(NONCE_BYTES)
    box = AESGCM(_master_key())
    return nonce + box.encrypt(nonce, plaintext.encode("utf-8"), None)


def decrypt(blob: bytes) -> str:
    box = AESGCM(_master_key())
    try:
        return box.decrypt(blob[:NONCE_BYTES], bytes(blob[NONCE_BYTES:]), None).decode()
    except InvalidTag as exc:
        # Чаще всего это значит, что мастер-ключ сменили, а данные остались старыми.
        raise AppError(
            "key_undecryptable",
            "Ключ источника не расшифровывается: похоже, сменился ключ шифрования. "
            "Подключи источник заново.",
            409,
        ) from exc


def mask(secret: str) -> str:
    """Маска для показа: первые четыре и последние четыре символа."""
    clean = secret.strip()
    if len(clean) <= 10:
        return "*" * len(clean)
    return f"{clean[:4]}{'*' * 8}{clean[-4:]}"


def generate_master_key() -> str:
    """Сгенерировать мастер-ключ. Вызывается руками, не приложением."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()
