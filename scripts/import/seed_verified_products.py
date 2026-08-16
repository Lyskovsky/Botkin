#!/usr/bin/env python3
"""Сид справочника verified_products (#255) — общие записи (user_id NULL).

Всегда сеет хардкод-список проверенных продуктов (этикеточные КБЖУ),
опционально досеивает из legacy data/products.json (файл живёт на сервере,
в репозитории его нет).

Запуск (на сервере, внутри контейнера или с DATABASE_URL):
    python3 scripts/import/seed_verified_products.py
    python3 scripts/import/seed_verified_products.py --products-json data/products.json
    python3 scripts/import/seed_verified_products.py --dry-run

Идемпотентен: upsert по (user_id NULL, name_norm).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Этикеточные данные. Всё per 100g; portion_g — вес одной штуки/порции.
# Solvie: этикетка 144 ккал / Б 16.7 / Ж 6.6 / У 4.5 / клетчатка 11.4 на 50 г.
# Bombbar/Fit Kit — из промпта LLM-роутера (проверенные значения линейки).
SEED_PRODUCTS = [
    {
        "name": "Solvie Protein Barre",
        "brand": "Solvie",
        "barcode": "4673728135932",
        "aliases": ["солви протеиновый батончик", "solvie барре", "protein barre"],
        "calories_per_100g": 288.0,
        "protein_per_100g": 33.4,
        "fats_per_100g": 13.2,
        "carbs_per_100g": 9.0,
        "fiber_per_100g": 22.8,
        "portion_g": 50.0,
    },
    {
        "name": "Bombbar глазированный батончик",
        "brand": "Bombbar",
        "aliases": ["bombbar glazed", "бомббар глазированный", "bombbar в шоколаде", "bombbar no sugar added"],
        "calories_per_100g": 355.0,  # 142 ккал / 40 г
        "protein_per_100g": 25.0,
        "fats_per_100g": 17.25,
        "carbs_per_100g": 6.5,
        "fiber_per_100g": 37.5,  # 15 г / 40 г
        "portion_g": 40.0,
    },
    {
        "name": "Bombbar Pro",
        "brand": "Bombbar",
        "aliases": ["бомббар про"],
        "calories_per_100g": 400.0,  # 240 ккал / 60 г
        "protein_per_100g": 33.3,
        "fats_per_100g": 15.0,
        "carbs_per_100g": 33.3,
        "fiber_per_100g": None,
        "portion_g": 60.0,
    },
    {
        "name": "Fit Kit Chocolate Bar",
        "brand": "Fit Kit",
        "aliases": ["фит кит батончик"],
        "calories_per_100g": 346.0,  # 173 ккал / 50 г
        "protein_per_100g": 28.0,
        "fats_per_100g": 10.0,
        "carbs_per_100g": 36.0,
        "fiber_per_100g": None,
        "portion_g": 50.0,
    },
]


def load_legacy_products(path: Path) -> list:
    """products.json → записи сид-формата. Пропускает неполные."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    products_map = data.get("products", data)

    rows = []
    required = ("calories_per_100g", "protein_per_100g", "fats_per_100g", "carbs_per_100g")
    for name, p in products_map.items():
        if not isinstance(p, dict) or any(p.get(k) is None for k in required):
            print(f"  ⚠️ пропуск (неполные КБЖУ): {name}")
            continue
        rows.append(
            {
                "name": p.get("name") or name,
                "brand": p.get("brand"),
                "barcode": p.get("barcode"),
                "aliases": p.get("aliases"),
                "calories_per_100g": float(p["calories_per_100g"]),
                "protein_per_100g": float(p["protein_per_100g"]),
                "fats_per_100g": float(p["fats_per_100g"]),
                "carbs_per_100g": float(p["carbs_per_100g"]),
                "fiber_per_100g": p.get("fiber_per_100g"),
                "portion_g": p.get("weight_g") or p.get("portion_g"),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Сид общих записей verified_products")
    parser.add_argument("--products-json", type=Path, help="путь к legacy data/products.json (опционально)")
    parser.add_argument("--dry-run", action="store_true", help="показать, что будет засеяно, без записи")
    args = parser.parse_args()

    rows = list(SEED_PRODUCTS)
    if args.products_json:
        if args.products_json.exists():
            rows += load_legacy_products(args.products_json)
        else:
            print(f"⚠️ {args.products_json} не найден — сею только хардкод-список")

    if args.dry_run:
        for r in rows:
            print(f"  would seed: {r['name']} ({r['calories_per_100g']:g} ккал/100г)")
        print(f"Итого: {len(rows)} записей (dry-run)")
        return 0

    from core.food.verified_products import normalize_product_name
    from database import SessionLocal, upsert_verified_product

    db = SessionLocal()
    seeded = 0
    try:
        for r in rows:
            upsert_verified_product(
                db,
                user_id=None,  # общая запись, видна всем
                name=r["name"],
                name_norm=normalize_product_name(r["name"]),
                brand=r.get("brand"),
                barcode=r.get("barcode"),
                aliases=r.get("aliases"),
                calories_per_100g=r["calories_per_100g"],
                protein_per_100g=r["protein_per_100g"],
                fats_per_100g=r["fats_per_100g"],
                carbs_per_100g=r["carbs_per_100g"],
                fiber_per_100g=r.get("fiber_per_100g"),
                portion_g=r.get("portion_g"),
                source="import",
            )
            seeded += 1
            print(f"  ✅ {r['name']}")
    finally:
        db.close()

    print(f"Готово: {seeded} общих записей в verified_products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
