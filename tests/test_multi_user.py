#!/usr/bin/env python3
"""
Tests for multi-user authorization and data isolation.
"""

import pytest
from datetime import date, time
from database.models import NutritionLog
from database.crud import ensure_user_exists, get_nutrition_logs_by_period
from config.users import is_admin


class TestMultiUserAuth:
    """Tests for multi-user authorization"""

    def test_admin_user_is_admin(self, monkeypatch):
        """Admin gate works when ADMIN_USER_IDS настроен.

        Источник истины — множество ADMIN_USER_IDS (из env). В тест-окружении env
        не задан, поэтому патчим множество и проверяем is_admin напрямую.
        """
        import config.users as users_mod

        monkeypatch.setattr(users_mod, "ADMIN_USER_IDS", {895655})
        assert users_mod.is_admin(895655) is True
        assert users_mod.is_admin(1) is False

    def test_multiple_admins(self, monkeypatch):
        """#272: несколько админов — is_admin пускает любого из списка."""
        import config.users as users_mod

        monkeypatch.setattr(users_mod, "ADMIN_USER_IDS", {895655, 830908046})
        assert users_mod.is_admin(895655) is True
        assert users_mod.is_admin(830908046) is True
        assert users_mod.is_admin(1) is False

    def test_no_admins_configured_nobody_is_admin(self, monkeypatch):
        """Пустой список админов → никто не админ (дефолт безопасный)."""
        import config.users as users_mod

        monkeypatch.setattr(users_mod, "ADMIN_USER_IDS", set())
        assert users_mod.is_admin(895655) is False
        assert users_mod.is_admin(0) is False

    def test_parse_admin_ids_list(self, monkeypatch):
        """#272: BOTKIN_ADMIN_IDS — список через запятую (с пробелами/точкой с запятой)."""
        import config.users as users_mod

        monkeypatch.setenv("BOTKIN_ADMIN_IDS", " 895655, 830908046 ;  7 ")
        monkeypatch.delenv("BOTKIN_ADMIN_ID", raising=False)
        assert users_mod._parse_admin_ids() == {895655, 830908046, 7}

    def test_parse_admin_ids_backward_compat_legacy(self, monkeypatch):
        """#272: старая одиночная BOTKIN_ADMIN_ID продолжает работать и объединяется."""
        import config.users as users_mod

        monkeypatch.delenv("BOTKIN_ADMIN_IDS", raising=False)
        monkeypatch.setenv("BOTKIN_ADMIN_ID", "895655")
        assert users_mod._parse_admin_ids() == {895655}

        # И BOTKIN_ADMIN_IDS, и legacy заданы → объединение.
        monkeypatch.setenv("BOTKIN_ADMIN_IDS", "830908046")
        assert users_mod._parse_admin_ids() == {895655, 830908046}

    def test_parse_admin_ids_ignores_garbage_and_zero(self, monkeypatch):
        """Нечисловые токены, пустые и 0 игнорируются."""
        import config.users as users_mod

        monkeypatch.setenv("BOTKIN_ADMIN_IDS", "0, abc, , 895655, -5")
        monkeypatch.delenv("BOTKIN_ADMIN_ID", raising=False)
        assert users_mod._parse_admin_ids() == {895655}

    def test_parse_admin_ids_empty_when_unset(self, monkeypatch):
        """Ничего не задано → пустое множество (никто не админ)."""
        import config.users as users_mod

        monkeypatch.delenv("BOTKIN_ADMIN_IDS", raising=False)
        monkeypatch.delenv("BOTKIN_ADMIN_ID", raising=False)
        assert users_mod._parse_admin_ids() == set()

    def test_random_user_is_not_admin(self):
        """Test that random users don't have admin privileges"""
        assert is_admin(999999999) is False

    def test_ensure_user_creates_new_user(self, test_db):
        """Test that ensure_user_exists creates new users — open registration"""
        test_user_id = 123456789

        user = ensure_user_exists(test_db, telegram_id=test_user_id, username="test_user", first_name="Test")

        assert user is not None
        assert user.telegram_id == test_user_id
        assert user.username == "test_user"
        assert user.first_name == "Test"
        assert user.is_active is True

    def test_ensure_user_updates_existing(self, test_db):
        """Test that ensure_user_exists updates existing users"""
        test_user_id = 987654321

        # Create user first time
        user1 = ensure_user_exists(test_db, telegram_id=test_user_id, username="user1")

        # Call again - should update, not duplicate
        user2 = ensure_user_exists(test_db, telegram_id=test_user_id, username="user1")

        assert user1.telegram_id == user2.telegram_id
        assert user2.last_active is not None

    def test_new_user_gets_default_settings(self, test_db):
        """Test that new users get UserSettings with sane defaults"""
        from database.crud import get_user_settings

        test_user_id = 777000111
        ensure_user_exists(test_db, telegram_id=test_user_id, username="newbie")

        settings = get_user_settings(test_db, test_user_id)
        assert settings is not None
        assert settings.calorie_goal_pct == -15
        assert settings.show_calorie_budget_bar is True


class TestDataIsolation:
    """Tests for user data isolation"""

    def test_users_cannot_see_others_nutrition_logs(self, test_db):
        """Test that users can't access each other's nutrition data"""
        user1_id = 895655
        user2_id = 999999

        # Ensure both users exist
        ensure_user_exists(test_db, user1_id, "user1")
        ensure_user_exists(test_db, user2_id, "user2")

        # User 1 logs food
        log1 = NutritionLog(
            user_id=user1_id,
            date=date.today(),
            meal_time=time(12, 0),
            meal_name="обед",
            items=[{"name": "курица", "weight": 100}],
            totals={"calories": 165, "protein": 31, "fats": 3.6, "carbs": 0},
        )
        test_db.add(log1)
        test_db.commit()

        # User 2 shouldn't see user 1's data
        user2_logs = get_nutrition_logs_by_period(test_db, user2_id, date.today(), date.today())

        assert len(user2_logs) == 0

        # User 1 should see their own data
        user1_logs = get_nutrition_logs_by_period(test_db, user1_id, date.today(), date.today())

        assert len(user1_logs) == 1
        assert user1_logs[0].user_id == user1_id

    def test_different_users_can_have_same_date_logs(self, test_db):
        """Test that multiple users can log data on the same date"""
        user1_id = 100001
        user2_id = 100002

        ensure_user_exists(test_db, user1_id, "user1")
        ensure_user_exists(test_db, user2_id, "user2")

        today = date.today()

        # Both users log food on same date
        for uid in [user1_id, user2_id]:
            log = NutritionLog(
                user_id=uid,
                date=today,
                meal_time=time(12, 0),
                meal_name="обед",
                items=[{"name": "еда", "weight": 100}],
                totals={"calories": 200, "protein": 10, "fats": 5, "carbs": 20},
            )
            test_db.add(log)

        test_db.commit()

        # Each user should only see their own log
        user1_logs = get_nutrition_logs_by_period(test_db, user1_id, today, today)
        user2_logs = get_nutrition_logs_by_period(test_db, user2_id, today, today)

        assert len(user1_logs) == 1
        assert len(user2_logs) == 1
        assert user1_logs[0].user_id == user1_id
        assert user2_logs[0].user_id == user2_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
