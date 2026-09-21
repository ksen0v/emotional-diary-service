"""Шифрование ключей источников.

Смысл теста не в том, что AES-GCM работает, а в том, что смена мастер-ключа
даёт понятную ошибку, а не мусор вместо ключа: иначе после переразвёртывания
сервис молча ходил бы к провайдеру с испорченным ключом и получал 401.
"""

import base64

import pytest

from eds.platform import crypto
from eds.platform.config import settings
from eds.platform.errors import AppError

KEY_A = base64.urlsafe_b64encode(b"A" * 32).decode()
KEY_B = base64.urlsafe_b64encode(b"B" * 32).decode()


@pytest.fixture
def master(monkeypatch):
    def use(value: str) -> None:
        monkeypatch.setenv("EDS_SECRET_KEY", value)
        settings.cache_clear()

    use(KEY_A)
    yield use
    settings.cache_clear()


def test_round_trip(master) -> None:
    blob = crypto.encrypt("R3KYsecretkeyp2a9")
    assert b"R3KY" not in blob  # в базе ключа открытым текстом нет
    assert crypto.decrypt(blob) == "R3KYsecretkeyp2a9"


def test_two_encryptions_differ(master) -> None:
    """Одинаковый ключ шифруется по-разному: нонс каждый раз новый."""
    assert crypto.encrypt("same-key-value") != crypto.encrypt("same-key-value")


def test_changed_master_key_gives_named_error(master) -> None:
    blob = crypto.encrypt("R3KYsecretkeyp2a9")
    master(KEY_B)
    with pytest.raises(AppError) as exc:
        crypto.decrypt(blob)
    assert exc.value.code == "key_undecryptable"


def test_without_master_key_nothing_is_stored(master) -> None:
    master("")
    assert crypto.available() is False
    with pytest.raises(crypto.SecretKeyMissing):
        crypto.encrypt("R3KYsecretkeyp2a9")


def test_bad_master_key_length_is_refused(master) -> None:
    master(base64.urlsafe_b64encode(b"short").decode())
    assert crypto.available() is False


def test_mask_shows_ends_only() -> None:
    assert crypto.mask("R3KYmiddlepartp2a9") == "R3KY********p2a9"
    assert crypto.mask("short") == "*****"


def test_generated_key_fits(master) -> None:
    master(crypto.generate_master_key())
    assert crypto.available() is True
