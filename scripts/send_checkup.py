"""
Запустить health checkup для конкретного пользователя.

Что делает:
1. Ставит users.checkup_mode = 'active' в БД на сервере
2. Шлёт пользователю сообщение через Bot API — бот инициирует разговор

Использование:
    python3 scripts/send_checkup.py --user 33831673
    python3 scripts/send_checkup.py --user 33831673 --dry-run  # только показать что будет
    python3 scripts/send_checkup.py --user 33831673 --reset    # сбросить checkup_mode

Требует: TELEGRAM_BOT_TOKEN в .env и SSH-доступ к серверу (alias botkin)
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")

CHECKUP_INVITE_TEXT = (
    "Павел Леонидович, добрый день! 👋\n\n"
    "Я хочу задать вам несколько вопросов о вашем здоровье — "
    "чтобы обновить ваш профиль и лучше вам помогать. "
    "Займёт примерно 10 минут.\n\n"
    "Если сейчас удобно — просто напишите «готов» или «да». "
    "Если нет — напишите когда будет время, не тороплю 🙂"
)


def set_checkup_mode(user_id: int, mode: str | None, dry_run: bool = False) -> None:
    """Ставит checkup_mode на сервере через SSH + psql."""
    val = "NULL" if mode is None else f"'{mode}'"
    sql = f"UPDATE users SET checkup_mode = {val} WHERE telegram_id = {user_id};"
    cmd = [
        "ssh",
        "-o",
        "ConnectTimeout=15",
        "botkin",
        f'docker exec healthvault_postgres psql -U healthvault -d healthvault -c "{sql}"',
    ]
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR setting checkup_mode: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ checkup_mode={val} set for user {user_id}")


def send_telegram_message(user_id: int, text: str, dry_run: bool = False) -> None:
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)
    if dry_run:
        print(f"[dry-run] would send to {user_id}:\n{text}")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": user_id, "text": text}, timeout=15)
    if not r.ok:
        print(f"ERROR sending message: {r.text}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Message sent to {user_id}")


def main():
    parser = argparse.ArgumentParser(description="Запустить health checkup через бот")
    parser.add_argument("--user", type=int, required=True, help="telegram_id пользователя")
    parser.add_argument("--dry-run", action="store_true", help="Только показать — не делать")
    parser.add_argument("--reset", action="store_true", help="Сбросить checkup_mode → NULL")
    args = parser.parse_args()

    if args.reset:
        set_checkup_mode(args.user, None, dry_run=args.dry_run)
        print("Checkup mode сброшен.")
        return

    print(f"Запускаем checkup для user_id={args.user}...")
    set_checkup_mode(args.user, "active", dry_run=args.dry_run)
    send_telegram_message(args.user, CHECKUP_INVITE_TEXT, dry_run=args.dry_run)
    print("Готово. Ждём ответа пользователя.")


if __name__ == "__main__":
    main()
