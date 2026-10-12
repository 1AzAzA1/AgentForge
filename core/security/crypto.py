"""Криптография профиля: вывод мастер-ключа и шифрование секретов.

Схема (ответ на вопросы 4 и 5):

* Пароль профиля - единственный секрет, который вводит пользователь.
* ``Argon2id(password, salt)`` -> 32-байтовый мастер-ключ. Ключ живёт только
  в оперативной памяти и никогда не пишется на диск.
* Проверка пароля при входе - по хэшу ``Argon2id(password, verify_salt)``,
  сравнение выполняется в постоянном времени.
* API-ключи шифруются ``AES-256-GCM`` мастер-ключом. На диск ложится
  ``nonce(12) || ciphertext || tag`` - сама БД остаётся обычным SQLite,
  открытым для чтения инструментами, но секреты в ней нечитаемы.

Зависимость только одна - ``cryptography``. Argon2id берётся из неё
(версия >= 42 с поддержкой KDF), при её отсутствии - из ``argon2-cffi``.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Параметры Argon2id: ~64 МБ памяти, 3 прохода - разумный компромисс
# между стойкостью и временем отклика десктопного логина (~0.2-0.5 с).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_LANES = 4
KEY_LENGTH = 32
SALT_LENGTH = 16
NONCE_LENGTH = 12


def _derive_raw(password: bytes, salt: bytes, length: int = KEY_LENGTH) -> bytes:
    """Argon2id через ``cryptography`` с откатом на ``argon2-cffi``."""
    try:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

        kdf = Argon2id(
            salt=salt,
            length=length,
            iterations=ARGON2_TIME_COST,
            lanes=ARGON2_LANES,
            memory_cost=ARGON2_MEMORY_KIB,
        )
        return kdf.derive(password)
    except ImportError:  # pragma: no cover - путь для старых cryptography
        from argon2.low_level import Type, hash_secret_raw

        return hash_secret_raw(
            secret=password,
            salt=salt,
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_KIB,
            parallelism=ARGON2_LANES,
            hash_len=length,
            type=Type.ID,
        )


def new_salt() -> bytes:
    """Случайная соль для KDF."""
    return os.urandom(SALT_LENGTH)


def derive_master_key(password: str, salt: bytes) -> bytes:
    """Выводит 32-байтовый мастер-ключ шифрования из пароля профиля."""
    return _derive_raw(password.encode("utf-8"), salt)


def hash_password(password: str, salt: bytes) -> bytes:
    """Хэш для проверки пароля. Соль намеренно ОТЛИЧАЕТСЯ от соли мастер-ключа."""
    return _derive_raw(password.encode("utf-8"), salt)


def verify_password(password: str, salt: bytes, expected_hash: bytes) -> bool:
    """Сравнение хэшей в постоянном времени."""
    return hmac.compare_digest(hash_password(password, salt), expected_hash)


class SecretBox:
    """Шифрование/расшифровка коротких секретов (API-ключей) на мастер-ключе."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != KEY_LENGTH:
            raise ValueError("Мастер-ключ должен быть длиной 32 байта")
        self._aead = AESGCM(master_key)

    def encrypt(self, plaintext: str, associated: str = "") -> bytes:
        """Возвращает ``nonce || ciphertext||tag``."""
        nonce = os.urandom(NONCE_LENGTH)
        blob = self._aead.encrypt(
            nonce, plaintext.encode("utf-8"), associated.encode("utf-8") or None
        )
        return nonce + blob

    def decrypt(self, payload: bytes, associated: str = "") -> str:
        """Обратная операция; бросает исключение при неверном ключе/порче данных."""
        nonce, blob = payload[:NONCE_LENGTH], payload[NONCE_LENGTH:]
        raw = self._aead.decrypt(nonce, blob, associated.encode("utf-8") or None)
        return raw.decode("utf-8")


@dataclass
class Session:
    """Активная сессия пользователя: кто вошёл и на каком мастер-ключе работаем.

    Объект передаётся в репозитории, чтобы они могли прозрачно
    шифровать/расшифровывать поля с секретами.
    """

    user_id: int
    username: str
    box: SecretBox

    def wipe(self) -> None:
        """Обнуляет ссылку на ключ при выходе из профиля."""
        self.box = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Необязательная интеграция с хранилищем секретов ОС ("запомнить пароль")
# ---------------------------------------------------------------------------

_KEYRING_SERVICE = "agent-forge"


def keyring_available() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("keyring") is not None
    except Exception:  # noqa: BLE001
        return False


def keyring_store_password(username: str, password: str) -> bool:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, username, password)
        return True
    except Exception:  # noqa: BLE001
        return False


def keyring_get_password(username: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, username)
    except Exception:  # noqa: BLE001
        return None


def keyring_delete_password(username: str) -> None:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, username)
    except Exception:  # noqa: BLE001
        pass
