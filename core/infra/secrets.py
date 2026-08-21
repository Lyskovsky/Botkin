#!/usr/bin/env python3
"""Шифрование пользовательских секретов (пароли follower-аккаунтов CGM и т.п.).

Зачем: до этого модуля в проекте не было НИ ОДНОГО механизма шифрования —
`users.garmin_password` лежит plaintext, а колонки `encrypted_*_key` не читаются
и не пишутся нигде. Поэтому чужие пароли приходилось держать в `.env` прода
(см. docs/researches/2026-08-17-cgm-follower-self-service.md).

Алгоритм: Fernet (AES-128-CBC + HMAC-SHA256) из `cryptography`. Ключ — env
`SECRETS_KEY`, 32 байта в base64url (ровно то, что печатает `generate_key()`).

Версионный префикс в самой строке (`v1:<token>`) — чтобы смена ключа или
алгоритма не требовала миграции данных: новые значения пишутся как `v2:`,
а читаются оба, пока старые не вытеснятся.

Ключа нет → `encrypt_secret` падает с `SecretsKeyMissingError`, а НЕ пишет
plaintext молча: молчаливая деградация здесь опаснее упавшего хендлера.

Генерация ключа для прода:
    python -m core.infra.secrets --generate-key
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

# Версия → env-переменная с ключом. При ротации добавляем "v2": "SECRETS_KEY_V2",
# начинаем писать v2 (CURRENT_VERSION) и продолжаем читать v1, пока он есть в БД.
_KEY_ENV_BY_VERSION: dict[str, str] = {"v1": "SECRETS_KEY"}
CURRENT_VERSION = "v1"

# Кэш по значению ключа, а не по имени env: тесты подменяют SECRETS_KEY через
# monkeypatch, и кэш «по имени» отдавал бы им Fernet от прежнего ключа.
_fernet_cache: dict[str, Fernet] = {}


class SecretsKeyMissingError(RuntimeError):
    """`SECRETS_KEY` не задан или не является валидным Fernet-ключом."""


class SecretDecryptError(RuntimeError):
    """Значение не расшифровывается: не тот ключ, чужой формат или повреждение."""


def _fernet(version: str) -> Fernet:
    """Fernet для указанной версии префикса. Кэшируется по значению ключа."""
    env_name = _KEY_ENV_BY_VERSION.get(version)
    if env_name is None:
        raise SecretDecryptError(f"неизвестная версия шифрования {version!r} — нет ключа для неё")
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        raise SecretsKeyMissingError(
            f"{env_name} не задан — шифрование секретов недоступно. "
            f"Сгенерировать: python -m core.infra.secrets --generate-key"
        )
    cached = _fernet_cache.get(raw)
    if cached is None:
        try:
            cached = Fernet(raw.encode())
        except (ValueError, TypeError) as e:
            # Текст ошибки библиотеки не тащим — в нём может оказаться сам ключ.
            raise SecretsKeyMissingError(
                f"{env_name} задан, но это не валидный Fernet-ключ (нужны 32 байта в base64url, как из --generate-key)"
            ) from e
        _fernet_cache[raw] = cached
    return cached


def encrypt_secret(plain: str) -> str:
    """Зашифровать секрет. Возвращает `"v1:<fernet-token>"`.

    Fernet добавляет случайный IV, поэтому один и тот же пароль даёт разные
    строки при каждом вызове — сравнивать шифротексты между собой нельзя.
    """
    if not isinstance(plain, str) or not plain:
        raise ValueError("нечего шифровать: секрет пустой")
    token = _fernet(CURRENT_VERSION).encrypt(plain.encode()).decode()
    return f"{CURRENT_VERSION}:{token}"


def decrypt_secret(blob: str) -> str:
    """Расшифровать строку вида `"v1:<token>"`. Понимает все известные версии."""
    if not isinstance(blob, str) or ":" not in blob:
        raise SecretDecryptError("значение не похоже на зашифрованное (нет префикса версии)")
    version, _, token = blob.partition(":")
    if version not in _KEY_ENV_BY_VERSION:
        raise SecretDecryptError(f"неизвестная версия шифрования {version!r}")
    try:
        return _fernet(version).decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise SecretDecryptError("секрет не расшифровывается: ключ не тот или значение повреждено") from e


def is_encrypted(blob: object) -> bool:
    """True, если строка выглядит как результат `encrypt_secret` (любая версия).

    Нужно для миграции существующих plaintext-значений: читаем как есть,
    перезаписываем зашифрованным.
    """
    if not isinstance(blob, str):
        return False
    version, sep, token = blob.partition(":")
    return bool(sep) and version in _KEY_ENV_BY_VERSION and bool(token)


def generate_key() -> str:
    """Новый ключ для `SECRETS_KEY` (base64url, 32 байта)."""
    return Fernet.generate_key().decode()


if __name__ == "__main__":  # pragma: no cover - утилита для деплоя
    import argparse

    parser = argparse.ArgumentParser(description="Утилиты шифрования секретов Botkin")
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="напечатать новый SECRETS_KEY (положить в .env прода, никуда не коммитить)",
    )
    args = parser.parse_args()
    if args.generate_key:
        print(generate_key())
    else:
        parser.print_help()
