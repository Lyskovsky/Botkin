"""Вечерний вопрос «план доеден целиком?» (#407, задача 8).

Раз в день, в вечернем окне, если у пользователя остались открытые планы
(status='plan') на сегодня — бот спрашивает одним сообщением с кнопками
«Да, всё» / «Что-то осталось». Три слоя:
  - core/reminders/plan_close.py — чистая логика (should_ask/тексты/клавиатура)
  - telegram-bot/handlers/plan_close.py — callback-хендлер кнопок
  - scripts/server/send_reminders.py::dispatch_plan_close — диспетчер по всем юзерам
"""

import sys
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT / "telegram-bot"
for p in [str(PROJECT_ROOT), str(BOT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.reminders.plan_close import (
    PLAN_CLOSE_KEY,
    build_keyboard,
    build_question,
    should_ask,
)


# ==================== should_ask ====================


class TestShouldAsk:
    def test_inside_window_and_not_sent_today(self):
        now_local = datetime(2026, 9, 6, 21, 30)
        assert should_ask(now_local, {}, "2026-09-06") is True

    def test_inside_window_but_already_sent_today(self):
        now_local = datetime(2026, 9, 6, 21, 30)
        last_sent = {PLAN_CLOSE_KEY: "2026-09-06"}
        assert should_ask(now_local, last_sent, "2026-09-06") is False

    def test_inside_window_sent_on_a_different_day_is_ok(self):
        now_local = datetime(2026, 9, 6, 21, 30)
        last_sent = {PLAN_CLOSE_KEY: "2026-09-05"}
        assert should_ask(now_local, last_sent, "2026-09-06") is True

    def test_before_window_afternoon(self):
        now_local = datetime(2026, 9, 6, 15, 0)
        assert should_ask(now_local, {}, "2026-09-06") is False

    def test_after_window_late_night(self):
        now_local = datetime(2026, 9, 6, 23, 30)
        assert should_ask(now_local, {}, "2026-09-06") is False

    def test_boundary_start_of_window(self):
        now_local = datetime(2026, 9, 6, 21, 0)
        assert should_ask(now_local, {}, "2026-09-06") is True

    def test_boundary_end_of_window(self):
        now_local = datetime(2026, 9, 6, 22, 59)
        assert should_ask(now_local, {}, "2026-09-06") is True


# ==================== build_question ====================


class TestBuildQuestion:
    def test_singular_one_plan(self):
        text = build_question(1, 496.4)
        assert "1 запись" in text
        assert "496 ккал" in text
        assert "Доедено целиком?" in text

    def test_plural_few_two_plans(self):
        text = build_question(2, 300.0)
        assert "2 записи" in text

    def test_plural_few_three_plans(self):
        text = build_question(3, 300.0)
        assert "3 записи" in text

    def test_plural_many_five_plans(self):
        text = build_question(5, 300.0)
        assert "5 записей" in text

    def test_plural_many_eleven_plans(self):
        # 11 — особый случай "много", несмотря на окончание на 1
        text = build_question(11, 300.0)
        assert "11 записей" in text

    def test_kcal_rounded_no_decimals(self):
        text = build_question(1, 123.456)
        assert "123 ккал" in text


# ==================== build_keyboard ====================


class TestBuildKeyboard:
    def test_callback_data_matches_aiogram_pack_format(self):
        from handlers.callbacks import PlanCloseCallback

        kb = build_keyboard("2026-09-06")
        buttons = [b for row in kb["inline_keyboard"] for b in row]
        callback_datas = {b["callback_data"] for b in buttons}

        expected_all = PlanCloseCallback(action="all", date="2026-09-06").pack()
        expected_edit = PlanCloseCallback(action="edit", date="2026-09-06").pack()

        assert expected_all in callback_datas
        assert expected_edit in callback_datas

    def test_two_buttons(self):
        kb = build_keyboard("2026-09-06")
        buttons = [b for row in kb["inline_keyboard"] for b in row]
        assert len(buttons) == 2


# ==================== handlers/plan_close.py ====================


def _make_callback(action: str, date_str: str, user_id: int):
    from handlers.callbacks import PlanCloseCallback

    callback = AsyncMock()
    callback.from_user = MagicMock()
    callback.from_user.id = user_id
    callback.message = AsyncMock()
    callback_data = PlanCloseCallback(action=action, date=date_str)
    return callback, callback_data


class TestPlanCloseHandler:
    @pytest.mark.asyncio
    async def test_action_all_closes_open_plans_and_edits_message(self, test_db):
        from database.crud import get_open_plans
        from database.models import NutritionLog

        user_id = 895655
        today = date(2026, 9, 6)

        plan1 = NutritionLog(
            user_id=user_id,
            date=today,
            meal_time=time(13, 0),
            meal_name="Обед",
            items=[{"product": "Гречка", "calories": 200}],
            totals={"calories": 200},
            status="plan",
        )
        plan2 = NutritionLog(
            user_id=user_id,
            date=today,
            meal_time=time(19, 0),
            meal_name="Ужин",
            items=[{"product": "Курица", "calories": 296.4}],
            totals={"calories": 296.4},
            status="plan",
        )
        already_eaten = NutritionLog(
            user_id=user_id,
            date=today,
            meal_time=time(9, 0),
            meal_name="Завтрак",
            items=[{"product": "Овсянка", "calories": 150}],
            totals={"calories": 150},
            status="eaten",
        )
        test_db.add_all([plan1, plan2, already_eaten])
        test_db.commit()
        already_eaten_id = already_eaten.id

        import handlers.plan_close as plan_close

        with patch.object(plan_close, "SessionLocal", return_value=test_db):
            callback, callback_data = _make_callback("all", "2026-09-06", user_id)
            await plan_close.handle_plan_close(callback, callback_data)

        callback.answer.assert_awaited_once()

        open_plans = get_open_plans(test_db, user_id, today)
        assert open_plans == []

        # Уже съеденная запись не тронута. Хендлер закрывает свою сессию БД
        # (== test_db, т.к. SessionLocal запатчен) — старые объекты после close()
        # детачатся, поэтому проверяем свежим запросом, а не refresh().
        reloaded = test_db.query(NutritionLog).filter(NutritionLog.id == already_eaten_id).one()
        assert reloaded.status == "eaten"

        edited_text = callback.message.edit_text.call_args.args[0]
        assert "✅" in edited_text
        assert "2 запис" in edited_text
        assert "496" in edited_text  # 200 + 296.4 округлено

    @pytest.mark.asyncio
    async def test_action_all_with_no_open_plans_left(self, test_db):
        user_id = 895656
        import handlers.plan_close as plan_close

        with patch.object(plan_close, "SessionLocal", return_value=test_db):
            callback, callback_data = _make_callback("all", "2026-09-06", user_id)
            await plan_close.handle_plan_close(callback, callback_data)

        callback.answer.assert_awaited_once()
        edited_text = callback.message.edit_text.call_args.args[0]
        assert "Открытых планов уже нет" in edited_text

    @pytest.mark.asyncio
    async def test_action_edit_shows_hint_and_does_not_touch_statuses(self, test_db):
        from database.models import NutritionLog

        user_id = 895657
        today = date(2026, 9, 6)
        plan = NutritionLog(
            user_id=user_id,
            date=today,
            meal_time=time(13, 0),
            meal_name="Обед",
            items=[{"product": "Гречка", "calories": 200}],
            totals={"calories": 200},
            status="plan",
        )
        test_db.add(plan)
        test_db.commit()

        import handlers.plan_close as plan_close

        with patch.object(plan_close, "SessionLocal", return_value=test_db):
            callback, callback_data = _make_callback("edit", "2026-09-06", user_id)
            await plan_close.handle_plan_close(callback, callback_data)

        callback.answer.assert_awaited_once()
        edited_text = callback.message.edit_text.call_args.args[0]
        assert "минус 2 яйца" in edited_text or "напиши" in edited_text.lower()

        test_db.refresh(plan)
        assert plan.status == "plan"  # не тронуто


# ==================== scripts/server/send_reminders.py::dispatch_plan_close ====================


def _seed_user_with_open_plan(db, user_id: int, today: date):
    from database.models import NutritionLog, User, UserSettings

    db.add(User(telegram_id=user_id, username=f"u{user_id}", is_active=True, role="user"))
    db.add(UserSettings(user_id=user_id))
    db.add(
        NutritionLog(
            user_id=user_id,
            date=today,
            meal_time=time(13, 0),
            meal_name="Обед",
            items=[{"product": "Гречка", "calories": 200}],
            totals={"calories": 200},
            status="plan",
        )
    )
    db.commit()


def test_dispatch_plan_close_sends_once_and_is_idempotent(test_db):
    import scripts.server.send_reminders as send_reminders

    today = date(2026, 9, 6)
    user_with_plan = 895700
    user_without_plan = 895701
    _seed_user_with_open_plan(test_db, user_with_plan, today)

    from database.models import User, UserSettings

    test_db.add(User(telegram_id=user_without_plan, username="nolate", is_active=True, role="user"))
    test_db.add(UserSettings(user_id=user_without_plan))
    test_db.commit()

    fake_now = datetime(2026, 9, 6, 21, 30)

    sent_calls = []

    def fake_send(token, chat_id, text, dry, reply_markup=None):
        sent_calls.append((chat_id, dry))
        return True

    with (
        patch.object(send_reminders, "_send", side_effect=fake_send),
        patch("core.infra.tz.get_user_tz", return_value=None),
    ):
        # now_fn делает время «инжектируемым» для теста, без реальных таймзон.
        count = send_reminders.dispatch_plan_close(test_db, token="dummy", dry=True, now_fn=lambda tz: fake_now)

    assert count == 1
    assert sent_calls == [(user_with_plan, True)]

    # Идемпотентность: второй вызов с уже проставленным last_sent ничего не шлёт.
    settings = test_db.query(UserSettings).filter(UserSettings.user_id == user_with_plan).first()
    assert settings.meal_reminder_last_sent.get("__plan_close__") == "2026-09-06"

    sent_calls.clear()
    with (
        patch.object(send_reminders, "_send", side_effect=fake_send),
        patch("core.infra.tz.get_user_tz", return_value=None),
    ):
        count2 = send_reminders.dispatch_plan_close(test_db, token="dummy", dry=True, now_fn=lambda tz: fake_now)

    assert count2 == 0
    assert sent_calls == []


def test_dispatch_plan_close_one_failing_user_does_not_abort_others(test_db):
    """Ошибка отправки одному пользователю не срывает вечерний вопрос остальным."""
    import scripts.server.send_reminders as send_reminders

    today = date(2026, 9, 6)
    failing_uid, ok_uid = 895710, 895711
    _seed_user_with_open_plan(test_db, failing_uid, today)
    _seed_user_with_open_plan(test_db, ok_uid, today)
    fake_now = datetime(2026, 9, 6, 21, 30)
    sent_calls = []

    def fake_send(token, chat_id, text, dry, reply_markup=None):
        if chat_id == failing_uid:
            raise RuntimeError("telegram down for this chat")
        sent_calls.append(chat_id)
        return True

    with (
        patch.object(send_reminders, "_send", side_effect=fake_send),
        patch("core.infra.tz.get_user_tz", return_value=None),
    ):
        count = send_reminders.dispatch_plan_close(test_db, token="dummy", dry=False, now_fn=lambda tz: fake_now)

    assert count == 1
    assert sent_calls == [ok_uid]


def test_dispatch_plan_close_failure_after_success_keeps_earlier_dedup(test_db):
    """Ошибка у ПОСЛЕДУЮЩЕГО пользователя не откатывает дедуп-ключ предыдущего (per-user commit)."""
    import scripts.server.send_reminders as send_reminders
    from database.models import UserSettings

    today = date(2026, 9, 6)
    ok_uid, failing_uid = 895720, 895721  # ok_uid обрабатывается первым (меньший id / порядок вставки)
    _seed_user_with_open_plan(test_db, ok_uid, today)
    _seed_user_with_open_plan(test_db, failing_uid, today)
    fake_now = datetime(2026, 9, 6, 21, 30)

    def fake_send(token, chat_id, text, dry, reply_markup=None):
        if chat_id == failing_uid:
            raise RuntimeError("boom")
        return True

    with (
        patch.object(send_reminders, "_send", side_effect=fake_send),
        patch("core.infra.tz.get_user_tz", return_value=None),
    ):
        count = send_reminders.dispatch_plan_close(test_db, token="dummy", dry=False, now_fn=lambda tz: fake_now)

    assert count == 1
    settings = test_db.query(UserSettings).filter(UserSettings.user_id == ok_uid).first()
    assert settings.meal_reminder_last_sent.get("__plan_close__") == "2026-09-06"
