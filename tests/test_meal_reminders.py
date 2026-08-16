"""Тесты чистой логики напоминаний о еде (core/reminders/meal_reminders.py)."""

from datetime import datetime

import pytest

from core.reminders.meal_reminders import (
    DEFAULT_GRACE_MINUTES,
    DEFAULT_MEAL_TIMES,
    build_reminder_text,
    due_slots,
    normalize_times,
    parse_hhmm,
)

TIMES = {"Завтрак": "11:00", "Обед": "14:30", "Ужин": "22:00"}


def _at(h, m):
    return datetime(2026, 6, 28, h, m)


def test_parse_hhmm_ok():
    t = parse_hhmm("14:30")
    assert (t.hour, t.minute) == (14, 30)


@pytest.mark.parametrize("bad", ["25:00", "11:99", "abc", "", "1130"])
def test_parse_hhmm_bad(bad):
    with pytest.raises(ValueError):
        parse_hhmm(bad)


def test_normalize_drops_garbage_keeps_valid():
    raw = {"Завтрак": "11:00", "Мусор": "99:99", "Обед": "14:30"}
    assert normalize_times(raw) == {"Завтрак": "11:00", "Обед": "14:30"}
    assert normalize_times(None) == {}
    assert normalize_times({}) == {}


def test_due_exactly_at_slot():
    due = due_slots(now_local=_at(11, 0), meal_times=TIMES, last_sent={}, logged_labels=set())
    assert [d.label for d in due] == ["Завтрак"]


def test_due_within_grace():
    due = due_slots(now_local=_at(12, 30), meal_times=TIMES, last_sent={}, logged_labels=set())
    assert [d.label for d in due] == ["Завтрак"]  # 90 мин после 11:00 ещё в окне (grace 120)


def test_not_due_before_slot():
    due = due_slots(now_local=_at(10, 30), meal_times=TIMES, last_sent={}, logged_labels=set())
    assert due == []


def test_not_due_after_grace():
    # 11:00 + 120 = 13:00; в 13:05 завтрак уже протух
    due = due_slots(now_local=_at(13, 5), meal_times=TIMES, last_sent={}, logged_labels=set())
    assert [d.label for d in due] == []


def test_idempotent_already_sent_today():
    due = due_slots(
        now_local=_at(11, 10),
        meal_times=TIMES,
        last_sent={"Завтрак": "2026-06-28"},
        logged_labels=set(),
    )
    assert due == []


def test_sent_yesterday_still_due():
    due = due_slots(
        now_local=_at(11, 10),
        meal_times=TIMES,
        last_sent={"Завтрак": "2026-06-27"},
        logged_labels=set(),
    )
    assert [d.label for d in due] == ["Завтрак"]


def test_logged_slot_skipped():
    due = due_slots(
        now_local=_at(11, 10),
        meal_times=TIMES,
        last_sent={},
        logged_labels={"Завтрак"},
    )
    assert due == []


def test_lunch_slot_independent():
    due = due_slots(now_local=_at(14, 40), meal_times=TIMES, last_sent={}, logged_labels=set())
    assert [d.label for d in due] == ["Обед"]


def test_empty_times_never_due():
    assert due_slots(now_local=_at(11, 0), meal_times={}, last_sent={}, logged_labels=set()) == []


def test_defaults_sane():
    assert DEFAULT_MEAL_TIMES == {"Завтрак": "11:00", "Обед": "14:30", "Ужин": "22:00"}
    assert DEFAULT_GRACE_MINUTES == 120


def test_reminder_text_mentions_meal():
    assert "завтрак" in build_reminder_text("Завтрак").lower()
