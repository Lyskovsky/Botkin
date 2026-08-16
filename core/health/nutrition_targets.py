import logging
import os
import math
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# Значения по умолчанию (можно переопределить через ENV)
DEFAULT_WEIGHT_KG = 82.0
DEFAULT_PROTEIN_PER_KG = 1.8
DEFAULT_FAT_PER_KG_MIN = 0.7
DEFAULT_FAT_PER_KG_MAX = 0.9
DEFAULT_DEFICIT_PCT = 0.15  # 15%

# Fallback для пользователей без Garmin (женщина, ~60 кг)
FALLBACK_BMR_FEMALE = 1400
FALLBACK_ACTIVE_FEMALE = 250


def get_user_settings() -> Dict:
    """Получает настройки из ENV или дефолтные"""
    return {
        "weight_kg": float(os.getenv("TARGET_WEIGHT_KG", DEFAULT_WEIGHT_KG)),
        "protein_per_kg": float(os.getenv("TARGET_PROTEIN_PER_KG", DEFAULT_PROTEIN_PER_KG)),
        "fat_per_kg_min": float(os.getenv("TARGET_FAT_PER_KG_MIN", DEFAULT_FAT_PER_KG_MIN)),
        "fat_per_kg_max": float(os.getenv("TARGET_FAT_PER_KG_MAX", DEFAULT_FAT_PER_KG_MAX)),
        "deficit_pct": float(os.getenv("TARGET_DEFICIT_PCT", DEFAULT_DEFICIT_PCT)),
    }


def calculate_targets(
    avg_tdee: Optional[float] = None,
    stats: Optional[Dict] = None,
    user: Any = None,
    today_tdee: Optional[float] = None,
    today_tdee_final: bool = False,
    adaptive_tdee: Optional[float] = None,
) -> Dict:
    """
    Рассчитывает целевые калории и макросы.
    Источники TDEE: adaptive_tdee > user.bmr+user.avg_active > stats/avg_tdee > fallback.

    adaptive_tdee: maintenance, выведенный из собственных данных питания и веса
        (core/health/adaptive_tdee.py, метод MacroFactor). Приоритетнее девайсов
        и ручных настроек: закрывает энергобаланс по факту, а не по оценке.

    today_tdee: фактический расход (BMR+активные) за конкретный день из Garmin.
        Если он выше базового (среднего/ручного) — берётся он. Это «пол по среднему»:
        в тяжёлый тренировочный день цель растёт по факту, а в неполный/ленивый
        день (today_tdee мал) max() сохраняет стабильную цель по среднему.
        Так одна формула закрывает обе стороны бага: и заниженную цель в день
        тренировки, и скачущую вниз цель из-за неполных Garmin-дней (фикс 04.04.2026).

    today_tdee_final: True для завершённого дня с полным синком (фикс 02.07.2026):
        факт дня — истина, берётся вместо среднего даже если он НИЖЕ (иначе в
        ленивые дни цель по среднему разрешала переедать). Вызывающий обязан сам
        проверить полноту данных дня (get_day_energy_fact) — сюда битый tdee
        передавать нельзя.
    """
    settings = get_user_settings()
    weight = settings["weight_kg"]
    deficit_pct = settings["deficit_pct"]

    # Вес: user.target_weight > ENV > дефолт
    if user and getattr(user, "target_weight_kg", None) and user.target_weight_kg > 0:
        weight = user.target_weight_kg
    elif user and hasattr(user, "target_weight_kg"):
        pass  # use settings['weight_kg']

    FALLBACK_TDEE = FALLBACK_BMR_FEMALE + FALLBACK_ACTIVE_FEMALE  # 1650 — консервативно для пользователя без данных

    estimated_tdee = 0.0

    # 0. Адаптивный TDEE из питания+веса — самый честный источник
    if adaptive_tdee and adaptive_tdee > 1200:
        estimated_tdee = float(adaptive_tdee)
        logger.info(f"[targets] TDEE адаптивный (питание+вес): {estimated_tdee:.0f}")
    # 1. Ручные настройки пользователя (BMR + активные)
    elif user and getattr(user, "bmr", None) and user.bmr and user.bmr > 500:
        bmr_val = float(user.bmr)
        active_val = (
            float(user.avg_active_calories or 0)
            if getattr(user, "avg_active_calories", None)
            else FALLBACK_ACTIVE_FEMALE
        )
        estimated_tdee = bmr_val + active_val
        logger.info(
            f"[targets] TDEE из user: telegram_id={getattr(user, 'telegram_id', None)} bmr={bmr_val} active={active_val} → TDEE={estimated_tdee:.0f}"
        )
    elif stats and (stats.get("total_calories") or stats.get("total", 0) or 0) > 1500:
        estimated_tdee = stats.get("total_calories") or stats.get("total")
        logger.info(
            f"[targets] TDEE из activity stats: total_calories={stats.get('total_calories')} → TDEE={estimated_tdee:.0f}"
        )
    elif avg_tdee and avg_tdee > 1500:
        estimated_tdee = avg_tdee
        logger.info(f"[targets] TDEE из avg_tdee: {avg_tdee:.0f}")
    else:
        estimated_tdee = FALLBACK_TDEE
        logger.info(f"[targets] TDEE fallback (нет user.bmr и stats): {FALLBACK_TDEE:.0f}")

    # Today-boost: реальный сегодняшний расход важнее среднего, если выше.
    # Берём max — никогда не занижаем цель в день тренировки, но и не роняем её
    # в неполный/ленивый день (там today_tdee мал → выигрывает базовый TDEE).
    # Для завершённого дня (today_tdee_final) факт — истина в обе стороны.
    if today_tdee and today_tdee_final:
        logger.info(f"[targets] день завершён: TDEE {estimated_tdee:.0f} → {today_tdee:.0f} (факт дня)")
        estimated_tdee = float(today_tdee)
    elif today_tdee and today_tdee > estimated_tdee:
        logger.info(f"[targets] today-boost: {estimated_tdee:.0f} → {today_tdee:.0f} (факт за день > среднего)")
        estimated_tdee = float(today_tdee)

    # 1. Считаем целевые калории
    target_calories = round(estimated_tdee * (1 - deficit_pct))

    # Проверка на максимальный дефицит (безопасность)
    max_deficit = 800
    actual_deficit = estimated_tdee - target_calories
    if actual_deficit > max_deficit:
        target_calories = round(estimated_tdee - max_deficit)

    # Проверка на превышение TDEE
    if target_calories > estimated_tdee:
        target_calories = round(estimated_tdee)

    # 2. Считаем макросы
    # Белки - фиксировано от веса
    protein_g = round(weight * settings["protein_per_kg"])

    # Жиры - берем минимум для начала
    fats_g = round(weight * settings["fat_per_kg_min"])

    # Углеводы - остаток
    calories_from_protein = protein_g * 4
    calories_from_fats = fats_g * 9
    remaining_kcal_for_carbs = target_calories - calories_from_protein - calories_from_fats
    carbs_g = math.floor(remaining_kcal_for_carbs / 4)

    # Корректировка если углеводов меньше нуля
    if carbs_g < 0:
        # План Б: Снижаем жиры до абсолютного минимума (50г или 0.5г/кг)
        min_fats = max(50, round(weight * 0.5))
        if fats_g > min_fats:
            fats_g = min_fats
            calories_from_fats = fats_g * 9
            remaining_kcal_for_carbs = target_calories - calories_from_protein - calories_from_fats
            carbs_g = math.floor(remaining_kcal_for_carbs / 4)

    if carbs_g < 0:
        # План В: Снижаем белок до 1.6
        min_protein_per_kg = 1.6
        new_protein = round(weight * min_protein_per_kg)
        if protein_g > new_protein:
            protein_g = new_protein
            calories_from_protein = protein_g * 4
            remaining_kcal_for_carbs = target_calories - calories_from_protein - calories_from_fats
            carbs_g = math.floor(remaining_kcal_for_carbs / 4)

    # Если всё равно минус, ставим 0 (значит калорий слишком мало)
    if carbs_g < 0:
        carbs_g = 0

    logger.info(
        f"[targets] Итог: TDEE={estimated_tdee:.0f} вес={weight:.1f} цель_ккал={target_calories} белок={protein_g}г"
    )

    return {
        "calories": target_calories,
        "protein": protein_g,
        "fats": fats_g,
        "carbs": carbs_g,
        "avg_tdee": round(estimated_tdee),
    }


def check_feasibility(remaining_calories: float, remaining_protein: float) -> Optional[str]:
    """
    Проверяет, реально ли набрать оставшийся белок в рамках оставшихся калорий.
    Возвращает предупреждение, если нереально.
    """
    if remaining_calories <= 0:
        if remaining_protein > 5:
            return f"⚠️ Калории закончились, а белка нужно еще {remaining_protein:.0f}г!"
        return None

    # Максимум белка, который теоретически можно уместить в калории (если есть чистый белок)
    # 1г белка = 4 ккал
    max_protein_possible = math.floor(remaining_calories / 4)

    if remaining_protein > max_protein_possible:
        diff = remaining_protein - max_protein_possible
        return (
            f"⚠️ Цель по белку недостижима в рамках калорий.\n"
            f"Осталось {remaining_calories:.0f} ккал, это максимум {max_protein_possible} г белка (чистого).\n"
            f"Не хватает {diff:.0f} г. Рекомендую обезжиренный творог, тунец или протеин на воде."
        )

    # Если белок составляет очень большую часть оставшихся калорий (>70%)
    protein_ratio = (remaining_protein * 4) / remaining_calories
    if protein_ratio > 0.7:
        return (
            f"⚠️ Нужно наедать белок! Он займет {protein_ratio * 100:.0f}% оставшихся калорий.\n"
            f"Выбирай самые нежирные источники: креветки, белок яйца, грудка, треска."
        )

    return None
