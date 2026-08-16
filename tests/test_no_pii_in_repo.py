"""Сторож приватности публичного репозитория (#303).

Проверяет три инварианта на трекаемых git-файлах:

A. **example-конфиги без реальных ID.** `.env.example` / `.env.production.example`
   не должны публиковать настоящий telegram_id владельца — особенно в
   `BOTKIN_ADMIN_IDS`, который прямо говорит, какой аккаунт админский.

B. **Нет связки «ФИО ↔ telegram_id».** Отдельно имя автора публично намеренно
   (README, NOTICE, pyproject, лендинг), отдельно числовой id — терпимо.
   Опасна именно пара «вот этот человек = вот этот id», по которой внешний
   наблюдатель связывает публичный профиль с записями в БД.

C. **Ratchet по хардкоду owner-id в приложении.** Хардкод реального id в коде
   допустим только в файлах из ALLOWED_HARDCODED_ID — список фиксирован и может
   только сокращаться (Ф2 issue #303). Новые файлы туда не добавляем.

Значение реального id в этом файле НЕ хранится — проверки структурные, либо
берут id из env `BOTKIN_PII_IDS` (в CI — секрет, локально обычно не задан).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Файлы, где хардкод owner-id пока допустим: он load-bearing, а нужной переменной
# нет в прод-окружении. Снимается в Ф2 после добавления BOTKIN_USER_ID в /opt/botkin/.env.
# Список может только сокращаться.
ALLOWED_HARDCODED_ID = {
    "core/health/garmin_data.py",
    "telegram-bot/webhook/apple_health.py",
}

# Приложение (не скрипты и не тесты) — здесь ratchet действует.
APP_DIRS = ("core/", "telegram-bot/", "config/", "database/")

# Значения-плейсхолдеры, допустимые в example-конфигах.
ENV_PLACEHOLDERS = {
    "",
    "your_telegram_id",
    "your_telegram_ids",
    "123456789",
    "<telegram_id>",
}
ENV_ID_KEYS = ("TELEGRAM_USER_ID", "BOTKIN_USER_ID", "HEALTHVAULT_USER_ID", "BOTKIN_ADMIN_IDS", "BOTKIN_ADMIN_ID")

# Кандидат в telegram_id: 6–12 цифр, не часть длинного числа/идентификатора.
ID_LITERAL = re.compile(r"(?<![\w.])(\d{6,12})(?![\w.])")

# Кириллическое ФИО: два слова с заглавных, отчество опционально.
CYRILLIC_FULL_NAME = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}\b")

# Числа, которые выглядят как id, но ими не являются.
NOT_IDS = {
    "1048576",
    "86400000",
    "1000000",
    "100000",
    "123456",
    "123456789",
    "100000001",
    "100000002",
}

# YYYYMMDD — версии моделей (claude-…-20251001), даты в именах файлов.
DATE_LIKE = re.compile(r"^(19|20)\d{6}$")


def _id_candidates(line: str) -> list[str]:
    """Числа из строки, похожие на telegram_id.

    Отсекает URL целиком (PubMed/DOI/статьи несут длинные числа в пути) и
    даты вида YYYYMMDD, иначе сторож тонет в ложных срабатываниях.
    """
    if "http://" in line or "https://" in line:
        return []
    return [m for m in ID_LITERAL.findall(line) if m not in NOT_IDS and not DATE_LIKE.match(m)]


TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".sql",
    ".yml",
    ".yaml",
    ".json",
    ".sh",
    ".html",
    ".js",
    ".css",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".example",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def _read(rel: str) -> str | None:
    path = REPO_ROOT / rel
    if path.suffix not in TEXT_SUFFIXES and not path.name.endswith(".example"):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _text_files() -> list[tuple[str, str]]:
    result = []
    for rel in _tracked_files():
        content = _read(rel)
        if content is not None:
            result.append((rel, content))
    return result


# ── A. example-конфиги ────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", [".env.example", ".env.production.example"])
def test_env_examples_have_no_real_ids(rel):
    """example-конфиг публикует плейсхолдер, а не рабочий telegram_id."""
    path = REPO_ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} отсутствует")

    offenders = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() not in ENV_ID_KEYS:
            continue
        for token in (v.strip() for v in value.split(",")):
            if token not in ENV_PLACEHOLDERS:
                offenders.append(f"{rel}:{lineno} {key.strip()} — значение не плейсхолдер")

    assert not offenders, "Реальный telegram_id в публичном example-конфиге (#303):\n" + "\n".join(offenders)


# ── B. связка ФИО ↔ id ────────────────────────────────────────────────────────


def test_no_name_to_id_binding():
    """Ни в одном трекаемом файле ФИО не стоит на одной строке с telegram_id.

    Именно эта пара превращает публичное авторство в привязку к записям в БД.
    Само имя (README, NOTICE, лендинг) и сам id по отдельности проверку проходят.
    """
    offenders = []
    for rel, content in _text_files():
        if rel == "tests/test_no_pii_in_repo.py":
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if not CYRILLIC_FULL_NAME.search(line):
                continue
            ids = _id_candidates(line)
            if ids:
                offenders.append(f"{rel}:{lineno} — ФИО и id ({', '.join(ids)}) на одной строке")

    assert not offenders, (
        "Связка «реальное ФИО ↔ telegram_id» в публичном репо (#303).\n"
        "Замени id на env/плейсхолдер либо убери имя:\n" + "\n".join(offenders)
    )


# ── C. ratchet по хардкоду в приложении ───────────────────────────────────────


def test_no_new_hardcoded_owner_id_in_app_code():
    """Хардкод telegram-id в коде приложения — только в файлах из ALLOWED_HARDCODED_ID.

    Ratchet: список фиксирован и может только сокращаться. Если правишь один из
    этих файлов и убираешь хардкод — удали строку из ALLOWED_HARDCODED_ID.
    """
    offenders = []
    for rel, content in _text_files():
        if not rel.startswith(APP_DIRS) or not rel.endswith(".py"):
            continue
        if rel in ALLOWED_HARDCODED_ID:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            ids = _id_candidates(line)
            if ids:
                offenders.append(f"{rel}:{lineno} — числовой id-литерал ({', '.join(ids)})")

    assert not offenders, (
        "Новый хардкод telegram_id в коде приложения (#303).\n"
        "Читай id из env (см. паттерн в config/users.py), не хардкодь:\n" + "\n".join(offenders)
    )


def test_allowlist_entries_still_exist():
    """Разрешённые исключения не должны протухать: файл удалён/переименован → чистим список."""
    missing = [rel for rel in ALLOWED_HARDCODED_ID if not (REPO_ROOT / rel).exists()]
    assert not missing, "ALLOWED_HARDCODED_ID ссылается на несуществующие файлы, удали их из списка:\n" + "\n".join(
        missing
    )


# ── B'. точечная проверка по реальным значениям (только если заданы) ──────────


def test_known_pii_ids_absent_from_tracked_files():
    """Если CI передал реальные id в BOTKIN_PII_IDS — их не должно быть в репо вне allowlist.

    Локально переменная обычно не задана → тест скипается. В CI задаётся секретом,
    тогда проверка становится точной, а не структурной.
    """
    raw = os.getenv("BOTKIN_PII_IDS", "").strip()
    if not raw:
        pytest.skip("BOTKIN_PII_IDS не задан — точная проверка пропущена (структурные выше работают всегда)")

    known = {tok.strip() for tok in re.split(r"[,;\s]+", raw) if tok.strip().isdigit()}
    assert known, "BOTKIN_PII_IDS задан, но не содержит числовых id"

    offenders = []
    for rel, content in _text_files():
        if rel in ALLOWED_HARDCODED_ID or rel == "tests/test_no_pii_in_repo.py":
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            hits = known.intersection(ID_LITERAL.findall(line))
            if hits:
                offenders.append(f"{rel}:{lineno} — {', '.join(sorted(hits))}")

    assert not offenders, "Реальный telegram_id в трекаемых файлах (#303):\n" + "\n".join(offenders)
