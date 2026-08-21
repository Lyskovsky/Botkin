"""Хендлер /connect_cgm: ветка «свой follower-аккаунт» (#381).

Главное, что здесь проверяется: сообщение с паролем удаляется ДО сети и БД,
неверные креды не оседают в базе, а прежний EU-путь остался рабочим.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from handlers import connect_cgm as h

UID = 836757955


def _message(text=None):
    msg = MagicMock()
    msg.from_user.id = UID
    msg.text = text
    msg.delete = AsyncMock()
    status = MagicMock()
    status.edit_text = AsyncMock()
    msg.answer = AsyncMock(return_value=status)
    return msg, status


def _state(data=None):
    st = MagicMock()
    st.get_data = AsyncMock(return_value=data or {})
    st.update_data = AsyncMock()
    st.set_state = AsyncMock()
    st.clear = AsyncMock()
    return st


# ── чистые функции ────────────────────────────────────────────────────────────


def test_is_valid_email():
    assert h.is_valid_email("pohodnyalla@icloud.com")
    assert not h.is_valid_email("no-at-sign")
    assert not h.is_valid_email("a@b")  # без TLD
    assert not h.is_valid_email("")


def test_ordered_regions_puts_ru_and_eu_first():
    assert h.ordered_regions(["JP", "US", "EU", "RU"])[:2] == ["RU", "EU"]


def test_ordered_regions_keeps_unknown_ones():
    got = h.ordered_regions(["RU", "ZZ"])
    assert "ZZ" in got and got[0] == "RU"


def test_short_login_error_invalid_credentials():
    assert h.short_login_error(Exception("Invalid login credentials")) == "неверный email или пароль"


def test_short_login_error_476_explains_ban():
    """476 — бан Cloudflare, а не пароль: повторы только продлевают его."""
    msg = h.short_login_error(Exception("HTTP 476 too many requests"))
    assert "Abbott" in msg and "позже" in msg or "час" in msg


def test_short_login_error_truncates_long_text():
    assert len(h.short_login_error(Exception("x" * 500))) < 200


def test_format_patients_lists_names():
    text = h.format_patients([{"patient_id": "p1", "name": "Андрей"}])
    assert "Андрей" in text


# ── шаг email ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_step_rejects_garbage_and_stays():
    msg, _ = _message("это не email")
    st = _state()

    await h.step_email(msg, st)

    st.set_state.assert_not_called()  # остаёмся на шаге email
    assert msg.answer.await_count == 1


@pytest.mark.asyncio
async def test_email_step_accepts_and_moves_to_password():
    msg, _ = _message("a@icloud.com")
    st = _state()

    await h.step_email(msg, st)

    st.update_data.assert_awaited_once_with(email="a@icloud.com")
    st.set_state.assert_awaited_once_with(h.FollowerSetup.password)


# ── шаг пароля ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_message_deleted_before_network(monkeypatch):
    """Удаление сообщения — первым действием, до логина в LibreLinkUp."""
    order = []
    msg, status = _message("secret")
    msg.delete = AsyncMock(side_effect=lambda: order.append("delete"))
    st = _state({"region": "RU", "email": "a@x.ru"})

    def validate(region, email, password):
        order.append("network")
        return [{"patient_id": "p1", "name": "Андрей"}]

    monkeypatch.setattr(h, "_validate_follower", validate)
    monkeypatch.setattr(h, "_save_follower", lambda *a: (True, ""))

    await h.step_password(msg, st)

    assert order == ["delete", "network"]


@pytest.mark.asyncio
async def test_password_not_left_in_fsm_state(monkeypatch):
    """Пароль не должен оказаться в FSM-хранилище: state чистим до сети."""
    msg, _ = _message("secret")
    st = _state({"region": "RU", "email": "a@x.ru"})
    monkeypatch.setattr(h, "_validate_follower", lambda *a: [])
    monkeypatch.setattr(h, "_save_follower", lambda *a: (True, ""))

    await h.step_password(msg, st)

    st.clear.assert_awaited()
    for call in st.update_data.await_args_list:
        assert "password" not in call.kwargs


@pytest.mark.asyncio
async def test_invalid_credentials_are_not_saved(monkeypatch):
    """Логин не прошёл → в БД ничего не пишем (иначе ночной sync ловил бы 476)."""
    msg, status = _message("wrong")
    st = _state({"region": "RU", "email": "a@x.ru"})
    saved = MagicMock()

    def boom(*a):
        raise RuntimeError("Invalid login credentials")

    monkeypatch.setattr(h, "_validate_follower", boom)
    monkeypatch.setattr(h, "_save_follower", saved)

    await h.step_password(msg, st)

    saved.assert_not_called()
    assert "неверный email или пароль" in status.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_valid_credentials_saved_with_region_and_email(monkeypatch):
    msg, status = _message("right-pass")
    st = _state({"region": "ru", "email": "a@x.ru"})
    calls = []

    monkeypatch.setattr(h, "_validate_follower", lambda *a: [{"patient_id": "p1", "name": "Андрей"}])
    monkeypatch.setattr(h, "_save_follower", lambda *a: (calls.append(a), (True, ""))[1])

    await h.step_password(msg, st)

    assert calls == [(UID, "RU", "a@x.ru", "right-pass")]  # регион нормализован
    assert status.edit_text.await_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_save_failure_reported(monkeypatch):
    msg, status = _message("p")
    st = _state({"region": "RU", "email": "a@x.ru"})
    monkeypatch.setattr(h, "_validate_follower", lambda *a: [{"patient_id": "p1", "name": "N"}])
    monkeypatch.setattr(h, "_save_follower", lambda *a: (False, "внутренняя ошибка, посмотри логи"))

    await h.step_password(msg, st)

    assert "Не сохранил" in status.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_login_ok_but_no_patients_still_saves(monkeypatch):
    """Креды валидны, но follower никого не наблюдает — сохраняем, чтобы не
    заставлять вводить пароль второй раз, и просим пригласить аккаунт."""
    msg, status = _message("p")
    st = _state({"region": "RU", "email": "a@x.ru"})
    calls = []
    monkeypatch.setattr(h, "_validate_follower", lambda *a: [])
    monkeypatch.setattr(h, "_save_follower", lambda *a: (calls.append(a), (True, ""))[1])

    await h.step_password(msg, st)

    assert len(calls) == 1
    assert "пригласи" in status.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_empty_password_does_not_touch_network(monkeypatch):
    msg, _ = _message("   ")
    st = _state({"region": "RU", "email": "a@x.ru"})
    net = MagicMock()
    monkeypatch.setattr(h, "_validate_follower", net)

    await h.step_password(msg, st)

    net.assert_not_called()


@pytest.mark.asyncio
async def test_lost_email_does_not_touch_network(monkeypatch):
    """Состояние потерялось (рестарт бота) — просим начать заново, а не логинимся пустым."""
    msg, _ = _message("p")
    st = _state({"region": "RU"})
    net = MagicMock()
    monkeypatch.setattr(h, "_validate_follower", net)

    await h.step_password(msg, st)

    net.assert_not_called()


@pytest.mark.asyncio
async def test_undeletable_password_warns_user(monkeypatch):
    """Если удалить сообщение нельзя — честно предупреждаем, но продолжаем."""
    msg, status = _message("p")
    msg.delete = AsyncMock(side_effect=RuntimeError("message can't be deleted"))
    st = _state({"region": "RU", "email": "a@x.ru"})
    monkeypatch.setattr(h, "_validate_follower", lambda *a: [{"patient_id": "p1", "name": "N"}])
    monkeypatch.setattr(h, "_save_follower", lambda *a: (True, ""))

    await h.step_password(msg, st)

    warned = any("удали" in str(c.args[0]).lower() for c in msg.answer.await_args_list)
    assert warned


# ── прежний EU-путь не сломан ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_command_offers_both_paths():
    msg, _ = _message()

    await h.cmd_connect_cgm(msg)

    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert len(kb.inline_keyboard) == 2


@pytest.mark.asyncio
async def test_invite_path_starts_old_flow(monkeypatch):
    """Кнопка «пригласить наш аккаунт» запускает прежний поллинг get_patients()."""
    started = []

    async def fake_flow(message, telegram_id):
        started.append(telegram_id)

    monkeypatch.setattr(h, "_connect_flow", fake_flow)
    cb = MagicMock()
    cb.from_user.id = UID
    cb.message, _ = _message()
    cb.answer = AsyncMock()

    await h.on_path(cb, h.CgmPathCallback(kind="invite"), _state())
    for task in list(h._background_tasks):
        await task

    assert started == [UID]
    assert "LibreLinkUp" in cb.message.answer.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_own_path_asks_region():
    cb = MagicMock()
    cb.from_user.id = UID
    cb.message, _ = _message()
    cb.answer = AsyncMock()

    await h.on_path(cb, h.CgmPathCallback(kind="own"), _state())

    kb = cb.message.answer.await_args.kwargs["reply_markup"]
    regions = [b.text for row in kb.inline_keyboard for b in row]
    assert "RU" in regions


@pytest.mark.asyncio
async def test_bind_saves_mapping(monkeypatch):
    monkeypatch.setattr(h, "_save_mapping", lambda pid, tid: True)
    cb = MagicMock()
    cb.from_user.id = UID
    cb.message, _ = _message()
    cb.answer = AsyncMock()

    await h.on_bind(cb, h.CgmBindCallback(patient_id="019b128b-e9d7-7e9f-996a-843e5887adc1"))

    assert "подключён" in cb.message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_bind_reports_taken_patient(monkeypatch):
    monkeypatch.setattr(h, "_save_mapping", lambda pid, tid: False)
    cb = MagicMock()
    cb.from_user.id = UID
    cb.message, _ = _message()
    cb.answer = AsyncMock()

    await h.on_bind(cb, h.CgmBindCallback(patient_id="p1"))

    assert "уже привязан" in cb.message.answer.await_args.args[0].lower()


# ── лимит неудачных попыток (ревью #382) ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_attempts():
    h._login_attempts.clear()
    yield
    h._login_attempts.clear()


def test_attempts_left_starts_full():
    assert h.attempts_left(UID) == h._MAX_LOGIN_ATTEMPTS


def test_register_failed_attempt_decrements():
    assert h.register_failed_attempt(UID, now=1000.0) == h._MAX_LOGIN_ATTEMPTS - 1
    assert h.attempts_left(UID, now=1000.0) == h._MAX_LOGIN_ATTEMPTS - 1


def test_attempts_window_expires():
    """Старые неудачи выпадают из окна — пользователь не заперт навсегда."""
    h.register_failed_attempt(UID, now=1000.0)
    assert h.attempts_left(UID, now=1000.0 + h._ATTEMPT_WINDOW_SEC + 1) == h._MAX_LOGIN_ATTEMPTS


def test_clear_failed_attempts_on_success():
    h.register_failed_attempt(UID, now=1000.0)
    h.clear_failed_attempts(UID)
    assert h.attempts_left(UID, now=1000.0) == h._MAX_LOGIN_ATTEMPTS


@pytest.mark.asyncio
async def test_exhausted_attempts_block_network(monkeypatch):
    """Лимит исчерпан → в LibreLinkUp не идём: серия неудач = бан 476 на весь регион."""
    # Время берём то же, что использует хендлер (monotonic), иначе попытки
    # окажутся «просроченными» и лимит не сработает.
    now = h._now()
    for i in range(h._MAX_LOGIN_ATTEMPTS):
        h.register_failed_attempt(UID, now=now + i)
    msg, _ = _message("p")
    st = _state({"region": "RU", "email": "a@x.ru"})
    net = MagicMock()
    monkeypatch.setattr(h, "_validate_follower", net)

    await h.step_password(msg, st)

    net.assert_not_called()
    assert msg.delete.await_count == 1  # пароль всё равно удалён
    assert "попыток" in msg.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_failed_login_registers_attempt(monkeypatch):
    msg, _ = _message("wrong")
    st = _state({"region": "RU", "email": "a@x.ru"})

    def boom(*a):
        raise RuntimeError("Invalid login credentials")

    monkeypatch.setattr(h, "_validate_follower", boom)
    monkeypatch.setattr(h, "_save_follower", lambda *a: (True, ""))

    await h.step_password(msg, st)

    assert h.attempts_left(UID) == h._MAX_LOGIN_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_successful_login_clears_attempts(monkeypatch):
    h.register_failed_attempt(UID)
    msg, _ = _message("right")
    st = _state({"region": "RU", "email": "a@x.ru"})
    monkeypatch.setattr(h, "_validate_follower", lambda *a: [{"patient_id": "p1", "name": "N"}])
    monkeypatch.setattr(h, "_save_follower", lambda *a: (True, ""))

    await h.step_password(msg, st)

    assert h.attempts_left(UID) == h._MAX_LOGIN_ATTEMPTS


@pytest.mark.asyncio
async def test_lost_region_does_not_fall_back_to_eu(monkeypatch):
    """Регион и email проверяем одинаково: молчаливый EU дал бы логин не в тот регион."""
    msg, _ = _message("p")
    st = _state({"email": "a@x.ru"})  # region потерян
    net = MagicMock()
    monkeypatch.setattr(h, "_validate_follower", net)

    await h.step_password(msg, st)

    net.assert_not_called()
    assert "начни заново" in msg.answer.await_args.args[0].lower()


# ── привязка: своя vs чужая (живой прогон 21.08.2026) ────────────────────────


@pytest.mark.asyncio
async def test_bind_existing_own_mapping_says_already_yours(monkeypatch):
    """Сенсор уже привязан к ЭТОМУ пользователю — не пугаем «другим профилем»."""
    monkeypatch.setattr(h, "_save_mapping", lambda pid, tid: False)
    monkeypatch.setattr(h, "_mapping_owner", lambda pid: UID)
    cb = MagicMock()
    cb.from_user.id = UID
    cb.message, _ = _message()
    cb.answer = AsyncMock()

    await h.on_bind(cb, h.CgmBindCallback(patient_id="p1"))

    text = cb.message.answer.await_args.args[0].lower()
    assert "твоему профилю" in text and "другому профилю" not in text


@pytest.mark.asyncio
async def test_bind_foreign_mapping_still_warns(monkeypatch):
    monkeypatch.setattr(h, "_save_mapping", lambda pid, tid: False)
    monkeypatch.setattr(h, "_mapping_owner", lambda pid: 111111)
    cb = MagicMock()
    cb.from_user.id = UID
    cb.message, _ = _message()
    cb.answer = AsyncMock()

    await h.on_bind(cb, h.CgmBindCallback(patient_id="p1"))

    assert "другому профилю" in cb.message.answer.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_bind_unknown_owner_falls_back_to_warning(monkeypatch):
    """Владельца определить не удалось — осторожная формулировка, а не «всё ок»."""
    monkeypatch.setattr(h, "_save_mapping", lambda pid, tid: False)
    monkeypatch.setattr(h, "_mapping_owner", lambda pid: None)
    cb = MagicMock()
    cb.from_user.id = UID
    cb.message, _ = _message()
    cb.answer = AsyncMock()

    await h.on_bind(cb, h.CgmBindCallback(patient_id="p1"))

    assert "другому профилю" in cb.message.answer.await_args.args[0].lower()
