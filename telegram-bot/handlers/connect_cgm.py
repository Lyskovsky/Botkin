#!/usr/bin/env python3
"""Команда /connect_cgm — самоподключение CGM (Abbott FreeStyle Libre) через LibreLinkUp (#96, #381).

Два пути на выбор:

  A. «Пригласить наш аккаунт» (прежний, #96) — для EU:
     1. Бот даёт инструкцию пригласить follower dr@botkin.health.
     2. Фоновая задача опрашивает get_patients() (через сервисный follower-аккаунт) и ловит
        новый patient_id, которого ещё нет в cgm_connections → привязывает к telegram_id.
     3. Дальше 5-минутный/ночной импортёр (scripts/import/librelinkup.py) тянет глюкозу.

  B. «Свой follower-аккаунт» (#381) — для всех остальных регионов:
     приглашение follower'а в LibreLinkUp работает ТОЛЬКО внутри региона (ограничение
     Abbott), поэтому наш EU-аккаунт RU-пациента не видит в принципе. Раньше это
     лечилось правкой прод-.env админом — то есть передачей чужого пароля в переписку.
     Теперь пользователь вводит креды сам: бот удаляет сообщение с паролем, проверяет
     логин и только потом пишет в cgm_followers уже зашифрованным.

Атрибуция: один flow за раз (asyncio-lock, бот — однопроцессный aiogram). Уже привязанные
patient_id (из cgm_connections) исключаются из кандидатов, а запись идёт идемпотентно
(unique patient_id → IntegrityError = уже занят), чтобы чужой сенсор не переназначить.
"""

import asyncio
import logging
import re
import time

from aiogram import Router
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)
router = Router()

FOLLOWER_EMAIL = "dr@botkin.health"
POLL_INTERVAL = 30  # сек между опросами get_patients()
POLL_TIMEOUT = 600  # 10 мин ждём появления нового пациента

# Один flow подключения одновременно — иначе нельзя однозначно атрибутировать новый patient_id.
_connect_lock = asyncio.Lock()
# Держим ссылки на фоновые задачи: без этого CPython может собрать task GC до завершения.
_background_tasks: set[asyncio.Task] = set()


def detect_new_patient_ids(baseline: set[str], current: list[str]) -> list[str]:
    """Чистая функция: какие patient_id появились по сравнению с baseline (порядок сохраняется)."""
    return [pid for pid in current if pid not in baseline]


def _fetch_patient_ids() -> list[str]:
    """Синхронный сетевой вызов: id всех пациентов, видимых follower-аккаунтом.

    Через общий рантайм — переиспользует валидный токен бота, без свежего логина (#135).
    """
    from core.health.glucose_runtime import fetch_patient_ids

    return fetch_patient_ids()


def _mapped_patient_ids() -> set[str]:
    """patient_id, уже привязанные к кому-либо (из cgm_connections)."""
    from database import SessionLocal
    from database.models import CgmConnection

    db = SessionLocal()
    try:
        return {row.patient_id for row in db.query(CgmConnection.patient_id).all()}
    finally:
        db.close()


def _save_mapping(patient_id: str, telegram_id: int) -> bool:
    """Привязать patient_id к пользователю. False — если patient_id уже занят (idempotent)."""
    from sqlalchemy.exc import IntegrityError

    from database import SessionLocal
    from database.models import CgmConnection

    db = SessionLocal()
    try:
        db.add(CgmConnection(patient_id=patient_id, telegram_id=telegram_id))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()  # patient_id уже привязан (unique) — кто-то успел раньше
        return False
    finally:
        db.close()


def _mapping_owner(patient_id: str) -> int | None:
    """Кому принадлежит уже существующая привязка patient_id (None — никому).

    Нужен, чтобы отличить «этот сенсор уже подключён к ТВОЕМУ профилю» от
    «занят другим». Без этого пользователь, у которого привязка уже есть,
    получал пугающее «привязан к другому профилю» — так и вышло на первом
    живом прогоне 21.08.2026, когда patient_id был привязан вручную заранее.
    """
    from database import SessionLocal
    from database.models import CgmConnection

    db = SessionLocal()
    try:
        row = db.query(CgmConnection.telegram_id).filter(CgmConnection.patient_id == patient_id).first()
        return row[0] if row else None
    except Exception as e:
        logger.warning("connect_cgm: не смог определить владельца привязки %s: %s", patient_id, e)
        return None
    finally:
        db.close()


async def _connect_flow(message: Message, telegram_id: int) -> None:
    """Фоновая задача: ждём новый patient_id и привязываем его к пользователю."""
    async with _connect_lock:
        loop = asyncio.get_running_loop()
        try:
            # baseline = всё, что follower видит сейчас + всё уже привязанное в БД.
            # Так чужой/ранее-подключённый сенсор не попадёт в кандидаты.
            baseline = set(await asyncio.to_thread(_fetch_patient_ids))
            baseline |= await asyncio.to_thread(_mapped_patient_ids)
        except Exception as e:
            logger.error(f"connect_cgm: ошибка старта (get_patients): {e}")
            await message.answer("⚠️ Не удалось связаться с LibreLinkUp. Попробуй позже.")
            return

        deadline = loop.time() + POLL_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                current = await asyncio.to_thread(_fetch_patient_ids)
            except Exception as e:
                logger.warning(f"connect_cgm: опрос get_patients упал, повтор: {e}")
                continue
            new_ids = detect_new_patient_ids(baseline, current)
            if not new_ids:
                continue

            patient_id = new_ids[0]
            try:
                saved = await asyncio.to_thread(_save_mapping, patient_id, telegram_id)
            except Exception as e:
                logger.error(f"connect_cgm: не смог сохранить маппинг {patient_id}: {e}")
                await message.answer("⚠️ Поймал подключение, но не смог сохранить. Напиши /connect_cgm ещё раз.")
                return
            if not saved:
                # patient_id уже занят другим пользователем — не наш, продолжаем ждать.
                baseline.add(patient_id)
                continue

            logger.info(f"connect_cgm: привязал patient {patient_id} → user {telegram_id}")
            await message.answer(
                "✅ CGM подключён! Глюкоза начнёт поступать после прогрева сенсора (~1 час).\n"
                "Дальше данные обновляются автоматически."
            )
            return

        await message.answer(
            "⏳ Не увидел приглашение за 10 минут. Проверь, что пригласил "
            f"`{FOLLOWER_EMAIL}` в LibreLinkUp, и запусти /connect_cgm ещё раз.",
            parse_mode="Markdown",
        )


@router.message(Command("connect_cgm"))
async def cmd_connect_cgm(message: Message) -> None:
    """`/connect_cgm` — подключить непрерывный мониторинг глюкозы (CGM)."""
    await message.answer(CHOOSE_PATH_TEXT, reply_markup=_path_keyboard(), parse_mode="Markdown")


INSTRUCTIONS = (
    "🩸 *Подключение CGM (FreeStyle Libre 3)*\n\n"
    "1. Открой приложение *FreeStyle Libre 3* → ☰ → *Connected Apps* → *LibreLinkUp*\n"
    "2. Нажми *Invite Follower* и введи email:\n"
    f"`{FOLLOWER_EMAIL}`\n\n"
    "Я подожду до 10 минут и сам поймаю подключение — как приглашение пройдёт, "
    "напишу сюда. Глюкоза начнёт поступать после прогрева сенсора (~1 час)."
)


# ── Путь B: свой follower-аккаунт (#381) ──────────────────────────────────────


class FollowerSetup(StatesGroup):
    """Ввод кред своего follower-аккаунта. Пароль в состоянии НЕ храним —
    он обрабатывается целиком внутри step_password и наружу не выходит."""

    email = State()
    password = State()


class CgmPathCallback(CallbackData, prefix="cgmpath"):
    kind: str  # "invite" — наш follower (EU); "own" — свой аккаунт


class CgmRegionCallback(CallbackData, prefix="cgmreg"):
    region: str


class CgmBindCallback(CallbackData, prefix="cgmbind"):
    patient_id: str


# Регионы, которые понимает pylibrelinkup; RU/EU наверх — самые частые у нас.
_REGION_ORDER = ("RU", "EU", "EU2", "US", "DE", "FR", "AE", "AP", "AU", "CA", "JP", "LA")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# Неудачные попытки логина на пользователя. Ограничение нужно не от злоумышленника
# (человек вводит пароль к своему же аккаунту), а от Abbott: на серию неудачных
# логинов Cloudflare отвечает баном 476 на ВЕСЬ регион (#135/#139/#141) — встанут
# и чужие followers того же региона. Бот однопроцессный, dict в памяти достаточно.
_MAX_LOGIN_ATTEMPTS = 5
_ATTEMPT_WINDOW_SEC = 900  # 15 минут
_MAX_TRACKED_USERS = 500
_login_attempts: dict[int, list[float]] = {}


def _now() -> float:
    return time.monotonic()


def attempts_left(user_id: int, now: float | None = None) -> int:
    """Сколько неудачных попыток ещё доступно пользователю в текущем окне."""
    now = _now() if now is None else now
    fresh = [ts for ts in _login_attempts.get(user_id, []) if now - ts < _ATTEMPT_WINDOW_SEC]
    return max(0, _MAX_LOGIN_ATTEMPTS - len(fresh))


def register_failed_attempt(user_id: int, now: float | None = None) -> int:
    """Зафиксировать неудачный логин. Возвращает остаток попыток."""
    now = _now() if now is None else now
    fresh = [ts for ts in _login_attempts.get(user_id, []) if now - ts < _ATTEMPT_WINDOW_SEC]
    fresh.append(now)
    _login_attempts[user_id] = fresh
    if len(_login_attempts) > _MAX_TRACKED_USERS:
        # Подчищаем тех, у кого окно давно истекло, чтобы dict не рос вечно.
        for uid in [u for u, ts in _login_attempts.items() if not ts or now - ts[-1] > _ATTEMPT_WINDOW_SEC]:
            _login_attempts.pop(uid, None)
    return max(0, _MAX_LOGIN_ATTEMPTS - len(fresh))


def clear_failed_attempts(user_id: int) -> None:
    """Успешный логин обнуляет счётчик."""
    _login_attempts.pop(user_id, None)


# ── Чистая логика (тестируется без Telegram) ──────────────────────────────────


def is_valid_email(text: str) -> bool:
    """Грубая проверка формата — отсечь опечатку до сетевого логина."""
    return bool(_EMAIL_RE.match((text or "").strip()))


def ordered_regions(available: list[str]) -> list[str]:
    """Регионы для клавиатуры: сначала частые (_REGION_ORDER), потом остальные."""
    known = [r for r in _REGION_ORDER if r in available]
    return known + sorted(r for r in available if r not in known)


def short_login_error(err: object) -> str:
    """Причина неудачного логина человеческим языком.

    Текст библиотеки наружу не отдаём целиком: он длинный и может содержать
    служебные детали. Два случая разбираем отдельно, потому что путать их дорого:
    неверный пароль исправляет пользователь, а 476 — это бан Cloudflare, и
    повторные попытки только продлевают его (см. #135/#139/#141).
    """
    text = str(err or "")
    low = text.lower()
    if "invalid" in low and ("credential" in low or "login" in low):
        return "неверный email или пароль"
    if "476" in low:
        return "Abbott временно блокирует запросы (защита от частых логинов) — попробуй через час"
    return (text[:180] + "…") if len(text) > 180 else (text or "неизвестная ошибка")


def format_patients(patients: list[dict]) -> str:
    """Текст со списком пациентов, видимых follower-аккаунтом."""
    lines = ["✅ Логин прошёл. Этот аккаунт видит:\n"]
    for pat in patients:
        lines.append(f"• {pat.get('name') or '(без имени)'}")
    lines.append("\nВыбери, чьи данные писать в твой профиль:")
    return "\n".join(lines)


# ── Клавиатуры ────────────────────────────────────────────────────────────────


def _path_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пригласить наш аккаунт (EU)",
                    callback_data=CgmPathCallback(kind="invite").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="Свой follower-аккаунт",
                    callback_data=CgmPathCallback(kind="own").pack(),
                )
            ],
        ]
    )


def _region_keyboard() -> InlineKeyboardMarkup:
    try:
        from core.health.glucose_runtime import available_regions

        regions = ordered_regions(available_regions())
    except Exception as e:  # pylibrelinkup недоступен — не оставляем пользователя без выбора
        logger.warning("connect_cgm: не смог получить список регионов (%s)", e)
        regions = ["RU", "EU", "US"]
    rows, row = [], []
    for reg in regions:
        row.append(InlineKeyboardButton(text=reg, callback_data=CgmRegionCallback(region=reg).pack()))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bind_keyboard(patients: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=pat.get("name") or pat["patient_id"][:8],
                    callback_data=CgmBindCallback(patient_id=pat["patient_id"]).pack(),
                )
            ]
            for pat in patients
        ]
    )


# ── Обёртки над сетью и БД ────────────────────────────────────────────────────


def _validate_follower(region: str, email: str, password: str) -> list[dict]:
    """Синхронный логин + список пациентов. Backoff региона не трогает (см. librelinkup)."""
    from core.health.glucose_runtime import validate_follower

    return validate_follower(region, email, password)


def _save_follower(telegram_id: int, region: str, email: str, password: str) -> tuple[bool, str]:
    """Сохранить креды (шифрование внутри crud). (ok, причина-если-нет)."""
    from database import SessionLocal
    from database.crud import create_cgm_follower

    db = SessionLocal()
    try:
        # login_ok: логин уже проверен живьём выше, поэтому сразу ставим last_ok_at —
        # иначе /my_connections покажет «ещё не логинился» у рабочего аккаунта.
        create_cgm_follower(db, telegram_id, region=region, email=email, password=password, login_ok=True)
        return True, ""
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        # Самый вероятный случай — не задан SECRETS_KEY: шифровать нечем, и
        # писать plaintext вместо этого мы осознанно не будем.
        logger.error("connect_cgm: не смог сохранить follower для %s: %s", telegram_id, e)
        return False, "внутренняя ошибка, посмотри логи"
    finally:
        db.close()


# ── Хендлеры ──────────────────────────────────────────────────────────────────


@router.callback_query(CgmPathCallback.filter())
async def on_path(callback: CallbackQuery, callback_data: CgmPathCallback, state: FSMContext) -> None:
    """Выбор пути: пригласить наш follower (прежний поток) или завести свой."""
    if callback.message is None:
        await callback.answer()
        return

    if callback_data.kind == "invite":
        if _connect_lock.locked():
            await callback.answer("Подключение уже выполняется — подожди пару минут", show_alert=True)
            return
        await callback.message.answer(INSTRUCTIONS, parse_mode="Markdown")
        task = asyncio.create_task(_connect_flow(callback.message, callback.from_user.id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        await callback.answer()
        return

    await state.clear()
    await callback.message.answer(OWN_FOLLOWER_INTRO, reply_markup=_region_keyboard(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(CgmRegionCallback.filter())
async def on_region(callback: CallbackQuery, callback_data: CgmRegionCallback, state: FSMContext) -> None:
    """Регион выбран → просим email."""
    if callback.message is None:
        await callback.answer()
        return
    await state.update_data(region=callback_data.region)
    await state.set_state(FollowerSetup.email)
    await callback.message.answer(
        f"Регион *{callback_data.region}*. Пришли email аккаунта LibreLinkUp, "
        "который наблюдает за твоим сенсором.\n\n/cancel — отменить",
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(Command("cancel"), StateFilter(FollowerSetup.email, FollowerSetup.password))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил. Ничего не сохранил.")


@router.message(FollowerSetup.email)
async def step_email(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not is_valid_email(text):
        await message.answer("Это не похоже на email. Пришли адрес целиком или /cancel")
        return
    await state.update_data(email=text)
    await state.set_state(FollowerSetup.password)
    await message.answer(PASSWORD_PROMPT)


@router.message(FollowerSetup.password)
async def step_password(message: Message, state: FSMContext) -> None:
    """Пароль: удаляем сообщение, проверяем логин, только потом пишем в БД."""
    password = message.text or ""

    # ПЕРВЫМ действием — удалить сообщение с паролем: до сети и до записи в БД.
    # Если дальше что-то упадёт, пароль всё равно уже не висит в истории чата.
    try:
        await message.delete()
    except Exception as e:
        logger.warning("connect_cgm: не смог удалить сообщение с паролем: %s", e)
        await message.answer("⚠️ Не смог удалить твоё сообщение с паролем — удали его сам, пожалуйста.")

    data = await state.get_data()
    region = (data.get("region") or "").upper()
    email = data.get("email") or ""
    # Состояние чистим сразу: пароль в FSM-хранилище не кладём вообще, а email и
    # регион дальше не нужны — всё, что нужно, уже в локальных переменных.
    await state.clear()

    if not password.strip():
        await message.answer("Пустой пароль — начни заново: /connect_cgm")
        return
    # Регион и email проверяем одинаково: молчаливый фолбэк региона на EU привёл бы
    # к логину не в тот регион и невнятной ошибке «неверный пароль».
    if not email or not region:
        await message.answer("Потерял данные диалога — начни заново: /connect_cgm")
        return

    if attempts_left(message.from_user.id) <= 0:
        await message.answer(
            f"⏳ Слишком много неудачных попыток. Подожди {_ATTEMPT_WINDOW_SEC // 60} минут: "
            "Abbott банит частые логины на весь регион, и тогда встанет уже работающий сбор данных."
        )
        return

    status = await message.answer("Проверяю логин…")

    # Проверка ДО сохранения: иначе неверные креды осели бы в БД, и ночной sync
    # ловил бы ими 476, уходя в backoff вместе с рабочими follower'ами.
    try:
        patients = await asyncio.to_thread(_validate_follower, region, email, password)
    except Exception as e:
        logger.warning("connect_cgm: логин follower[%s] не прошёл: %s", region, type(e).__name__)
        left = register_failed_attempt(message.from_user.id)
        tail = f"Осталось попыток: {left}." if left else "Попытки на сегодня исчерпаны, вернись через 15 минут."
        await status.edit_text(
            f"❌ Логин не прошёл: {short_login_error(e)}\n\nНичего не сохранил. {tail}\n/connect_cgm — попробовать снова."
        )
        return

    clear_failed_attempts(message.from_user.id)

    ok, err = await asyncio.to_thread(_save_follower, message.from_user.id, region, email, password)
    if not ok:
        await status.edit_text(f"❌ Не сохранил: {err}")
        return

    if not patients:
        await status.edit_text(
            "✅ Креды сохранил, логин работает — но этот аккаунт пока никого не наблюдает.\n\n"
            "Открой FreeStyle Libre → Connected Apps → LibreLinkUp и пригласи этот email "
            "как follower, потом запусти /connect_cgm ещё раз."
        )
        return

    await status.edit_text(format_patients(patients), reply_markup=_bind_keyboard(patients))


@router.callback_query(CgmBindCallback.filter())
async def on_bind(callback: CallbackQuery, callback_data: CgmBindCallback) -> None:
    """Привязать выбранного пациента к пользователю."""
    if callback.message is None:
        await callback.answer()
        return
    try:
        saved = await asyncio.to_thread(_save_mapping, callback_data.patient_id, callback.from_user.id)
    except Exception as e:
        logger.error("connect_cgm: не смог привязать %s: %s", callback_data.patient_id, e)
        await callback.answer("Не смог сохранить привязку", show_alert=True)
        return
    if saved:
        await callback.message.answer(
            "✅ CGM подключён! Глюкоза начнёт поступать после ближайшего синка "
            "(и после прогрева сенсора, если он новый — около часа)."
        )
        await callback.answer()
        return

    # unique(patient_id) не дал записать — выясняем, чей он: свой или чужой.
    owner = await asyncio.to_thread(_mapping_owner, callback_data.patient_id)
    if owner == callback.from_user.id:
        await callback.message.answer(
            "✅ Этот сенсор уже подключён к твоему профилю — ничего менять не нужно.\n"
            "Креды follower-аккаунта я сохранил, дальше данные идут автоматически."
        )
    else:
        await callback.message.answer(
            "Этот сенсор уже привязан к другому профилю. Если это ошибка — напиши, разберёмся вручную."
        )
    await callback.answer()


CHOOSE_PATH_TEXT = (
    "🩸 *Подключение CGM*\n\n"
    "Есть два способа:\n\n"
    "*Пригласить наш аккаунт* — проще, но работает только для EU: приглашения "
    "follower'а в LibreLinkUp действуют внутри одного региона.\n\n"
    "*Свой follower-аккаунт* — для России и остальных регионов: подойдёт аккаунт, "
    "который уже наблюдает за твоим сенсором (например, аккаунт близкого)."
)

OWN_FOLLOWER_INTRO = (
    "Выбери регион аккаунта LibreLinkUp.\n\n"
    "Это тот регион, в котором активирован сенсор: у российского приложения — *RU*."
)

PASSWORD_PROMPT = (
    "Теперь пришли пароль от этого аккаунта.\n\n"
    "Я удалю сообщение с паролем сразу, как прочитаю, и сохраню его "
    "зашифрованным — в открытом виде он нигде не останется.\n\n"
    "/cancel — отменить"
)
