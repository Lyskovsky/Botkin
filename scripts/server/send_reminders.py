#!/usr/bin/env python3
"""Диспетчер напоминаний (еда + добавки). Запускается host-cron'ом через docker exec.

Cron на сервере (каждые 15 мин в дневном окне):
    */15 8-23 * * * docker exec <bot_container> python /app/scripts/server/send_reminders.py >> /var/log/botkin_reminders.log 2>&1

- Multi-user: каждому в его локальной TZ (core.infra.tz.get_user_tz).
- Идемпотентно: по каждому слоту/добавке шлём максимум раз в день (last_sent в UserSettings).
- Питание: фиксированные слоты (meal_reminder_times). Если приём уже залогирован рядом — пропускаем.
- Добавки: оживляет ранее «мёртвый» тумблер supplement_reminders_enabled.

Запуск вручную:
    python scripts/server/send_reminders.py            # боевой прогон
    python scripts/server/send_reminders.py --dry-run  # ничего не шлёт, только лог
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover - dotenv опционален
    pass

import requests  # noqa: E402

from core.infra.tz import get_user_tz  # noqa: E402
from core.reminders.meal_reminders import (  # noqa: E402
    DEFAULT_GRACE_MINUTES,
    build_reminder_text,
    due_slots,
    normalize_times,
    parse_hhmm,
)

logger = logging.getLogger("send_reminders")

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 15
SUPPLEMENT_KEY = "__supplement__"  # ключ дедупа добавок внутри meal_reminder_last_sent
SUPPLEMENT_WINDOW_MIN = 120  # окно после supplement_reminder_time
LOGGED_PRE_MINUTES = 90  # «приём залогирован», если запись в [slot-90, slot+grace]


def _send(token: str, chat_id: int, text: str, dry: bool) -> bool:
    if dry:
        logger.info("[dry] -> %s: %s", chat_id, text.replace("\n", " ")[:80])
        return True
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=REQUEST_TIMEOUT)
    body = resp.json()
    if resp.status_code != 200 or not body.get("ok"):
        logger.error("Telegram error chat=%s: %s", chat_id, body.get("description", resp.text))
        return False
    return True


def _logged_labels(db, NutritionLog, uid, today, tz, meal_times, grace=DEFAULT_GRACE_MINUTES):
    """Метки слотов, для которых приём уже залогирован рядом по времени → не напоминаем."""
    logs = db.query(NutritionLog).filter(NutritionLog.user_id == uid, NutritionLog.date == today).all()
    logged_minutes: list[int] = []
    for lg in logs:
        if lg.meal_time is not None:
            logged_minutes.append(lg.meal_time.hour * 60 + lg.meal_time.minute)
        elif lg.created_at is not None:
            local = lg.created_at.astimezone(tz)
            logged_minutes.append(local.hour * 60 + local.minute)
    labels: set[str] = set()
    for label, hhmm in meal_times.items():
        t = parse_hhmm(hhmm)
        slot_min = t.hour * 60 + t.minute
        if any(slot_min - LOGGED_PRE_MINUTES <= m <= slot_min + grace for m in logged_minutes):
            labels.add(label)
    return labels


def run(dry: bool = False) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token and not dry:
        logger.error("TELEGRAM_BOT_TOKEN env var not set")
        return 1

    from database import SessionLocal
    from database.models import NutritionLog, User, UserSettings

    db = SessionLocal()
    sent_total = 0
    try:
        rows = (
            db.query(UserSettings, User)
            .join(User, User.telegram_id == UserSettings.user_id)
            .filter(
                (UserSettings.meal_reminders_enabled.is_(True)) | (UserSettings.supplement_reminders_enabled.is_(True))
            )
            .all()
        )
        for settings, user in rows:
            uid = user.telegram_id
            tz = get_user_tz(uid)
            now_local = datetime.now(tz)
            today = now_local.date()
            today_iso = today.isoformat()
            last_sent = dict(settings.meal_reminder_last_sent or {})
            changed = False

            # --- Питание ---
            if settings.meal_reminders_enabled:
                meal_times = normalize_times(settings.meal_reminder_times or {})
                if meal_times:
                    logged = _logged_labels(db, NutritionLog, uid, today, tz, meal_times)
                    for slot in due_slots(
                        now_local=now_local,
                        meal_times=meal_times,
                        last_sent=last_sent,
                        logged_labels=logged,
                    ):
                        if _send(token, uid, build_reminder_text(slot.label), dry):
                            last_sent[slot.label] = today_iso
                            sent_total += 1
                            changed = True

            # --- Добавки (оживляем мёртвый тумблер) ---
            if settings.supplement_reminders_enabled and settings.supplement_reminder_time:
                if last_sent.get(SUPPLEMENT_KEY) != today_iso:
                    st = settings.supplement_reminder_time
                    slot_dt = now_local.replace(hour=st.hour, minute=st.minute, second=0, microsecond=0)
                    mins_since = (now_local - slot_dt).total_seconds() / 60.0
                    if 0 <= mins_since <= SUPPLEMENT_WINDOW_MIN:
                        text = "💊 Напоминание: не забудьте принять добавки/лекарства и отметить приём."
                        if _send(token, uid, text, dry):
                            last_sent[SUPPLEMENT_KEY] = today_iso
                            sent_total += 1
                            changed = True

            if changed:
                settings.meal_reminder_last_sent = last_sent

        if not dry:
            db.commit()
    finally:
        db.close()

    logger.info("Reminders dispatch done, sent=%d", sent_total)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Диспетчер напоминаний еда+добавки")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять, только логировать")
    args = parser.parse_args()
    return run(dry=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
