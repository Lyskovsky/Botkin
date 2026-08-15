# -*- coding: utf-8 -*-
"""Тесты досинка «замороженных» прошедших Garmin-дней.

Прецедент 15.08.2026: сервер синкал только «сегодня», строка прошедшего дня
оставалась вечерним частичным снимком (BMR/total занижены), 14-дневное среднее
занижало базовый расход и цель калорий на ~300-400 ккал.
"""

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from core.health.garmin_data import (
    _FINAL_GRACE_HOURS,
    _RESYNC_LOOKBACK_DAYS,
    _day_is_final,
    _resync_stale_past_days,
)

TZ = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 8, 15)
YESTERDAY = TODAY - timedelta(days=1)


def _row(synced_at):
    return SimpleNamespace(synced_at=synced_at)


def _day_end(day):
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=TZ)


class TestDayIsFinal:
    def test_missing_row_not_final(self):
        assert _day_is_final(None, YESTERDAY, TZ) is False

    def test_row_without_synced_at_not_final(self):
        assert _day_is_final(_row(None), YESTERDAY, TZ) is False

    def test_evening_snapshot_not_final(self):
        # Синк в 21:30 того же дня — промежуточный снимок
        synced = datetime.combine(YESTERDAY, time(21, 30), tzinfo=TZ)
        assert _day_is_final(_row(synced), YESTERDAY, TZ) is False

    def test_early_next_morning_not_final(self):
        # 00:05 следующего дня — часы ещё не досинкали день (grace-период)
        synced = _day_end(YESTERDAY) + timedelta(minutes=5)
        assert _day_is_final(_row(synced), YESTERDAY, TZ) is False

    def test_after_grace_final(self):
        synced = _day_end(YESTERDAY) + timedelta(hours=_FINAL_GRACE_HOURS, minutes=1)
        assert _day_is_final(_row(synced), YESTERDAY, TZ) is True

    def test_naive_synced_at_treated_as_utc(self):
        # timestamptz может прийти naive из SQLite-тестов — трактуем как UTC
        synced_utc_naive = (
            (_day_end(YESTERDAY) + timedelta(hours=_FINAL_GRACE_HOURS, minutes=1))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
        assert _day_is_final(_row(synced_utc_naive), YESTERDAY, TZ) is True


class TestResyncStalePastDays:
    def _run(self, rows_by_day, client_stats=None):
        """rows_by_day: {date: activity_row}; отсутствующие дни → None."""
        client = MagicMock()
        client.get_stats.return_value = (
            client_stats
            if client_stats is not None
            else {
                "totalKilocalories": 2523.0,
                "activeKilocalories": 606.0,
                "bmrKilocalories": 1917.0,
                "totalSteps": 11805,
            }
        )
        saved = []
        with (
            patch(
                "core.health.garmin_data.get_activity_by_date",
                side_effect=lambda db, uid, d: rows_by_day.get(d),
            ),
            patch(
                "core.health.garmin_data._save_garmin_stats",
                side_effect=lambda db, uid, d, s: saved.append(d),
            ),
            patch("core.infra.tz.get_user_tz", return_value=TZ),
        ):
            n = _resync_stale_past_days(client, db=MagicMock(), user_id=895655, today=TODAY)
        return n, saved, client

    def test_stale_yesterday_resynced(self):
        stale = _row(datetime.combine(YESTERDAY, time(21, 30), tzinfo=TZ))
        final = _row(datetime.now(timezone.utc))  # свежий синк — финален для старых дней
        rows = {YESTERDAY: stale}
        for i in range(2, _RESYNC_LOOKBACK_DAYS + 1):
            rows[TODAY - timedelta(days=i)] = final
        n, saved, _ = self._run(rows)
        assert n == 1
        assert saved == [YESTERDAY]

    def test_final_days_untouched(self):
        final = _row(datetime.now(timezone.utc))
        rows = {TODAY - timedelta(days=i): final for i in range(1, _RESYNC_LOOKBACK_DAYS + 1)}
        n, saved, client = self._run(rows)
        assert n == 0
        assert saved == []
        client.get_stats.assert_not_called()

    def test_missing_rows_backfilled(self):
        # Дни вообще без строки (отпуск/дырка синка) тоже перечитываются
        n, saved, client = self._run({})
        assert n == _RESYNC_LOOKBACK_DAYS
        assert client.get_stats.call_count == _RESYNC_LOOKBACK_DAYS

    def test_api_error_on_one_day_does_not_break_others(self):
        client = MagicMock()
        client.get_stats.side_effect = [Exception("boom")] + [
            {"totalKilocalories": 2000.0} for _ in range(_RESYNC_LOOKBACK_DAYS - 1)
        ]
        saved = []
        with (
            patch("core.health.garmin_data.get_activity_by_date", return_value=None),
            patch(
                "core.health.garmin_data._save_garmin_stats",
                side_effect=lambda db, uid, d, s: saved.append(d),
            ),
            patch("core.infra.tz.get_user_tz", return_value=TZ),
        ):
            n = _resync_stale_past_days(client, db=MagicMock(), user_id=895655, today=TODAY)
        assert n == _RESYNC_LOOKBACK_DAYS - 1

    def test_empty_stats_skipped(self):
        n, saved, _ = self._run({}, client_stats={})
        assert n == 0
        assert saved == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
