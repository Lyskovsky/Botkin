"""#409: build_router_result_from_menu_data должен пробрасывать product_label дальше.

Раньше пересобранный `data` терял product_label, и process_llm_food_data не мог
сверить КБЖУ с этикеткой «на 100 г» для меню/чеков, распознанных в 2 захода
(components-путь build_router_result_from_menu_data, см. issue #115).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

from handlers.photo import build_router_result_from_menu_data

LABEL = {"name": "Ролл", "calories_per_100g": 284}


def test_product_label_survives_rebuild():
    menu_data = {"dish_name": "X", "calories": 1, "weight": 10, "product_label": LABEL}
    result = build_router_result_from_menu_data(menu_data)
    assert result["data"]["product_label"] == LABEL
