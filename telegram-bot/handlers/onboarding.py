"""Onboarding wizard — 7-question health profile setup.

State machine stored in users.onboarding_step + users.onboarding_data (jsonb).
Steps: goal → sex → age → height → weight → activity → smoking → artifact → persona → done.
("artifact" is a transient pseudo-step: it renders the BMR/TDEE summary and
immediately advances to "persona", it is never persisted as onboarding_step.)
Курение спрашивается (влияет на PhenoAge/биовозраст) — последний квиз-вопрос.

LEGACY_STEP_MAP remaps users stuck on old steps (name/birth_date/chronic/
wearables) from the previous 10-step wizard onto the new flow.

On completion: computes BMR/TDEE/calorie goal, generates health_token, sends summary.

Public API:
- process_onboarding_message(payload) — wizard state machine entrypoint (called by router)
- handle_setup_command(payload) — /setup command for existing users to fill missing fields
- start_wizard(payload) — alias for process_onboarding_message (back-compat)
"""

import logging
import secrets
import uuid
from datetime import datetime
from typing import Optional

import httpx

from bot_token import resolve_bot_token
from database import SessionLocal
from database.models import User, UserSettings, Weight, log_event

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────

GOAL_MAP = {
    # label_match_substring: (display, calorie_goal_pct)
    "похудеть": ("Похудеть", -15),
    "удержать": ("Удержать форму", 0),
    "набрать": ("Набрать мышцы", 10),
    "долголетие": ("Долголетие/профилактика", 0),
}

ACTIVITY_MAP = {
    # label_match_substring: (db_value, mifflin_multiplier)
    "сидячий": ("sedentary", 1.2),
    "лёгкий": ("light", 1.375),
    "легкий": ("light", 1.375),
    "умеренный": ("moderate", 1.55),
    "высокий": ("high", 1.725),
}

SMOKING_MAP = {
    "никогда": "never",
    "бросил": "former",
    "курю": "current",
}


# ─── Telegram helpers ─────────────────────────────────────────────────────


async def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
    """Send a message via Telegram Bot API."""
    bot_token = resolve_bot_token()
    if not bot_token:
        logger.warning("No BOT_TOKEN — cannot send message")
        return
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        body["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=body,
                timeout=10.0,
            )
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")


def _kb(rows: list[list[str]], one_time: bool = True) -> dict:
    return {"keyboard": rows, "one_time_keyboard": one_time, "resize_keyboard": True}


KB_SEX = _kb([["М", "Ж"]])
KB_GOAL = _kb([["🔻 Похудеть", "⚖ Удержать форму"], ["💪 Набрать мышцы", "🛡 Долголетие/профилактика"]])
KB_ACTIVITY = _kb([["🪑 Сидячий", "🚶 Лёгкий 1-3/нед"], ["🏃 Умеренный 4-5/нед", "🏋 Высокий 6+/нед"]])
KB_SMOKING = _kb([["Никогда", "Бросил", "Курю"]])
KB_PERSONA = _kb(
    [
        ["🩺 Заботливый врач", "💪 Строгий тренер"],
        ["🔬 Дотошный профессор", "🧘 Спокойный наставник"],
        ["Пропустить"],
    ]
)

PROGRESS = {
    "goal": "1/7",
    "sex": "2/7",
    "age": "3/7",
    "height": "4/7",
    "weight": "5/7",
    "activity": "6/7",
    "smoking": "7/7",
}

# Users who were mid-wizard on the old 10-step flow (name/birth_date/…/smoking/
# chronic/wearables) get remapped onto the nearest equivalent new-flow step.
LEGACY_STEP_MAP = {
    "name": "goal",
    "birth_date": "age",
    "chronic": "artifact",
    "wearables": "artifact",
}

# Human-readable (RU) labels for step keys shown to users in /setup messages —
# raw keys like "goal, birth_date" must never leak into user-facing text.
STEP_LABELS = {
    "name": "имя",
    "birth_date": "возраст",
    "age": "возраст",
    "sex": "пол",
    "height": "рост",
    "weight": "вес",
    "goal": "цель",
    "activity": "активность",
    "smoking": "курение",
}


# ─── Public entry points ──────────────────────────────────────────────────


async def start_wizard(payload: dict) -> None:
    """Back-compat alias."""
    await process_onboarding_message(payload)


async def process_onboarding_message(payload: dict) -> None:
    """Process one message in the onboarding state machine."""
    msg = payload.get("message") or payload.get("edited_message") or {}
    from_id = msg.get("from", {}).get("id")
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()

    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=from_id).first()

        if not user:
            # deep-link: "/start <payload>" → track (b2c|b2b) + source-атрибуция
            payload_arg = ""
            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                payload_arg = parts[1].strip() if len(parts) > 1 else ""
            is_coach = payload_arg.startswith("coach")
            track = "b2b" if is_coach else "b2c"
            source = "" if is_coach else payload_arg

            if track == "b2b":
                from handlers.onboarding_coach import start_coach_onboarding

                await start_coach_onboarding(payload)
                return

            user = User(
                telegram_id=from_id,
                username=msg.get("from", {}).get("username"),
                first_name=(msg.get("from", {}) or {}).get("first_name") or "",
                cohort="external",
                pack_name="generic",
                onboarding_step="goal",
                onboarding_data={"track": track, "source": source},
                is_active=True,
            )
            db.add(user)
            db.commit()
            log_event(db, user_id=from_id, event="onboarding_started", track=track, source=source or None)
            db.commit()
            await _send_greeting_and_first_question(chat_id, user)
            return

        await _run_step(user, text, chat_id, db)
    finally:
        db.close()


async def handle_setup_command(payload: dict) -> bool:
    """Handle /setup command for users with onboarding_step='done'.

    Detects which required fields are missing and resumes the wizard from
    the first missing step. Returns True if /setup was handled (wizard
    started), False if everything is already filled.
    """
    msg = payload.get("message") or {}
    from_id = msg.get("from", {}).get("id")
    chat_id = msg.get("chat", {}).get("id")

    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=from_id).first()
        if not user:
            await send_message(chat_id, "Сначала напиши /start — пройдём знакомство.")
            return True

        missing = _detect_missing_steps(user, db)
        if not missing:
            await send_message(
                chat_id,
                "✅ Профиль уже заполнен полностью. Если хочешь поменять отдельные значения — напиши, какое поле.",
            )
            return True

        # Resume from first missing step
        user.onboarding_step = missing[0]
        db.commit()
        await send_message(
            chat_id,
            f"Догоним пропущенные поля ({len(missing)} шт.): {', '.join(STEP_LABELS.get(m, m) for m in missing)}.",
        )
        # Trigger the question for the first missing step with empty input
        await _run_step(user, "", chat_id, db, prompt_only=True)
        return True
    finally:
        db.close()


# ─── State machine ────────────────────────────────────────────────────────


def _compute_goal(data: dict, user) -> dict:
    """BMR/TDEE/goal_kcal. Возвращает {} если веса нет."""
    w = data.get("weight_kg")
    if not w:
        return {}
    h = data.get("height_cm") or user.height_cm or 170
    age = data.get("age") or 30
    is_male = (data.get("sex") or user.sex or "male").lower() in ("male", "m")
    mult = data.get("activity_multiplier", 1.375)
    goal_pct = data.get("goal_pct", 0)
    bmr = 10 * w + 6.25 * h - 5 * age + (5 if is_male else -161)
    tdee = bmr * mult
    goal_kcal = round(tdee * (1 + goal_pct / 100))
    return {"bmr": round(bmr), "tdee": round(tdee), "goal_kcal": goal_kcal}


def _weight_forecast(goal_pct: int, tdee: float) -> dict:
    """Простая проекция: суточный дефицит ккал → кг/нед → дата −4 кг.
    7700 ккал ≈ 1 кг жира. Нулевой/положительный дефицит для похудения → 0."""
    daily_deficit = -tdee * (goal_pct / 100) if goal_pct < 0 else 0
    kg_per_week = round(daily_deficit * 7 / 7700, 2) if daily_deficit > 0 else 0
    target_date = None
    if kg_per_week > 0:
        from datetime import date as _date, timedelta

        weeks = 4 / kg_per_week
        target_date = (_date.today() + timedelta(weeks=weeks)).strftime("%d.%m")
    return {"kg_per_week": kg_per_week, "target_date": target_date}


async def _show_artifact(user, data: dict, chat_id: int, db) -> None:
    goal = _compute_goal(data, user)
    goal_label = data.get("goal", "Удержать форму")
    if goal:
        fc = _weight_forecast(data.get("goal_pct", 0), goal["tdee"])
        line = (
            f"Твоя цель: <b>{goal['goal_kcal']} ккал/день</b> ({goal_label}).\nBMR {goal['bmr']} · TDEE {goal['tdee']}."
        )
        if fc["kg_per_week"]:
            line += f"\nПри таком темпе ≈ −{fc['kg_per_week']} кг/нед → −4 кг к {fc['target_date']}."
        try:
            user.bmr = float(goal["bmr"])
        except Exception:
            pass
        data["weight_forecast"] = fc
    else:
        line = "Вес пока не указал — как взвесишься, напиши «вес 78», и я посчитаю норму калорий."
    log_event(
        db,
        user_id=user.telegram_id,
        event="goal_computed",
        track=data.get("track"),
        source=data.get("source") or None,
        meta={"goal_kcal": goal.get("goal_kcal")},
    )
    user.onboarding_step = "persona"
    user.onboarding_data = data
    db.commit()
    await send_message(chat_id, f"Готово, {user.first_name}! 🎯\n{line}")
    await send_message(chat_id, "И последнее — каким тоном мне с тобой общаться?", reply_markup=KB_PERSONA)


async def _send_greeting_and_first_question(chat_id: int, user: "User") -> None:
    name = user.first_name or "друг"
    await send_message(
        chat_id,
        f"👋 Привет, {name}! Я Botkin — помощник по здоровью и питанию.\n"
        "Со мной не надо учить команды — можно просто писать или говорить "
        "словами (покажу в конце). Настроим твою цель за 7 вопросов.\n\n"
        "<b>Цель · 1/7</b> Главная цель?",
        reply_markup=KB_GOAL,
    )


async def _run_step(user: User, text: str, chat_id: int, db, prompt_only: bool = False) -> None:
    """Execute one step of the wizard. If prompt_only=True, only re-prompts the question."""
    step = user.onboarding_step or "goal"
    if step in LEGACY_STEP_MAP:
        step = LEGACY_STEP_MAP[step]
        user.onboarding_step = step
        db.commit()
        if step == "artifact":
            await _show_artifact(user, dict(user.onboarding_data or {}), chat_id, db)
            return
    data = dict(user.onboarding_data or {})

    # ── Цель · 1/7 ───────────────────────────────────────────────
    if step == "goal":
        if prompt_only:
            await send_message(chat_id, "<b>Цель · 1/7</b> Главная цель?", reply_markup=KB_GOAL)
            return
        t = text.lower()
        match = next(((label, pct) for key, (label, pct) in GOAL_MAP.items() if key in t), None)
        if not match:
            await send_message(chat_id, "Выбери одну из 4 кнопок", reply_markup=KB_GOAL)
            return
        goal_label, goal_pct = match
        data["goal"] = goal_label
        data["goal_pct"] = goal_pct
        _ensure_user_settings(db, user.telegram_id, calorie_goal_pct=goal_pct)
        user.onboarding_step = "sex"
        user.onboarding_data = data
        db.commit()
        await send_message(chat_id, "<b>Пол · 2/7</b> Пол?", reply_markup=KB_SEX)
        return

    # ── Пол · 2/7 ────────────────────────────────────────────────
    if step == "sex":
        if prompt_only:
            await send_message(chat_id, "<b>Пол · 2/7</b> Пол?", reply_markup=KB_SEX)
            return
        t = text.upper().strip()
        if t.startswith("М") or t.startswith("M"):
            sex = "male"
        elif t.startswith("Ж") or t.startswith("F"):
            sex = "female"
        else:
            await send_message(chat_id, "Нажми кнопку М или Ж", reply_markup=KB_SEX)
            return
        user.sex = sex
        data["sex"] = sex
        user.onboarding_step = "age"
        user.onboarding_data = data
        db.commit()
        await send_message(chat_id, "<b>Возраст · 3/7</b> Сколько тебе лет? (число)")
        return

    # ── Возраст · 3/7 ────────────────────────────────────────────
    if step == "age":
        if prompt_only or not text:
            await send_message(chat_id, "<b>Возраст · 3/7</b> Сколько тебе лет? (число)")
            return
        try:
            age = int(text)
            if not (10 <= age <= 100):
                raise ValueError
        except ValueError:
            await send_message(chat_id, "Введи число 10–100")
            return
        data["age"] = age
        from datetime import date as _date

        user.birth_date = _date(_date.today().year - age, 1, 1)  # приблизительно, ±1 год
        user.onboarding_step = "height"
        user.onboarding_data = data
        db.commit()
        await send_message(chat_id, "<b>Рост · 4/7</b> Рост в см? (например, 178)")
        return

    # ── Рост · 4/7 ───────────────────────────────────────────────
    if step == "height":
        if prompt_only or not text:
            await send_message(chat_id, "<b>Рост · 4/7</b> Рост в см? (например, 178)")
            return
        try:
            h = int(text)
            if not (100 <= h <= 230):
                raise ValueError
        except ValueError:
            await send_message(chat_id, "Введи число 100–230 (см)")
            return
        user.height_cm = h
        data["height_cm"] = h
        user.onboarding_step = "weight"
        user.onboarding_data = data
        db.commit()
        await send_message(
            chat_id,
            "<b>Вес · 5/7</b> Текущий вес в кг? (если не знаешь — напиши «позже»)",
        )
        return

    # ── Вес · 5/7 ────────────────────────────────────────────────
    if step == "weight":
        if prompt_only or not text:
            await send_message(chat_id, "<b>Вес · 5/7</b> Текущий вес в кг? (или «позже»)")
            return
        if text.lower() in ("позже", "не знаю", "later", "skip", "-", "пропустить"):
            w = None
        else:
            try:
                w = float(text.replace(",", "."))
                if not (30 <= w <= 300):
                    raise ValueError
            except ValueError:
                await send_message(chat_id, "Введи число 30–300 (кг) или напиши «позже»")
                return
        data["weight_kg"] = w
        if w is not None:
            # Save as a Weight measurement point
            try:
                from datetime import timezone as _tz

                weight_entry = Weight(
                    user_id=user.telegram_id,
                    measured_at=datetime.now(_tz.utc),
                    weight=w,
                    source="onboarding",
                )
                db.add(weight_entry)
            except Exception as e:
                logger.warning(f"Could not save Weight for {user.telegram_id}: {e}")
        user.onboarding_step = "activity"
        user.onboarding_data = data
        db.commit()
        await send_message(chat_id, "<b>Активность · 6/7</b> Уровень активности?", reply_markup=KB_ACTIVITY)
        return

    # ── Активность · 6/7 → артефакт → персона ─────────────────────
    if step == "activity":
        if prompt_only:
            await send_message(chat_id, "<b>Активность · 6/7</b> Уровень активности?", reply_markup=KB_ACTIVITY)
            return
        t = text.lower()
        match = next(((lev, mult) for key, (lev, mult) in ACTIVITY_MAP.items() if key in t), None)
        if not match:
            await send_message(chat_id, "Выбери одну из 4 кнопок", reply_markup=KB_ACTIVITY)
            return
        level, mult = match
        data["activity_level"] = level
        data["activity_multiplier"] = mult
        _ensure_user_settings(db, user.telegram_id, activity_level=level)
        user.onboarding_step = "smoking"
        user.onboarding_data = data
        db.commit()
        await send_message(chat_id, "<b>Курение · 7/7</b> Куришь?", reply_markup=KB_SMOKING)
        return

    # ── Курение · 7/7 → артефакт → персона (влияет на PhenoAge) ────
    if step == "smoking":
        if prompt_only:
            await send_message(chat_id, "<b>Курение · 7/7</b> Куришь?", reply_markup=KB_SMOKING)
            return
        t = text.lower().strip()
        sm = next((v for k, v in SMOKING_MAP.items() if k in t), None)
        if not sm:
            await send_message(chat_id, "Выбери одну кнопку", reply_markup=KB_SMOKING)
            return
        user.smoking_status = sm
        data["smoking"] = sm
        log_event(
            db,
            user_id=user.telegram_id,
            event="quiz_completed",
            track=data.get("track"),
            source=data.get("source") or None,
        )
        await _show_artifact(user, data, chat_id, db)  # ставит step="persona", логирует E4
        return

    # ── Артефакт (self-heal, если onboarding_step застрял на "artifact") ──
    if step == "artifact":
        await _show_artifact(user, data, chat_id, db)
        return

    # ── Персона (тон агента) → финал ──────────────────────────────
    if step == "persona":
        from core.personas import PERSONAS, DEFAULT_PERSONA

        t = text.strip().lower()
        if prompt_only:
            await send_message(chat_id, "Каким тоном мне с тобой общаться?", reply_markup=KB_PERSONA)
            return
        skipped = t in ("пропустить", "skip", "-")
        chosen = (
            DEFAULT_PERSONA
            if skipped
            else next(
                (
                    p.key
                    for p in PERSONAS.values()
                    if p.display.lower() == t or p.display.split(maxsplit=1)[-1].lower() in t
                ),
                None,
            )
        )
        if chosen is None:
            await send_message(chat_id, "Выбери одну из кнопок или «Пропустить»", reply_markup=KB_PERSONA)
            return
        data["persona"] = chosen
        user.onboarding_data = data
        log_event(
            db,
            user_id=user.telegram_id,
            event="persona_selected",
            track=data.get("track"),
            source=data.get("source") or None,
            meta={"persona": chosen, "skipped": skipped},
        )
        db.commit()
        await _finish_onboarding(user, db, chat_id)
        return

    # already done
    if step == "done":
        logger.info(f"User {user.telegram_id} already done; router shouldn't route here")
        return


# ─── Finalisation ─────────────────────────────────────────────────────────


async def _finish_onboarding(user, db, chat_id: int) -> None:
    """Финал онбординга: токены, step=done, first_food_pending, демо-приглашение.

    Калории уже посчитаны и показаны в артефакте (_show_artifact) — тут только
    выдаём токены и передаём эстафету в чат-first демо логирования еды.
    """
    from core.personas import get_persona

    data = dict(user.onboarding_data or {})
    if not user.health_token:
        user.health_token = f"hvt_{user.telegram_id}_{secrets.token_hex(16)}"
    if not user.share_token:
        user.share_token = str(uuid.uuid4()).replace("-", "")[:32]
    data["first_food_pending"] = True
    user.onboarding_step = "done"
    user.onboarding_data = data
    db.commit()
    persona = get_persona(data.get("persona"))
    await send_message(
        chat_id,
        f"Принято — буду в манере «{persona.display}».\n\n"
        "Теперь главное: просто напиши или сфоткай, что ел. "
        "Например «овсянка на молоке и кофе». Попробуй прямо сейчас 👇\n\n"
        "Чтобы советы были точнее (по желанию):\n"
        "📂 анализы /doc · ⌚ есть часы/тонометр — напиши какие, подскажу как подключить · 💊 добавки\n"
        "🩺 расскажи о хронических болячках и постоянных лекарствах — прямо в чат, "
        "буду учитывать в советах",
    )


# ─── Helpers ──────────────────────────────────────────────────────────────


def _ensure_user_settings(db, user_id: int, **fields) -> None:
    """Upsert user_settings row with given fields."""
    us = db.query(UserSettings).filter_by(user_id=user_id).first()
    if us is None:
        us = UserSettings(user_id=user_id, **fields)
        db.add(us)
    else:
        for k, v in fields.items():
            setattr(us, k, v)


def _detect_missing_steps(user: User, db) -> list[str]:
    """Return list of onboarding steps with missing data, in canonical order."""
    data = user.onboarding_data or {}
    missing = []

    # birth_date
    if not user.birth_date:
        missing.append("birth_date")
    # sex
    if not user.sex or user.sex == "":
        missing.append("sex")
    # height
    if not user.height_cm:
        missing.append("height")
    # weight — check if any Weight record exists
    weight_count = db.query(Weight).filter_by(user_id=user.telegram_id).count()
    if weight_count == 0 and "weight_kg" not in data:
        missing.append("weight")
    # goal — calorie_goal_pct must be in user_settings
    us = db.query(UserSettings).filter_by(user_id=user.telegram_id).first()
    if not data.get("goal"):
        missing.append("goal")
    # activity
    if not (us and us.activity_level):
        missing.append("activity")
    # smoking (нужно для PhenoAge/биовозраста)
    if not user.smoking_status:
        missing.append("smoking")

    return missing
