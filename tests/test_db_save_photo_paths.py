"""Смоук-тест: save_meal_to_db() доносит photo_paths до nutrition_log (#256).

Регрессия: handle_menu_photo() (telegram-bot/handlers/photo.py) писал в state
ключ "photo_path" (ед.ч.) вместо "photo_paths" (мн.ч.), из-за чего
save_meal_to_db() получала meal_data без фото и сохраняла пустой список даже
когда фото реально было. Этот тест бьёт по save_meal_to_db напрямую — с уже
правильной формой meal_data — чтобы зафиксировать контракт независимо от
конкретного хендлера, который её собирает.
"""

from unittest.mock import patch

from database.models import NutritionLog, User

USER_ID = 895655


def _make_user(db, telegram_id=USER_ID):
    db.add(User(telegram_id=telegram_id, first_name="test"))
    db.commit()


def test_save_meal_to_db_persists_photo_paths(test_db):
    _make_user(test_db)
    import helpers.db_save as db_save

    meal_data = {
        "meal_items": [
            {"name": "Гречка с курицей", "weight": 300, "calories": 350, "protein": 25, "fats": 8, "carbs": 45}
        ],
        "meal_totals": {"calories": 350, "protein": 25, "fats": 8, "carbs": 45},
        "photo_paths": ["/app/data/media/nutrition/2026-07-03/photo_123.jpg"],
    }

    with (
        patch.object(db_save, "SessionLocal", return_value=test_db),
        patch.object(test_db, "close"),
    ):
        log_id = db_save.save_meal_to_db(meal_data, "Обед", user_id=USER_ID)
        assert log_id is not None

    row = test_db.query(NutritionLog).filter(NutritionLog.id == log_id).first()
    assert row is not None
    assert row.photo_paths == ["/app/data/media/nutrition/2026-07-03/photo_123.jpg"]


def test_save_meal_to_db_missing_photo_paths_saves_empty_list(test_db):
    """Текстовый флоу без фото — norm, photo_paths остаётся пустым списком."""
    _make_user(test_db)
    import helpers.db_save as db_save

    meal_data = {
        "meal_items": [{"name": "Овсянка", "weight": 100, "calories": 150, "protein": 5, "fats": 3, "carbs": 25}],
        "meal_totals": {"calories": 150, "protein": 5, "fats": 3, "carbs": 25},
    }

    with (
        patch.object(db_save, "SessionLocal", return_value=test_db),
        patch.object(test_db, "close"),
    ):
        log_id = db_save.save_meal_to_db(meal_data, "Завтрак", user_id=USER_ID)
        assert log_id is not None

    row = test_db.query(NutritionLog).filter(NutritionLog.id == log_id).first()
    assert row.photo_paths == []
