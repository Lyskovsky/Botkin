#!/usr/bin/env python3
"""
Обработчик команд бота (/start, /help, /day, /week, /vitamins и т.д.)
"""

from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from datetime import datetime, timedelta

from core.infra.tz import get_user_tz  # noqa: E402

from core.health.garmin_data import get_garmin_data_for_date, sync_today_garmin
from core.health.weekly_nutrition import analyze_weekly_nutrition
from core.health.nutrition_targets import check_feasibility
from config.users import is_admin
from config.settings import public_base_url
# NOTE: SupplementService imported per-request to support multi-user

router = Router()


# Персональные приветствия на /start для отдельных пользователей, у которых
# главный сценарий — медицинский Q&A с агентом (а не трекинг еды).
# Тексты содержат PII (имена, медконтекст, ссылки на дашборды) → вынесены в
# приватный модуль config/personal_greetings_private.py (в .gitignore).
# Если файла нет (напр. чистый клон) — персональных приветствий просто не будет.
try:
    from config.personal_greetings_private import PERSONAL_START_GREETINGS
except ImportError:
    PERSONAL_START_GREETINGS = {}


@router.message(Command("start"))
async def cmd_start(message: Message, user_id: int, username: str, first_name: str):
    """Обработчик команды /start - регистрация и приветствие"""
    from database import SessionLocal
    from database.crud import ensure_user_exists, get_average_activity_stats

    db = SessionLocal()
    try:
        user = ensure_user_exists(db, telegram_id=user_id, username=username, first_name=first_name)
        # Check if user has any Garmin/activity data for calorie targets
        has_activity = bool(get_average_activity_stats(db, user_id, days=30))
        has_manual_bmr = bool(user.bmr)
    except Exception as e:
        db.close()
        await message.answer(f"❌ Error registering user: {e}")
        return
    finally:
        db.close()

    # Персональное приветствие (если задано) — и выходим, не показывая food-блок
    personal = PERSONAL_START_GREETINGS.get(user_id)
    if personal:
        await message.answer(personal, parse_mode="HTML", disable_web_page_preview=True)
        return

    setup_hint = ""
    if not has_activity and not has_manual_bmr:
        setup_hint = (
            "\n⚙️ <b>Нет данных о калориях?</b> Введи /setup BMR 1400 активные 250 "
            "— бот сразу начнёт считать твой бюджет. Или просто логируй еду, "
            "цели подстроятся по мере накопления данных.\n"
        )

    await message.answer(
        f"👋 Привет, {first_name}! Я <b>Botkin</b> — твой трекер питания, добавок и здоровья.\n\n"
        "📸 Отправь фото еды — распознаю состав и КБЖУ.\n"
        "🗣 Или голосом: «Съел яблоко», «Принял Омега-3 1000 мг».\n"
        "📝 Или текстом: «Завтрак: овсянка 200г, кофе».\n\n"
        "🍽 Кнопка <b>«Дневник»</b> снизу — мини-приложение с дашбордом: "
        "калории, витамины, активность, тренды.\n"
        f"{setup_hint}\n"
        "Команды:\n"
        "/day — итоги дня\n"
        "/week — анализ недели\n"
        "/vitamins — чек-лист добавок\n"
        "/share — личный дашборд здоровья\n"
        "/report — снимок дашборда (постоянная ссылка)\n"
        "/doctor_report — PDF-отчёт для врача (файл)\n"
        "/profile — твои данные (вес, рост, возраст)\n"
        "/help — полная справка\n\n"
        "🌐 botkin.health",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка по командам"""
    await message.answer(
        "📖 <b>Справка по боту:</b>\n\n"
        "🍽 <b>Еда:</b>\n"
        "📸 <b>Фото:</b> Фото тарелки. Бот поймет состав и вес.\n"
        "🗣 <b>Голос:</b> 'Завтрак: 2 яйца и хлеб'.\n"
        "📝 <b>Текст:</b> 'Ужин: стейк и салат'.\n"
        "<i>💡 Называйте прием (Завтрак/Ужин) или учту текущее время. Это для контроля углеводов.</i>\n"
        "<i>💡 Уточняйте: 'вареная гречка', 'сухой рис', 'готовая паста' — иначе бот считает как сухой продукт.</i>\n"
        "📋 <b>План:</b> 'План: 3 яйца, творог' — внести еду авансом, потом 'минус 2 яйца' или 'съела всё'.\n\n"
        "💊 <b>Витамины и Лекарства:</b>\n"
        "📸 <b>Фото:</b> Таблетки на ладони или упаковки.\n"
        "🗣 <b>Голос:</b> 'Выпил утренние', 'Принял нурофен'.\n"
        "📝 <b>Текст:</b> 'Омега и Д3 плюс'.\n\n"
        "⚙️ <b>Команды:</b>\n"
        "/day [дата] — Итоги дня (можно 'вчера' или '26.02').\n"
        "/week — Анализ рациона за неделю.\n"
        "/vitamins — Чек-лист и схема приема.\n"
        "/share — Личный дашборд здоровья (ссылка).\n"
        "/profile — Настройка профиля (рост, возраст, цель).\n"
        "/setup — Настройка BMR и активных ккал (без Garmin).\n"
        "/activity &lt;число&gt; — Ввести активные калории за сегодня вручную.\n"
        "/help — Эта справка."
    )


@router.message(Command("day"))
async def cmd_day(message: Message, user_id: int):
    """Показывает итоги дня"""
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"📊 /day user={message.from_user.id} ({message.from_user.first_name})")

    db = None
    try:
        from database import SessionLocal
        from database.crud import get_user_settings

        db = SessionLocal()
        _us = get_user_settings(db, user_id)
        show_bar = _us.show_calorie_budget_bar if _us else True

        real_today = datetime.now(get_user_tz(user_id)).date()
        today_date = real_today

        # Check if user asked for a specific date
        text = message.text.lower() if message.text else ""
        if "вчера" in text or "yesterday" in text:
            today_date -= timedelta(days=1)
        else:
            parts = text.split()
            if len(parts) > 1:
                date_str = parts[1]
                for fmt in ("%d.%m", "%d.%m.%Y", "%Y-%m-%d"):
                    try:
                        parsed = datetime.strptime(date_str, fmt).date()
                        if fmt == "%d.%m":
                            parsed = parsed.replace(year=today_date.year)
                        today_date = parsed
                        break
                    except ValueError:
                        pass

        today_str = today_date.strftime("%Y-%m-%d")
        today_formatted = today_date.strftime("%d.%m.%Y")
        activity_label = "сегодня" if today_date == real_today else today_formatted

        # Garmin Data — для сегодня синхронизируем через API (с 15-мин кешем),
        # для исторических дат читаем только из БД.
        # ВАЖНО: синк идёт ДО расчёта цели (get_day_stats), чтобы today-boost
        # увидел свежий фактический расход за день (иначе цель считается по стейлу).
        garmin_error = False
        if today_date == real_today:
            active_calories, garmin_status = sync_today_garmin(user_id, today_date)
            garmin_error = garmin_status == "error"
        else:
            garmin_data = get_garmin_data_for_date(today_str, user_id=user_id)
            active_calories = garmin_data.get("activeKilocalories", 0.0) or 0.0 if garmin_data else 0.0

        # New Service Logic
        from services.nutrition_service import get_nutrition_service

        service = get_nutrition_service(user_id=user_id)
        stats = service.get_day_stats(today_date)

        from database.crud import get_open_plans

        open_plans = get_open_plans(db, user_id, today_date)
        plans_text = ""
        if open_plans:
            plans_kcal = sum(float((p.totals or {}).get("calories") or 0) for p in open_plans)
            plans_text = f"\n📋 В плане, не закрыто: {len(open_plans)} · {plans_kcal:.0f} ккал (уже в итоге)"

        totals = stats["totals"]
        targets = stats["targets"]
        remaining = stats["remaining"]

        # Supplements Status - create per-user instance
        from core.health.supplements import SupplementService

        user_supplement_service = SupplementService(user_id=user_id)
        supplements_text = user_supplement_service.get_brief_status(for_date=today_str)

        # Apple Health - latest weight
        from database.crud import get_latest_weight

        weight_text = ""
        weight = get_latest_weight(db, user_id)
        if weight:
            # Only show if recorded today
            if weight.measured_at.date() == today_date:
                weight_text = f"⚖️ Вес: <b>{weight.weight} кг</b>"

        # --- Energy balance (14-day averages → consistent with target calculation) ---
        from database.crud import get_average_activity_stats
        from core.health.caloric_budget import make_block_bar

        avg_stats = get_average_activity_stats(db, user_id, days=14)
        avg_bmr = round(avg_stats.get("bmr_calories", 0)) if avg_stats else 0
        avg_active = round(avg_stats.get("active_calories", 0)) if avg_stats else 0
        avg_total = round(avg_stats.get("total_calories", 0)) if avg_stats else 0
        today_active_r = round(active_calories)
        target_cal = targets["calories"]

        deficit_pct = round((1 - 0.85) * 100)  # 15%
        # Завершённый день: цель посчитана от факта дня (см. get_day_stats),
        # подписываем это явно, чтобы цифра не расходилась с (BMR+ср.актив)×0.85.
        is_past_day = today_date < real_today
        goal_suffix = " от факта дня" if (is_past_day and not stats.get("data_incomplete")) else ""
        # Источник цели (адаптивный TDEE и т.п.) в строку НЕ выносим — детали
        # живут в справке миниаппа (решение 16.08.2026, длинная подпись ломала
        # раскладку на узких экранах).
        if avg_total > 1500:
            if garmin_error:
                active_line = f"🏃 ⚠️ Garmin недоступен · {avg_active} в среднем"
            else:
                active_line = f"🏃 {today_active_r} ккал {activity_label} · {avg_active} в среднем"
            energy_line = (
                f"💤 {avg_bmr} ккал — базовый расход\n"
                f"{active_line}\n"
                f"🎯 {target_cal} ккал — цель (дефицит −{deficit_pct}%{goal_suffix})"
            )
        else:
            energy_line = (
                f"🏃 {today_active_r} ккал — активность сегодня\n"
                f"🎯 {target_cal} ккал — цель (дефицит −{deficit_pct}%{goal_suffix})"
            )

        # --- Calorie bar ---
        cal_bar, cal_pct = make_block_bar(totals.calories, target_cal)
        cal_remaining = target_cal - round(totals.calories)
        data_incomplete = stats.get("data_incomplete", False)
        if data_incomplete:
            # Битый Garmin-день: цель оценочная, вердикт «перебор» не выносим.
            cal_tail = "итог оценочный ⚠️"
        elif cal_remaining < 0:
            cal_tail = f"перебор +{abs(cal_remaining)}"
        else:
            cal_tail = f"ост. {cal_remaining}"
        if show_bar:
            cal_line = f"{cal_bar} {round(totals.calories):.0f} / {target_cal} ккал · {cal_tail}"
        else:
            cal_line = f"{round(totals.calories):.0f} / {target_cal} ккал · {cal_tail}"

        # --- Macro bars ---
        p_bar, p_pct = make_block_bar(totals.protein, targets["protein"], invert=True)
        f_bar, f_pct = make_block_bar(totals.fats, targets["fats"])
        c_bar, c_pct = make_block_bar(totals.carbs, targets["carbs"])

        # --- Fiber (optional, norm 30g) ---
        fiber_val = getattr(totals, "fiber", 0) or 0
        fiber_line = ""
        if fiber_val > 0:
            fib_bar, fib_pct = make_block_bar(fiber_val, 30, invert=True)
            fiber_line = f"🌿 {fib_bar} {fiber_val:.0f}/30г"

        # --- Response Construction ---
        if show_bar:
            macro_lines = [
                f"Б {p_bar} {totals.protein:.0f}/{targets['protein']}г",
                f"Ж {f_bar} {totals.fats:.0f}/{targets['fats']}г",
                f"У {c_bar} {totals.carbs:.0f}/{targets['carbs']}г",
            ]
        else:
            macro_lines = [
                f"Б {totals.protein:.0f}/{targets['protein']}г",
                f"Ж {totals.fats:.0f}/{targets['fats']}г",
                f"У {totals.carbs:.0f}/{targets['carbs']}г",
            ]

        response_parts = [
            f"📅 <b>Итоги дня {today_formatted}</b>",
            "",
            energy_line,
            "",
            cal_line,
            "",
            *macro_lines,
        ]
        if fiber_line:
            response_parts.append(fiber_line)
        if plans_text:
            response_parts.append(plans_text)

        # Weight and supplements
        response_parts.append("")
        if weight_text:
            response_parts.append(weight_text)
        response_parts.append(supplements_text)

        # Data quality warning — битый Garmin-день, вердикт не выносим
        if data_incomplete:
            response_parts.append(
                "\n⚠️ <i>Garmin-данные за этот день неполные (частичный синк) — "
                "расход неизвестен, итог по калориям оценочный.</i>"
            )

        # Feasibility Warning
        feasibility_warning = check_feasibility(remaining["calories"], remaining["protein"])
        if feasibility_warning:
            response_parts.append(f"\n⚠️ <i>{feasibility_warning}</i>")

        await message.answer("\n".join(response_parts))

    except Exception as e:
        logger.error(f"Error in /day: {e}", exc_info=True)
        import html

        await message.answer(f"❌ Ошибка при получении статистики: {html.escape(str(e))}")
    finally:
        if db:
            db.close()


@router.message(Command("vitamins"))
async def cmd_vitamins(message: Message, user_id: int):
    """Чек-лист приема витаминов и добавок"""
    try:
        # Create per-user supplement service
        from core.health.supplements import SupplementService

        user_supplement_service = SupplementService(user_id=user_id)
        status = user_supplement_service.get_detailed_schedule()
        await message.answer(status)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("week"))
async def cmd_week(message: Message, user_id: int):
    """Анализ недели"""
    try:
        # Получаем данные
        weekly_data = analyze_weekly_nutrition(user_id=user_id)
        totals = weekly_data.get("totals", {})
        days_count = weekly_data.get("days_with_data", 0)

        if days_count == 0:
            await message.answer("📊 Нет данных за последние 7 дней.")
            return

        # Средние значения потребления
        avg_consumed = totals.get("calories", 0) / days_count
        avg_prot = totals.get("protein", 0) / days_count
        avg_fats = totals.get("fats", 0) / days_count
        avg_carbs = totals.get("carbs", 0) / days_count
        avg_fiber = totals.get("fiber", 0) / days_count

        # TDEE (расход) и дефицит
        avg_tdee = totals.get("avg_tdee", 0)
        avg_active_cal = totals.get("avg_active_cal", 0)
        avg_bmr = totals.get("avg_bmr", 1700)

        # Расчет дефицита
        deficit = avg_tdee - avg_consumed
        deficit_pct = (deficit / avg_tdee * 100) if avg_tdee > 0 else 0

        # Цель с дефицитом 15%
        target_cal = avg_tdee * 0.85

        # Эмодзи для оценки дефицита
        if deficit_pct < 10:
            deficit_emoji = "⚠️"  # Слишком мало
        elif 10 <= deficit_pct <= 20:
            deficit_emoji = "✅"  # Отлично
        elif 20 < deficit_pct <= 30:
            deficit_emoji = "👍"  # Хорошо
        else:
            deficit_emoji = "⚠️"  # Слишком много

        # Целевые значения БЖУ
        target_protein = 150  # Целевое значение

        recommendations = weekly_data.get("recommendations", [])

        # Заголовок (показываем дни только если не все 7)
        if days_count < 7:
            title = f"📊 Анализ за 7 дней (дней с данными: {days_count})"
        else:
            title = "📊 Анализ за 7 дней"

        response = [
            title,
            "",
            "📥 Среднее потребление:",
            f"• Калории: {avg_consumed:.0f} ккал",
            f"• Белки: {avg_prot:.0f} г (Цель: > {target_protein}г)",
            f"• Жиры: {avg_fats:.0f} г",
            f"• Углеводы: {avg_carbs:.0f} г",
            f"• Клетчатка: {avg_fiber:.0f} г (Норма: > 30г)",
            "",
            f"🔥 Расход {avg_tdee:.0f} = {avg_bmr:.0f}😴 + {avg_active_cal:.0f}🏃",
            f"🎯 Цель {target_cal:.0f} (-15%) : {deficit:.0f} ({deficit_pct:.1f}%) {deficit_emoji}",
            "",
            "🔍 Анализ питания:",
        ]

        if recommendations:
            for rec in recommendations:
                response.append(f"• {rec}")
        else:
            response.append("• Ваш рацион выглядит сбалансированным! Продолжайте в том же духе.")

        await message.answer("\n".join(response))

    except Exception as e:
        await message.answer(f"❌ Ошибка анализа недели: {e}")


@router.message(Command("cache_stats"))
async def cmd_cache_stats(message: Message):
    """Показывает статистику Image Cache"""
    try:
        from infrastructure.cache.image_cache import get_image_cache

        cache = get_image_cache()
        stats = cache.stats()

        total = stats["total"]
        valid = stats["valid"]
        expired = stats["expired"]

        if total == 0:
            await message.answer(
                "📊 <b>Статистика кэша изображений</b>\n\nКэш пустой. Отправьте фото для создания первой записи."
            )
        else:
            hit_rate = (valid / total * 100) if total > 0 else 0
            await message.answer(
                "📊 <b>Статистика кэша изображений</b>\n\n"
                f"• Всего записей: {total}\n"
                f"• Активных: {valid}\n"
                f"• Просрочено: {expired}\n"
                f"• Hit Rate: ~{hit_rate:.0f}%\n\n"
                f"💰 Экономия: ~${valid * 0.01:.2f}\n"
                f"📅 TTL: 7 дней"
            )
    except ImportError:
        await message.answer("❌ Кэш не настроен")


@router.message(Command("cache_clear"))
async def cmd_cache_clear(message: Message):
    """Очищает Image Cache"""
    try:
        from infrastructure.cache.image_cache import get_image_cache

        cache = get_image_cache()

        stats_before = cache.stats()
        cache.clear()

        await message.answer(
            "🗑 <b>Кэш очищен</b>\n\n"
            f"Удалено записей: {stats_before['total']}\n"
            f"Освобождено места: ~{stats_before['total'] * 10}KB"
        )
    except ImportError:
        await message.answer("❌ Кэш не настроен")


@router.message(Command("agent_reset"))
async def cmd_agent_reset(message: Message, user_id: int):
    """/agent_reset — сбросить недавнюю историю AI-ассистента (за 24ч).

    Лечит «токсичную» историю: когда бот когда-то ответил «нет данных» и
    повторяет это даже после загрузки данных. Старше 24ч НЕ трогаем —
    это долговременная память (контекст истории здоровья).

    /agent_reset          — показать сколько сообщений будет удалено
    /agent_reset confirm  — удалить
    """
    from sqlalchemy import text as _sql

    from database import SessionLocal

    args = message.text.split()[1:] if message.text else []
    do_confirm = bool(args) and args[0].lower() in ("confirm", "да", "yes")

    db = SessionLocal()
    try:
        # RLS-правило проекта: всегда WHERE user_id=<свой>.
        cnt = db.execute(
            _sql(
                "SELECT COUNT(*) FROM agent_conversations "
                "WHERE user_id = :uid AND created_at > NOW() - INTERVAL '24 hours'"
            ),
            {"uid": user_id},
        ).scalar()

        if not cnt:
            await message.answer("🤖 За последние 24 часа истории нет — сбрасывать нечего.")
            return

        if not do_confirm:
            await message.answer(
                f"🤖 В истории AI-ассистента за 24 часа: <b>{cnt}</b> сообщений.\n\n"
                "Сброс уберёт недавний контекст (полезно, если бот «застрял» на старом "
                "ответе). История здоровья старше 24ч сохранится.\n\n"
                "Подтвердить: <code>/agent_reset confirm</code>",
                parse_mode="HTML",
            )
            return

        deleted = db.execute(
            _sql("DELETE FROM agent_conversations WHERE user_id = :uid AND created_at > NOW() - INTERVAL '24 hours'"),
            {"uid": user_id},
        ).rowcount
        db.commit()
        await message.answer(
            f"🗑 Контекст AI-ассистента сброшен: удалено {deleted} сообщений за 24ч.\n"
            "Следующий вопрос обработаю с чистого листа (данные и история здоровья на месте)."
        )
    except Exception as e:
        db.rollback()
        await message.answer(f"❌ Ошибка сброса: {e}")
    finally:
        db.close()


# Alias for old users / comfort
@router.message(Command("status"))
async def cmd_status_alias(message: Message):
    await cmd_day(message)


import re


@router.message(Command("setup"))
async def cmd_setup(message: Message, user_id: int):
    """
    Настройка калорий для пользователей без Garmin.
    Примеры: /setup BMR 1400 активные 250
             /setup BMR 1400, активные 250, вес 60
    """
    text = (message.text or "").replace("/setup", "").strip()
    if not text:
        await message.answer(
            "⚙️ <b>Настройка целей (для пользователей без Garmin)</b>\n\n"
            "Укажи BMR и средние активные калории из Apple Health (Здоровье → Энергия):\n\n"
            "<code>/setup BMR 1400, активные 250</code>\n"
            "<code>/setup BMR 1400 активные 250 вес 60</code>\n\n"
            "• BMR — базовая энергия (ккал/день)\n"
            "• Активные — среднее сжигание от движения\n"
            "• Вес — для расчёта белков (опционально)",
            parse_mode="HTML",
        )
        return
    bmr = None
    active = None
    weight = None
    for m in re.finditer(r"(?:bmr|базовы[йе])\s*[=:]?\s*(\d+)", text, re.I):
        bmr = float(m.group(1))
        break
    if not bmr:
        for m in re.finditer(r"(\d+)\s*(?:ккал)?\s*(?:bmr|базовы)", text, re.I):
            bmr = float(m.group(1))
            break
    for m in re.finditer(r"(?:активн[ые]?|active)\s*[=:]?\s*(\d+)", text, re.I):
        active = float(m.group(1))
        break
    if not active:
        for m in re.finditer(r"(\d+)\s*(?:ккал)?\s*(?:активн|active)", text, re.I):
            candidate = float(m.group(1))
            if bmr is None or candidate != bmr:  # не подставлять BMR за активные
                active = candidate
                break
    for m in re.finditer(r"(?:вес|weight)\s*[=:]?\s*(\d+(?:[.,]\d+)?)", text, re.I):
        weight = float(m.group(1).replace(",", "."))
        break
    if not bmr and not active and not weight:
        await message.answer("❌ Не удалось распознать BMR, активные или вес. Пример: /setup BMR 1400 активные 250")
        return
    from database import SessionLocal
    from database.crud import update_user_calorie_settings

    db = SessionLocal()
    try:
        update_user_calorie_settings(db, user_id, bmr=bmr, avg_active_calories=active, target_weight_kg=weight)
        parts = ["✅ Настройки сохранены:"]
        if bmr:
            parts.append(f"• BMR: {bmr:.0f} ккал")
        if active is not None:
            parts.append(f"• Активные: {active:.0f} ккал")
        if weight:
            parts.append(f"• Целевой вес: {weight:.1f} кг")
        parts.append(f"\nTDEE ≈ {((bmr or 1400) + (active or 0)):.0f} ккал. Цели в /day будут пересчитаны.")
        await message.answer("\n".join(parts))
    finally:
        db.close()


@router.message(Command("activity"))
async def cmd_activity(message: Message, user_id: int):
    """Логирование активных калорий за сегодня: /activity 300"""
    text = (message.text or "").replace("/activity", "").strip()
    try:
        cal = int(text) if text else 0
    except ValueError:
        cal = 0
    if cal <= 0 or cal > 3000:
        await message.answer("Использование: /activity <число> — активные калории за сегодня.\nПример: /activity 300")
        return
    from database import SessionLocal
    from database.crud import create_or_update_activity

    db = SessionLocal()
    try:
        today = datetime.now(get_user_tz(user_id)).date()
        create_or_update_activity(db, user_id, today, active_calories=float(cal), source="manual")
        await message.answer(f"✅ Активность сегодня: {cal} ккал сохранена.")
    finally:
        db.close()


@router.message(Command("burn"))
async def cmd_burn(message: Message, user_id: int):
    """Alias для /activity: /burn 300"""
    text = (message.text or "").replace("/burn", "").strip()
    try:
        cal = int(text) if text else 0
    except ValueError:
        cal = 0
    if cal <= 0 or cal > 3000:
        await message.answer("Использование: /burn <число> — активные калории за сегодня.")
        return
    from database import SessionLocal
    from database.crud import create_or_update_activity

    db = SessionLocal()
    try:
        create_or_update_activity(
            db,
            user_id,
            datetime.now(get_user_tz(user_id)).date(),
            active_calories=float(cal),
            source="manual",
        )
        await message.answer(f"✅ Активность сегодня: {cal} ккал сохранена.")
    finally:
        db.close()


@router.message(Command("targets"))
async def cmd_targets(message: Message):
    """Redirect to /setup"""
    await message.answer("Используй /setup для настройки BMR и активных калорий.")


# ── Admin commands ────────────────────────────────────────────────────────────


@router.message(Command("block"))
async def cmd_block(message: Message, user_id: int):
    """/block <telegram_id> — заблокировать пользователя (только admin)"""
    if not is_admin(user_id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /block <telegram_id>")
        return
    target_id = int(parts[1])
    if is_admin(target_id):
        await message.answer("❌ Нельзя заблокировать администратора.")
        return

    from database import SessionLocal
    from database.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == target_id).first()
        if not user:
            await message.answer(f"❌ Пользователь {target_id} не найден в БД.")
            return
        user.is_active = False
        db.commit()
        name = f"@{user.username}" if user.username else user.first_name or str(target_id)
        await message.answer(f"🚫 Пользователь {name} ({target_id}) заблокирован.")
    finally:
        db.close()


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, user_id: int):
    """/unblock <telegram_id> — разблокировать пользователя (только admin)"""
    if not is_admin(user_id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /unblock <telegram_id>")
        return
    target_id = int(parts[1])

    from database import SessionLocal
    from database.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == target_id).first()
        if not user:
            await message.answer(f"❌ Пользователь {target_id} не найден в БД.")
            return
        user.is_active = True
        db.commit()
        name = f"@{user.username}" if user.username else user.first_name or str(target_id)
        await message.answer(f"✅ Пользователь {name} ({target_id}) разблокирован.")
    finally:
        db.close()


@router.message(Command("users"))
async def cmd_users(message: Message, user_id: int):
    """/users — список всех пользователей (только admin)"""
    if not is_admin(user_id):
        return

    from database import SessionLocal
    from database.models import User

    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.registered_at.desc()).all()
    finally:
        db.close()

    if not users:
        await message.answer("Нет зарегистрированных пользователей.")
        return

    lines = [f"👥 <b>Пользователи ({len(users)}):</b>\n"]
    for u in users:
        status = "✅" if u.is_active else "🚫"
        name = f"@{u.username}" if u.username else u.first_name or "—"
        reg = u.registered_at.strftime("%d.%m.%Y") if u.registered_at else "?"
        lines.append(f"{status} <code>{u.telegram_id}</code> {name} · с {reg}")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("share"))
async def cmd_share(message: Message, user_id: int):
    """/share — создаёт/показывает секретную ссылку на персональный дашборд.

    /share        — показать/создать ссылку
    /share reset  — пересоздать ссылку (старая перестаёт работать)
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from database import SessionLocal
    from database.crud import generate_share_token, reset_share_token

    args = message.text.split()[1:] if message.text else []
    do_reset = bool(args) and args[0].lower() == "reset"

    db = SessionLocal()
    try:
        if do_reset:
            token = reset_share_token(db, user_id)
            action_text = "🔁 Ссылка пересоздана — старая больше не работает."
        else:
            token = generate_share_token(db, user_id)
            action_text = "📊 Твой персональный дашборд здоровья:"
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    finally:
        db.close()

    url = f"{public_base_url()}/mc/{token}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть дашборд", url=url)]])

    await message.answer(
        f"{action_text}\n\n"
        f"<code>{url}</code>\n\n"
        "Кидай ссылку друзьям — дашборд обновляется автоматически при каждом открытии.\n"
        "Хочешь сменить ссылку (старая перестанет работать)? Напиши /share reset",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.message(Command("report"))
async def cmd_report(message: Message, user_id: int):
    """/report — сгенерировать/обновить снимок дашборда (постоянная ссылка для шаринга)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from database import SessionLocal

    await message.answer("⏳ Готовлю снимок дашборда…")

    db = SessionLocal()
    try:
        from services.report_generator import generate_and_save_report

        token, diff = generate_and_save_report(db, user_id)
    except Exception as e:
        await message.answer(f"❌ Не удалось сгенерировать отчёт: {e}")
        return
    finally:
        db.close()

    url = f"{public_base_url()}/r/{token}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Открыть снимок", url=url)]])

    if diff:
        intro = f"✅ Снимок дашборда обновлён ({diff})."
    else:
        intro = "✅ Снимок дашборда готов."

    await message.answer(
        f"{intro}\n\n"
        f"<code>{url}</code>\n\n"
        "Ссылка постоянная — при повторном /report URL не меняется, данные обновляются.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


_DOCTOR_REPORT_LANG_ALIASES = {
    "en": "en",
    "eng": "en",
    "english": "en",
    "английский": "en",
    "англ": "en",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "русский": "ru",
}


def parse_doctor_report_lang(text: str | None, tg_language_code: str | None) -> str:
    """Из текста '/doctor_report <lang>' + language_code → 'ru'|'en'.

    Явный алиас перебивает; неизвестный/пустой аргумент → резолв по language_code.
    """
    from services.report_i18n import resolve_report_language

    explicit = None
    parts = (text or "").split()
    if len(parts) >= 2:
        explicit = _DOCTOR_REPORT_LANG_ALIASES.get(parts[1].strip().lower())
    return resolve_report_language(explicit, tg_language_code)


@router.message(Command("doctor_report"))
async def cmd_doctor_report(message: Message, user_id: int):
    """/doctor_report [ru|en] — PDF-отчёт для врача, приходит документом в чат."""
    from database import SessionLocal
    from services.doctor_report import send_doctor_report_to_chat
    from services.report_i18n import CHROME

    lang_code = message.from_user.language_code if message.from_user else None
    lang = parse_doctor_report_lang(message.text, lang_code)
    chrome = CHROME[lang]

    await message.answer(chrome["status_preparing"])

    db = SessionLocal()
    try:
        result = send_doctor_report_to_chat(db, user_id, lang=lang)
    finally:
        db.close()

    if result.get("sent"):
        await message.answer(chrome["status_done"])
    else:
        await message.answer(chrome["status_failed"])


@router.message(Command("health_token"))
async def cmd_health_token(message: Message, user_id: int):
    """/health_token — персональный ключ для подключения Apple Health.

    /health_token        — показать/создать ключ
    /health_token rotate — перевыпустить (старый ключ перестаёт работать)
    """
    from database import SessionLocal
    from database.crud import get_or_create_health_token, reset_health_token
    from handlers.apple_health_connect import connect_intro_text, connect_keyboard_aiogram

    args = message.text.split()[1:] if message.text else []
    do_reset = bool(args) and args[0].lower() in ("rotate", "reset")

    db = SessionLocal()
    try:
        if do_reset:
            token = reset_health_token(db, user_id)
            prefix = "♻️ Ключ перевыпущен — старый больше не работает.\n\n"
        else:
            token = get_or_create_health_token(db, user_id)
            prefix = ""
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    finally:
        db.close()

    await message.answer(
        prefix + connect_intro_text(token),
        parse_mode="HTML",
        reply_markup=connect_keyboard_aiogram(),
        disable_web_page_preview=True,
    )


@router.message(Command("whoop"))
async def cmd_whoop(message: Message, user_id: int):
    """/whoop — подключить носимое устройство Whoop (сон, восстановление, пульс, HRV)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    import os

    if not os.environ.get("WHOOP_CLIENT_ID"):
        await message.answer(
            "⏳ Интеграция Whoop ещё настраивается на стороне сервера. "
            "Скоро здесь появится кнопка подключения — я напишу, когда будет готово."
        )
        return

    from webhook.whoop_oauth import make_connect_link

    url = make_connect_link(user_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Подключить Whoop", url=url)]])
    await message.answer(
        "⌚️ <b>Подключение Whoop</b>\n\n"
        "Нажми кнопку ниже → авторизуйся в Whoop → разреши доступ. "
        "После этого я буду автоматически получать твой сон, восстановление (recovery), "
        "пульс покоя, HRV и нагрузку (strain).\n\n"
        "Это безопасно: я получаю только чтение данных, пароль Whoop я не вижу.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.message(Command("meal_reminders"))
async def cmd_meal_reminders(message: Message, user_id: int):
    """/meal_reminders — напоминания логировать еду.

    /meal_reminders            — показать текущие настройки
    /meal_reminders on         — включить (дефолт: Завтрак 11:00, Обед 14:30, Ужин 22:00)
    /meal_reminders off        — выключить
    /meal_reminders times 11:00 14:30 22:00  — задать времена (Завтрак, Обед, Ужин)
    """
    from database import SessionLocal
    from database.crud import ensure_user_exists
    from database.models import UserSettings
    from core.reminders.meal_reminders import DEFAULT_MEAL_TIMES, normalize_times, parse_hhmm

    parts = (message.text or "").split()
    args = parts[1:]
    sub = args[0].lower() if args else "status"

    db = SessionLocal()
    try:
        ensure_user_exists(db, telegram_id=user_id)
        settings = db.get(UserSettings, user_id)
        if settings is None:
            settings = UserSettings(user_id=user_id)
            db.add(settings)

        if sub in ("on", "вкл"):
            settings.meal_reminders_enabled = True
            if not normalize_times(settings.meal_reminder_times or {}):
                settings.meal_reminder_times = dict(DEFAULT_MEAL_TIMES)
            db.commit()
            times = normalize_times(settings.meal_reminder_times or {})
            pretty = ", ".join(f"{k} {v}" for k, v in times.items())
            await message.answer(f"✅ Напоминания о еде включены.\nВремена: {pretty}")
            return

        if sub in ("off", "выкл"):
            settings.meal_reminders_enabled = False
            db.commit()
            await message.answer("🔕 Напоминания о еде выключены.")
            return

        if sub in ("times", "время"):
            vals = args[1:]
            if not vals:
                await message.answer("Укажите времена, например: /meal_reminders times 11:00 14:30 22:00")
                return
            labels = list(DEFAULT_MEAL_TIMES.keys())  # Завтрак, Обед, Ужин
            new_times: dict[str, str] = {}
            try:
                for i, v in enumerate(vals):
                    t = parse_hhmm(v)
                    label = labels[i] if i < len(labels) else f"Приём {i + 1}"
                    new_times[label] = f"{t.hour:02d}:{t.minute:02d}"
            except ValueError:
                await message.answer("❌ Время в формате HH:MM, например 11:00. Попробуйте снова.")
                return
            settings.meal_reminder_times = new_times
            settings.meal_reminders_enabled = True
            db.commit()
            pretty = ", ".join(f"{k} {v}" for k, v in new_times.items())
            await message.answer(f"✅ Времена напоминаний обновлены: {pretty}")
            return

        # status (по умолчанию)
        on = bool(settings.meal_reminders_enabled)
        times = normalize_times(settings.meal_reminder_times or {})
        if times:
            pretty = ", ".join(f"{k} {v}" for k, v in times.items())
        else:
            pretty = "не заданы"
        state = "включены ✅" if on else "выключены 🔕"
        await message.answer(
            f"🍽 Напоминания о еде: {state}\n"
            f"Времена: {pretty}\n\n"
            f"Команды: /meal_reminders on | off | times 11:00 14:30 22:00"
        )
    finally:
        db.close()
