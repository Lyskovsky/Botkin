"""Тесты шифрования секретов (core/infra/secrets.py, #381).

SECRETS_KEY в тестах подставляет conftest (_DUMMY_KEYS) — иначе каждый тест
пришлось бы обвешивать monkeypatch.
"""

import pytest

from core.infra.secrets import (
    SecretDecryptError,
    SecretsKeyMissingError,
    decrypt_secret,
    encrypt_secret,
    generate_key,
    is_encrypted,
)


def test_round_trip():
    assert decrypt_secret(encrypt_secret("s3cret-пароль")) == "s3cret-пароль"


def test_versioned_prefix():
    assert encrypt_secret("x").startswith("v1:")


def test_same_input_gives_different_ciphertext():
    """Fernet добавляет случайный IV — шифротексты сравнивать между собой нельзя."""
    assert encrypt_secret("одинаковый") != encrypt_secret("одинаковый")


def test_ciphertext_does_not_contain_plaintext():
    secret = "Avroralion-example"
    assert secret not in encrypt_secret(secret)


def test_empty_secret_rejected():
    with pytest.raises(ValueError):
        encrypt_secret("")


def test_missing_key_raises_not_silently_plaintext(monkeypatch):
    """Без ключа падаем явно: молчаливый plaintext хуже упавшего хендлера."""
    monkeypatch.delenv("SECRETS_KEY", raising=False)
    with pytest.raises(SecretsKeyMissingError):
        encrypt_secret("x")


def test_invalid_key_format_raises(monkeypatch):
    monkeypatch.setenv("SECRETS_KEY", "не-base64-и-не-32-байта")
    with pytest.raises(SecretsKeyMissingError):
        encrypt_secret("x")


def test_invalid_key_message_does_not_leak_key(monkeypatch):
    """В тексте ошибки не должно быть самого ключа — он уедет в логи."""
    monkeypatch.setenv("SECRETS_KEY", "leaky-key-value-should-not-appear")
    with pytest.raises(SecretsKeyMissingError) as e:
        encrypt_secret("x")
    assert "leaky-key-value" not in str(e.value)


def test_decrypt_with_other_key_fails(monkeypatch):
    blob = encrypt_secret("x")
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    with pytest.raises(SecretDecryptError):
        decrypt_secret(blob)


def test_decrypt_garbage_fails():
    with pytest.raises(SecretDecryptError):
        decrypt_secret("v1:мусор")


def test_decrypt_plaintext_without_prefix_fails():
    """Старое plaintext-значение не должно молча пройти как расшифрованное."""
    with pytest.raises(SecretDecryptError):
        decrypt_secret("просто-пароль")


def test_decrypt_unknown_version_fails():
    with pytest.raises(SecretDecryptError):
        decrypt_secret("v9:" + encrypt_secret("x")[3:])


def test_is_encrypted():
    assert is_encrypted(encrypt_secret("x")) is True
    assert is_encrypted("plain") is False
    assert is_encrypted("v1:") is False  # префикс без токена
    assert is_encrypted(None) is False


def test_generate_key_is_usable(monkeypatch):
    monkeypatch.setenv("SECRETS_KEY", generate_key())
    assert decrypt_secret(encrypt_secret("новый ключ")) == "новый ключ"
