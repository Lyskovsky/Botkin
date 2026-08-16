"""Чистая логика напоминаний о логировании еды (без БД и сети — тестируемо).

Режим: фиксированные слоты приёмов пищи в локальной таймзоне пользователя
(назначение эндокринолога Романовой: завтрак 11:00, обед 14:30, ужин 22:00).
К каждому слоту, если приём не залогирован рядом, шлём один мягкий пинг в день.

Диспетчер (`scripts/server/send_reminders.py`) поставляет сюда уже посчитанные
из БД факты (now в локальной TZ, был ли приём залогирован у слота), а решение
«какие слоты пора напомнить» принимается здесь — чисто и предсказуемо.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

# Дефолтные слоты при включении (режим Романовой). label -> "HH:MM".
DEFAULT_MEAL_TIMES: dict[str, str] = {
    "Завтрак": "11:00",
    "Обед": "14:30",
    "Ужин": "22:00",
}

# Сколько минут после времени слота ещё уместно напомнить (позже — слот «протух»).
DEFAULT_GRACE_MINUTES = 120


@dataclass(frozen=True)
class DueSlot:
    label: str  # "Завтрак"
    time_str: str  # "11:00"


def parse_hhmm(value: str) -> time:
    """'11:00' -> datetime.time(11, 0). Кидает ValueError на мусоре."""
    hh, mm = value.strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Некорректное время: {value!r}")
    return time(hour=h, minute=m)


def normalize_times(raw: dict | None) -> dict[str, str]:
    """Привести {label: 'HH:MM'} к валидному виду, выкинув мусор. Пустой/None -> {}."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for label, val in raw.items():
        try:
            t = parse_hhmm(str(val))
        except (ValueError, AttributeError):
            continue
        out[str(label)] = f"{t.hour:02d}:{t.minute:02d}"
    return out


def due_slots(
    *,
    now_local: datetime,
    meal_times: dict[str, str],
    last_sent: dict[str, str] | None,
    logged_labels: set[str],
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> list[DueSlot]:
    """Вернуть слоты, по которым ПОРА напомнить прямо сейчас.

    Args:
        now_local: текущий момент в локальной TZ пользователя (naive или aware — берём дату/время как есть).
        meal_times: {label: 'HH:MM'} — настроенные слоты.
        last_sent: {label: 'YYYY-MM-DD'} — когда по слоту в последний раз слали (идемпотентность).
        logged_labels: множество label'ов, для которых приём уже залогирован рядом (не напоминаем).
        grace_minutes: окно после времени слота, в течение которого ещё шлём.

    Слот «due», если: сейчас в [slot, slot+grace], сегодня по нему ещё не слали и он не залогирован.
    """
    last_sent = last_sent or {}
    today_iso = now_local.date().isoformat()
    result: list[DueSlot] = []

    for label, hhmm in normalize_times(meal_times).items():
        if label in logged_labels:
            continue
        if last_sent.get(label) == today_iso:
            continue
        slot_t = parse_hhmm(hhmm)
        slot_dt = now_local.replace(hour=slot_t.hour, minute=slot_t.minute, second=0, microsecond=0)
        minutes_since = (now_local - slot_dt).total_seconds() / 60.0
        if 0 <= minutes_since <= grace_minutes:
            result.append(DueSlot(label=label, time_str=hhmm))

    return result


def build_reminder_text(label: str) -> str:
    """Мягкий пинг под конкретный слот."""
    meal = label.lower()
    return (
        f"🍽 Напоминание: не забудьте залогировать {meal}.\nПришлите фото блюда или опишите текстом — я посчитаю КБЖУ."
    )
