#!/usr/bin/env python3
import psycopg2
import psycopg2.extras
import json
import os
from datetime import date, time, datetime
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL", "postgresql://healthvault:***REMOVED-SECRET***@db:5432/healthvault")
# telegram_id владельца — из env, не хардкодом в публичном репо (#303).
OWNER_USER_ID = int(os.getenv("BOTKIN_USER_ID") or os.getenv("HEALTHVAULT_USER_ID") or 0)


class CustomJSONEncoder(json.JSONEncoder):
    """Кастомный енкодер для дат и времени в JSON."""

    def default(self, obj):
        if isinstance(obj, (date, datetime, time)):
            return obj.isoformat()
        return super().default(obj)


def sync_data():
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        print("✅ Успешно подключились к удаленной БД")

        # 1. Nutrition Log
        cur.execute("SELECT * FROM nutrition_log WHERE user_id = %s ORDER BY date, meal_time", (OWNER_USER_ID,))
        nutrition_records = []
        for row in cur.fetchall():
            nutrition_records.append(dict(row))

        os.makedirs("data/nutrition", exist_ok=True)
        with open("data/nutrition/nutrition_log_remote.json", "w", encoding="utf-8") as f:
            json.dump(nutrition_records, f, ensure_ascii=False, indent=2, cls=CustomJSONEncoder)
        print(f"✅ Сохранено {len(nutrition_records)} записей питания")

        # 2. Supplements Log
        cur.execute("SELECT * FROM supplements_log WHERE user_id = %s ORDER BY date, time", (OWNER_USER_ID,))
        supps_records = []
        for row in cur.fetchall():
            supps_records.append(dict(row))

        os.makedirs("data/supplements", exist_ok=True)
        with open("data/supplements/supplements_log_remote.json", "w", encoding="utf-8") as f:
            json.dump(supps_records, f, ensure_ascii=False, indent=2, cls=CustomJSONEncoder)
        print(f"✅ Сохранено {len(supps_records)} записей добавок/витаминов")

        # 3. Weights — ВСЕ источники (apple_health_v2 от HAE, zepp_life от Zepp API,
        # screenshot_ocr/manual от бота, исторические импорты). Фильтрация по дате
        # делается на стороне дашборда — здесь забираем всё, чтобы не терять записи.
        cur.execute(
            "SELECT * FROM weights WHERE user_id = %s ORDER BY measured_at",
            (OWNER_USER_ID,),
        )
        weight_records = []
        for row in cur.fetchall():
            weight_records.append(dict(row))

        os.makedirs("data/weights", exist_ok=True)
        with open("data/weights/weights_remote.json", "w", encoding="utf-8") as f:
            json.dump(weight_records, f, ensure_ascii=False, indent=2, cls=CustomJSONEncoder)
        print(f"✅ Сохранено {len(weight_records)} записей веса/состава тела (все источники)")

        # 4. Blood pressure logs — ручные Omron замеры через Apple Health → HAE → сервер
        cur.execute(
            "SELECT * FROM blood_pressure_logs WHERE user_id = %s ORDER BY measured_at",
            (OWNER_USER_ID,),
        )
        bp_records = []
        for row in cur.fetchall():
            bp_records.append(dict(row))

        with open("data/weights/blood_pressure_remote.json", "w", encoding="utf-8") as f:
            json.dump(bp_records, f, ensure_ascii=False, indent=2, cls=CustomJSONEncoder)
        print(f"✅ Сохранено {len(bp_records)} замеров АД (Omron → Apple Health → HAE)")

        # 5. Garmin Activity Log + HAE-обогащение (steps, RHR, walking, weight, BP raw_data)
        cur.execute("SELECT * FROM activity_log WHERE user_id = %s ORDER BY date", (OWNER_USER_ID,))
        activity_records = []
        for row in cur.fetchall():
            activity_records.append(dict(row))

        os.makedirs("data/activities", exist_ok=True)
        with open("data/activities/activities_remote.json", "w", encoding="utf-8") as f:
            json.dump(activity_records, f, ensure_ascii=False, indent=2, cls=CustomJSONEncoder)
        print(f"✅ Сохранено {len(activity_records)} записей activity_log (Garmin + HAE)")

        cur.close()
        conn.close()

    except Exception as e:
        print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    sync_data()
