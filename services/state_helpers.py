#!/usr/bin/env python3
"""
Helper функции для безопасной работы с состоянием пользователя.

Эти функции предотвращают потерю данных при пересоздании состояния.
"""

from typing import List, Optional, Dict, Any
from pathlib import Path
from services.state import UserState, state_manager
from services.state_models import MealStateData, MenuData, PhotoStateData


def create_photo_state(
    user_id: str,
    photo_paths: List[Path],
    photo_file_ids: List[str],
    caption: str = "",
    menu_data: Optional[Dict] = None,
    existing_state: Optional[UserState] = None,
) -> UserState:
    """
    Создать состояние обработки фото, СОХРАНЯЯ menu_data из existing_state.

    КРИТИЧНО: Эта функция гарантирует что menu_data не теряется при пересоздании состояния!

    Args:
        user_id: ID пользователя
        photo_paths: Список путей к фото
        photo_file_ids: Список Telegram file IDs
        caption: Подпись от пользователя
        menu_data: Новые данные меню (если есть)
        existing_state: Существующее состояние (для сохранения данных)

    Returns:
        Новое UserState с сохраненными данными

    Example:
        >>> # Безопасное пересоздание состояния
        >>> existing = state_manager.get_state(user_id)
        >>> new_state = create_photo_state(
        ...     user_id=user_id,
        ...     photo_paths=[Path('/tmp/photo.jpg')],
        ...     photo_file_ids=['file123'],
        ...     caption='ужин',
        ...     existing_state=existing  # Сохранит menu_data если был
        ... )
    """
    # ВАЖНО: Выбираем menu_data в приоритете:
    # 1. Новый menu_data если передан
    # 2. menu_data из existing_state если есть
    # 3. None если ничего нет
    final_menu_data = menu_data
    if final_menu_data is None and existing_state:
        final_menu_data = existing_state.data.get("menu_data")

    # Используем Pydantic для валидации структуры данных
    state_data = PhotoStateData(
        photo_paths=[str(p) for p in photo_paths],
        photo_file_ids=photo_file_ids,
        caption=caption,
        menu_data=MenuData(**final_menu_data) if final_menu_data else None,
    )

    # Конвертируем обратно в dict для UserState
    return UserState(user_id=user_id, state="waiting_description", data=state_data.model_dump())


def update_state_menu_data(user_id: str, menu_data: Dict, state_mgr=None) -> UserState:
    """
    Обновить menu_data в существующем состоянии, сохраняя все остальное.

    Args:
        user_id: ID пользователя
        menu_data: Новые данные меню
        state_mgr: StateManager instance (для тестов)

    Returns:
        Обновленное состояние

    Raises:
        ValueError: Если состояние не существует
    """
    if state_mgr is None:
        state_mgr = state_manager

    user_state = state_mgr.get_state(user_id)
    if not user_state:
        raise ValueError(f"State for user {user_id} does not exist")

    # Валидируем menu_data через Pydantic
    validated_menu_data = MenuData(**menu_data)

    # Обновляем только menu_data, сохраняя все остальные поля
    data = user_state.data.copy()
    data["menu_data"] = validated_menu_data.model_dump()
    user_state.data = data

    state_mgr.set_state(user_id, user_state)
    return user_state


def get_menu_data_from_state(user_id: str, state_mgr=None) -> Optional[MenuData]:
    """
    Безопасно извлечь menu_data из состояния пользователя.

    Args:
        user_id: ID пользователя
        state_mgr: StateManager instance (для тестов)

    Returns:
        MenuData объект или None если нет состояния/данных
    """
    if state_mgr is None:
        state_mgr = state_manager

    user_state = state_mgr.get_state(user_id)
    if not user_state:
        return None

    menu_data_dict = user_state.data.get("menu_data")
    if not menu_data_dict:
        return None

    return MenuData(**menu_data_dict)


def build_meal_state_data(**kwargs) -> Dict[str, Any]:
    """
    Собрать и провалидировать данные meal-flow состояния (waiting_confirmation).

    Использовать при СОЗДАНИИ dict для UserState.data вместо dict-литерала:
    MealStateData(extra="forbid") ловит опечатку в имени ключа (например
    photo_path вместо photo_paths, #256) сразу здесь, а не тихой потерей
    данных при чтении в save_meal_to_db().

    Raises:
        pydantic.ValidationError: при опечатке в имени ключа или отсутствии
            обязательных полей (meal_items, meal_totals).
    """
    return MealStateData(**kwargs).model_dump(exclude_none=True)


def preserve_and_merge_state_data(
    existing_data: Dict[str, Any], new_data: Dict[str, Any], preserve_keys: List[str] = None
) -> Dict[str, Any]:
    """
    Безопасно слить данные состояния, сохраняя критические ключи.

    Args:
        existing_data: Существующие данные состояния
        new_data: Новые данные
        preserve_keys: Список ключей которые ОБЯЗАТЕЛЬНО сохранить из existing_data
                      По умолчанию: ['menu_data', 'meal_items', 'meal_totals']

    Returns:
        Объединенный dict с сохраненными критическими ключами

    Example:
        >>> existing = {'menu_data': {...}, 'caption': 'old'}
        >>> new = {'caption': 'new', 'photo_paths': [...]}
        >>> merged = preserve_and_merge_state_data(existing, new)
        >>> # menu_data сохранено, caption обновлено, photo_paths добавлено
    """
    if preserve_keys is None:
        preserve_keys = ["menu_data", "meal_items", "meal_totals"]

    # Начинаем с новых данных
    result = new_data.copy()

    # Сохраняем критические ключи из существующих данных
    for key in preserve_keys:
        if key in existing_data and existing_data[key] is not None:
            # Если в new_data нет этого ключа, берем из existing
            if key not in result or result[key] is None:
                result[key] = existing_data[key]

    return result


# Примеры использования в документации

__doc_examples__ = """
# Пример 1: Безопасное пересоздание состояния при обработке caption

```python
from services.state_helpers import create_photo_state, state_manager

# Получить существующее состояние
user_state = state_manager.get_state(user_id)

# Пересоздать состояние БЕЗ потери menu_data
new_state = create_photo_state(
    user_id=user_id,
    photo_paths=photo_paths,
    photo_file_ids=file_ids,
    caption="ужин",
    existing_state=user_state  # ← КРИТИЧНО! Передать existing_state
)

state_manager.set_state(user_id, new_state)
# menu_data сохранено! ✅
```

# Пример 2: Обновление только menu_data

```python
from services.state_helpers import update_state_menu_data

# Обновить только menu_data, сохранив все остальное
menu_data = parse_menu_photo(photo_path)
update_state_menu_data(user_id, menu_data)
# Все поля (caption, photo_paths и т.д.) сохранены ✅
```

# Пример 3: Безопасное слияние данных

```python
from services.state_helpers import preserve_and_merge_state_data

existing = user_state.data
new = {
    'caption': 'новая подпись',
    'photo_paths': ['/new/path']
}

# Слить данные, сохранив menu_data из existing
merged = preserve_and_merge_state_data(existing, new)
user_state.data = merged
# menu_data из existing сохранён, остальное обновлено ✅
```
"""
