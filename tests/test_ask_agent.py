"""Тесты ask_agent — главный agent-loop BotkinClaw (аудит 11.06.2026: было 0 тестов).

Anthropic API и tools API замоканы на уровне core.agent_chat.requests;
история — настоящая таблица agent_conversations на SQLite (прод-CAST AS JSONB
переписывается engine-событием, см. фикстуру agent_db).
"""

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.models import Base, User

import core.agent_chat as agent_chat


# ── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def agent_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    # Прод-SQL пишет историю через CAST(:content AS JSONB) — на SQLite такой
    # CAST имеет NUMERIC-affinity и превращает JSON-строку в 0. Переписываем
    # на лету, сохраняя остальную логику настоящей.
    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_jsonb_cast(conn, cursor, statement, parameters, context, executemany):
        return statement.replace("CAST(? AS JSONB)", "?"), parameters

    Base.metadata.create_all(bind=engine)
    with engine.connect() as c:
        # agent_conversations теперь есть в ORM-метадате (Base.create_all её создаёт),
        # но этот тест намеренно держит свою SQLite-схему таблицы (INTEGER PK AUTOINCREMENT,
        # content TEXT — чтобы воспроизвести прод-CAST AS JSONB). Сносим ORM-версию и
        # пересоздаём ровно в нужной форме.
        c.execute(text("DROP TABLE IF EXISTS agent_conversations"))
        c.execute(
            text(
                """CREATE TABLE agent_conversations (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       user_id BIGINT NOT NULL,
                       role TEXT NOT NULL,
                       content TEXT NOT NULL,
                       tool_use_id TEXT,
                       source TEXT,
                       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
            )
        )
        c.commit()
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    session = TestSession()
    session.add(
        User(
            telegram_id=895655,
            first_name="Sasha",
            cohort="owner",
            pack_name="bariatric",
            jwt_secret="test_secret",
            agent_system_prompt="Ты — семейный AI-врач. Отвечай кратко.",
            is_active=True,
        )
    )
    session.commit()
    session.close()

    monkeypatch.setattr(agent_chat, "SessionLocal", TestSession)
    # usage-логгер ходит в реальный Postgres своим SessionLocal — глушим
    import core.llm_usage as llm_usage

    monkeypatch.setattr(llm_usage, "log_anthropic_response", lambda **kw: None)
    return TestSession


class FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeRequests:
    """Подменяет core.agent_chat.requests: Anthropic — по сценарию, tools — заглушка."""

    def __init__(self, anthropic_script, tool_payload=None):
        self.anthropic_script = list(anthropic_script)
        self.tool_payload = tool_payload or {"status": "ok"}
        self.anthropic_calls = []
        self.tool_calls = []

    def post(self, url, headers=None, json=None, timeout=None, params=None):
        if url == agent_chat.ANTHROPIC_API_URL:
            self.anthropic_calls.append({"headers": headers, "payload": json})
            return self.anthropic_script.pop(0)
        self.tool_calls.append({"url": url, "headers": headers, "json": json})
        return FakeResp(self.tool_payload)

    def get(self, url, headers=None, params=None, timeout=None):
        self.tool_calls.append({"url": url, "headers": headers, "params": params})
        return FakeResp(self.tool_payload)


def _anthropic_text(text_str):
    return FakeResp(
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text_str}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
    )


def _anthropic_tool_use(name, args, tu_id="tu_001"):
    return FakeResp(
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "Сейчас посмотрю."},
                {"type": "tool_use", "id": tu_id, "name": name, "input": args},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 30},
        }
    )


def _history_rows(TestSession):
    s = TestSession()
    rows = s.execute(text("SELECT role, content, source FROM agent_conversations ORDER BY id")).fetchall()
    s.close()
    return rows


# ── Тесты ────────────────────────────────────────────────────────────────────


def test_text_answer_saved_to_history(agent_db, monkeypatch):
    """(1) Текстовый ответ возвращается и пишется в agent_conversations."""
    fake = FakeRequests([_anthropic_text("Всё в порядке, вес стабилен.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    reply = agent_chat.ask_agent(895655, "как мой вес?")

    assert reply == "Всё в порядке, вес стабилен."
    rows = _history_rows(agent_db)
    roles = [r.role for r in rows]
    assert roles == ["user", "assistant"]
    assert all(r.source == "botkinclaw" for r in rows)
    assert "как мой вес?" in json.loads(rows[0].content)
    assert "вес стабилен" in str(json.loads(rows[1].content))


def test_tool_loop_calls_tools_api_and_returns_final_answer(agent_db, monkeypatch):
    """(2) tool_use → HTTP-вызов tools API с JWT → tool_result → финальный ответ."""
    fake = FakeRequests(
        [
            _anthropic_tool_use("get_weight_history", {"days": 7}),
            _anthropic_text("Твой вес 82.0 кг, тренд стабильный."),
        ],
        tool_payload={"status": "ok", "latest": {"weight_kg": 82.0}},
    )
    monkeypatch.setattr(agent_chat, "requests", fake)

    reply = agent_chat.ask_agent(895655, "что с весом за неделю?")

    assert "82.0" in reply
    # Anthropic вызван дважды: tool_use + финал
    assert len(fake.anthropic_calls) == 2
    # Tools API вызван с Bearer JWT
    assert len(fake.tool_calls) == 1
    auth = fake.tool_calls[0]["headers"]["Authorization"]
    assert auth.startswith("Bearer ")
    # Во втором вызове Anthropic ушёл tool_result с данными
    second_msgs = fake.anthropic_calls[1]["payload"]["messages"]
    flat = json.dumps(second_msgs, ensure_ascii=False)
    assert "tool_result" in flat and "82.0" in flat.replace("\\", "")
    # История: user → assistant(tool_use) → tool_result → assistant(финал)
    roles = [r.role for r in _history_rows(agent_db)]
    assert roles == ["user", "assistant", "tool_result", "assistant"]


def test_supplement_daily_log_tool_dispatch(agent_db, monkeypatch):
    """get_supplement_daily_log → GET /supplement_daily_log с days + supplement."""
    fake = FakeRequests(
        [
            _anthropic_tool_use("get_supplement_daily_log", {"days": 30, "supplement": "магний"}),
            _anthropic_text("Магний принимался 12 из 30 дней."),
        ],
        tool_payload={"status": "ok", "supplements": [{"supplement": "магний", "days_taken": 12}]},
    )
    monkeypatch.setattr(agent_chat, "requests", fake)

    reply = agent_chat.ask_agent(895655, "влияет ли магний на мой сон?")

    assert "12" in reply
    assert len(fake.tool_calls) == 1
    call = fake.tool_calls[0]
    assert call["url"].endswith("/supplement_daily_log")
    assert call["params"]["days"] == 30
    assert call["params"]["supplement"] == "магний"


def test_api_error_raises_cleanly(agent_db, monkeypatch):
    """(3) 500 от Anthropic → чистый HTTPError наружу (хендлер его ловит),
    без полу-сохранённого ответа ассистента в истории."""
    import requests as real_requests

    fake = FakeRequests([FakeResp({"error": "boom"}, status_code=500)])
    monkeypatch.setattr(agent_chat, "requests", fake)

    with pytest.raises(real_requests.HTTPError):
        agent_chat.ask_agent(895655, "привет")

    roles = [r.role for r in _history_rows(agent_db)]
    assert "assistant" not in roles


def test_inactive_user_rejected(agent_db, monkeypatch):
    """Неактивный/чужой user_id — RuntimeError, ноль обращений к API."""
    fake = FakeRequests([])
    monkeypatch.setattr(agent_chat, "requests", fake)

    with pytest.raises(RuntimeError):
        agent_chat.ask_agent(999999, "привет")
    assert fake.anthropic_calls == []


# ── Дефолтный системный промпт для всех пользователей (#165) ──────────────────


def test_build_default_agent_prompt_includes_name_and_goal():
    """Билдер собирает непустой промпт с именем и целью из onboarding_data."""
    u = User(
        telegram_id=1,
        first_name="Кристина",
        onboarding_data={"name": "Кристина", "goal": "Долголетие/профилактика", "age": 33, "sex": "female"},
    )

    prompt = agent_chat.build_default_agent_prompt(u)

    assert "Кристина" in prompt
    assert "Долголетие" in prompt
    assert "Botkin" in prompt  # рамка проекта на месте


def test_build_default_agent_prompt_never_empty_without_data():
    """Даже без onboarding_data и first_name промпт не пустой (fallback-имя)."""
    u = User(telegram_id=2, first_name=None, onboarding_data=None)

    prompt = agent_chat.build_default_agent_prompt(u)

    assert prompt.strip()
    assert "AI-агент" in prompt


def test_build_default_prompt_no_history_claim_when_profile_exists():
    """#340: при непустом медпрофиле промпт НЕ утверждает, что медистории нет —
    иначе агент верил базовой фразе и советовал как здоровому, игнорируя блок
    «Медпрофиль» с диагнозами."""
    u = User(
        telegram_id=3,
        first_name="Тест",
        onboarding_data={"name": "Тест", "chronic_conditions": ["Гипотиреоз (E03.9)"]},
    )

    prompt = agent_chat.build_default_agent_prompt(u)

    assert "без подробной медицинской истории" not in prompt
    assert "Медпрофиль" in prompt  # отсылка к блоку, который приклеивается ниже


def test_build_default_prompt_keeps_history_claim_without_profile():
    """Без диагнозов/аллергий фраза про отсутствие медистории остаётся."""
    u = User(telegram_id=4, first_name="Тест", onboarding_data={"name": "Тест"})

    assert "без подробной медицинской истории" in agent_chat.build_default_agent_prompt(u)


def test_user_without_system_prompt_uses_default(agent_db, monkeypatch):
    """Зарегистрированный юзер без agent_system_prompt НЕ отвергается —
    агент работает на дефолтном промпте, в system уходит имя/цель юзера."""
    s = agent_db()
    s.add(
        User(
            telegram_id=700700,
            first_name="Кристина",
            cohort="external",
            jwt_secret="sek",
            is_active=True,
            agent_system_prompt=None,
            onboarding_data={"name": "Кристина", "goal": "Долголетие/профилактика"},
        )
    )
    s.commit()
    s.close()
    fake = FakeRequests([_anthropic_text("Просто напиши «съел банан» или пришли фото тарелки.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    reply = agent_chat.ask_agent(700700, "как мне вносить еду?")

    assert "банан" in reply
    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "Кристина" in sys_text


def test_system_prompt_instructs_supplement_logging(agent_db, monkeypatch):
    """#191: в system-prompt есть инструкция логировать приём добавок из текста
    и давать фидбек по схеме (а не просить написать её заново)."""
    fake = FakeRequests([_anthropic_text("Записал омегу.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "выпил омегу 3")

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "log_supplement" in sys_text
    assert "ДОБАВКИ" in sys_text
    # не просить переписать уже описанную схему
    assert "напиши схему" in sys_text.lower()


def test_system_prompt_forbids_claiming_data_without_tool(agent_db, monkeypatch):
    """#190: в system-prompt есть гард — не заявлять что видишь данные из БД
    без вызова инструмента в этом ходе."""
    fake = FakeRequests([_anthropic_text("Сейчас проверю.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "я же отправил фото добавок")

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "НЕ ЗАЯВЛЯЙ ЧТО ВИДИШЬ ДАННЫЕ БЕЗ ВЫЗОВА ИНСТРУМЕНТА" in sys_text
    assert "прямого доступа к базе данных" in sys_text


def test_system_prompt_forces_fresh_meal_tool_call(agent_db, monkeypatch):
    """#207: в system-prompt есть гард — на вопрос об истории еды ВСЕГДА свежий вызов
    get_recent_meals/get_day_summary, нельзя отвечать «лог пуст» из устаревшего контекста."""
    fake = FakeRequests([_anthropic_text("Сейчас гляну лог.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "что я ел сегодня?")

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "ВСЕГДА СВЕЖИЙ ВЫЗОВ ТУЛЗЫ" in sys_text
    assert "get_recent_meals" in sys_text and "get_day_summary" in sys_text
    # ядро фикса: прежний вывод мог устареть → не отвечать из памяти
    assert "мог УСТАРЕТЬ" in sys_text
    assert "без НОВОГО вызова тулзы" in sys_text


def test_system_prompt_instructs_flag_for_devs(agent_db, monkeypatch):
    """#188: в system-prompt есть директива флагать баги/пожелания через flag_for_devs."""
    fake = FakeRequests([_anthropic_text("Передал разработчикам.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "почему ты не умеешь строить графики сна?")

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "flag_for_devs" in sys_text
    assert "не замалчивай" in sys_text


def test_system_prompt_gi_honesty(agent_db, monkeypatch):
    """#232 изъян 1 (универсально): гард — не выдавать высокоГИ-продукты
    (белый хлеб, сухофрукты, белый рис, сладкое) за «медленные углеводы»."""
    fake = FakeRequests([_anthropic_text("Смотрю по составу.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "что бы съесть на завтрак?")

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "НЕ называй высокогликемические продукты «медленными углеводами»" in sys_text
    # перечислены конкретные высокоГИ-продукты, которые нельзя выдавать за «медленные»
    assert "сухофрукты" in sys_text and "белый хлеб" in sys_text


def test_system_prompt_gates_low_gi_by_diagnosis(agent_db, monkeypatch):
    """#232 изъян 1 (адресно): при демпинге/реактивной гипо/постбариатрии —
    активно предлагать низкоГИ-замены; гейт по constraints/KB, у остальных без изменений."""
    fake = FakeRequests([_anthropic_text("Гляну ограничения.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "что съесть на перекус?")

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "Демпинг / реактивная гипогликемия / постбариатрия — низкоГИ по умолчанию" in sys_text
    # гейт: явно указано, что без диагноза в constraints/KB совет не меняется
    assert "у кого таких ограничений в constraints/KB НЕТ" in sys_text
    assert "цельное зерно вместо белого хлеба" in sys_text


def test_system_prompt_doctor_prep_balanced_drugs(agent_db, monkeypatch):
    """#232 изъян 2: doctor-prep не подаёт один препарат «ключевым»; при демпинге/
    реактивной гипо (диагноз из KB) — акарбоза как вариант для обсуждения, без назначений."""
    fake = FakeRequests([_anthropic_text("Готовлю вопросы врачу.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "какие вопросы задать эндокринологу?")

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "не подавай один препарат" in sys_text.lower()
    assert "АКАРБОЗУ" in sys_text
    # гейт по диагнозу: у кого нет — поведение не меняется
    assert "У кого такого диагноза в KB нет — поведение не меняется" in sys_text


def _insert_router_row(TestSession, user_id, source, text_str):
    s = TestSession()
    s.execute(
        text("INSERT INTO agent_conversations (user_id, role, content, source) VALUES (:u, 'user', :c, :s)"),
        {"u": user_id, "c": json.dumps([{"text": text_str, "type": "text"}], ensure_ascii=False), "s": source},
    )
    s.commit()
    s.close()


def test_recent_tracker_events_summarizes_parser_rows(agent_db):
    """router_*/llm_text user-строки попадают в сводку (issue #169)."""
    _insert_router_row(agent_db, 895655, "router_weight", "54")
    _insert_router_row(agent_db, 895655, "router_food", "съела яблоко")

    s = agent_db()
    block = agent_chat._recent_tracker_events(s, 895655)
    s.close()

    assert "54" in block
    assert "яблоко" in block
    assert "[вес]" in block and "[еда]" in block


def test_recent_tracker_events_empty_when_no_parser_rows(agent_db):
    """Без parser-записей сводка пустая (не мусорит system-prompt)."""
    s = agent_db()
    block = agent_chat._recent_tracker_events(s, 895655)
    s.close()
    assert block == ""


def test_parser_rows_injected_into_system_prompt(agent_db, monkeypatch):
    """ask_agent подмешивает parser-записи в system — агент видит, что вес записан."""
    _insert_router_row(agent_db, 895655, "router_weight", "54")
    fake = FakeRequests([_anthropic_text("Твой вес 54 кг.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "какой у меня вес?")

    system_blocks = fake.anthropic_calls[0]["payload"]["system"]
    sys_text = " ".join(b["text"] for b in system_blocks)
    assert "ЗАПИСАЛ ЧЕРЕЗ ТРЕКЕР" in sys_text
    assert "54" in sys_text
    # tracker — отдельный блок БЕЗ cache_control (не бьёт prompt-кэш)
    tracker_blocks = [b for b in system_blocks if "ЗАПИСАЛ ЧЕРЕЗ ТРЕКЕР" in b["text"]]
    assert tracker_blocks and "cache_control" not in tracker_blocks[0]


def test_per_user_prompt_takes_precedence_over_default(agent_db, monkeypatch):
    """Если agent_system_prompt задан (семейный override) — используется он,
    дефолтный билдер не вызывается."""
    fake = FakeRequests([_anthropic_text("ок")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    def _boom(_user):
        raise AssertionError("build_default_agent_prompt не должен вызываться при заданном промпте")

    monkeypatch.setattr(agent_chat, "build_default_agent_prompt", _boom)

    agent_chat.ask_agent(895655, "привет")  # у 895655 agent_system_prompt задан в фикстуре

    sys_text = fake.anthropic_calls[0]["payload"]["system"][0]["text"]
    assert "семейный AI-врач" in sys_text


# ── Гарды еды (#181) ──────────────────────────────────────────────────────────


def test_meal_guard_in_universal_meta_prompt(agent_db, monkeypatch):
    """system-prompt к Anthropic содержит блок 🍽️ с запретом галлюцинировать состав и ложного ✅."""
    fake = FakeRequests([_anthropic_text("ок")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat.ask_agent(895655, "что я ел вчера?")

    sys_text = " ".join(b["text"] for b in fake.anthropic_calls[0]["payload"]["system"])
    assert "edit_meal" in sys_text
    assert "get_recent_meals" in sys_text
    assert "ЗАПРЕЩЕНО" in sys_text


def test_edit_meal_tool_registered():
    """edit_meal зарегистрирован в TOOLS с обязательным полем meal_id и enum new_slot."""
    tool_names = [t["name"] for t in agent_chat.TOOLS]
    assert "edit_meal" in tool_names

    edit_tool = next(t for t in agent_chat.TOOLS if t["name"] == "edit_meal")
    props = edit_tool["input_schema"]["properties"]
    assert "meal_id" in props
    assert "new_slot" in props
    assert "lunch" in props["new_slot"]["enum"]


def test_compact_mode_food_key_priority():
    """Compact-режим recent_meals читает ключ 'food' для composite items (прецедент 19.06.2026)."""
    composite_item = {"food": "Боул с киноа, креветками и авокадо", "calories": 511, "protein": 40}
    legacy_item = {"product": "Яблоко", "calories": 52}
    items = [composite_item, legacy_item]

    names = [
        (it.get("food") or it.get("product") or it.get("name") or "").strip()
        for it in items
        if (it.get("food") or it.get("product") or it.get("name"))
    ]

    assert len(names) == 2
    assert "киноа" in names[0]
    assert "Яблоко" in names[1]


# ── #347: транзакция не висит поперёк вызова Anthropic ───────────────────────


def _session_recording_factory(TestSession, sink):
    """Фабрика сессий, складывающая созданные сессии в sink (для инспекции в тесте)."""

    def _factory(*args, **kwargs):
        session = TestSession(*args, **kwargs)
        sink.append(session)
        return session

    return _factory


def test_no_open_transaction_during_anthropic_call(agent_db, monkeypatch):
    """(#347) В момент HTTP-вызова Anthropic ни одна сессия ask_agent не должна
    быть в открытой транзакции.

    Прод-инцидент 26.07.2026: транзакция висела открытой поперёк requests.post
    (до 60с), а session-level idle_in_transaction_session_timeout=15000 рвал
    соединение — INSERT ответа падал, готовый ответ пользователю не доходил.
    """
    sessions: list = []
    monkeypatch.setattr(agent_chat, "SessionLocal", _session_recording_factory(agent_db, sessions))

    tx_states: list[list[bool]] = []

    class TxProbeRequests(FakeRequests):
        def post(self, url, headers=None, json=None, timeout=None, params=None):
            if url == agent_chat.ANTHROPIC_API_URL:
                tx_states.append([s.in_transaction() for s in sessions])
            return super().post(url, headers=headers, json=json, timeout=timeout, params=params)

    fake = TxProbeRequests(
        [
            _anthropic_tool_use("get_weight_history", {"days": 7}),
            _anthropic_text("Вес 82.0 кг, тренд стабильный."),
        ],
        tool_payload={"status": "ok", "latest": {"weight_kg": 82.0}},
    )
    monkeypatch.setattr(agent_chat, "requests", fake)

    reply = agent_chat.ask_agent(895655, "что с весом за неделю?")

    assert reply
    assert tx_states, "Anthropic ни разу не вызвался — тест не проверил ничего"
    for call_no, snapshot in enumerate(tx_states):
        assert not any(snapshot), f"вызов Anthropic #{call_no}: сессия осталась в открытой транзакции"


def test_answer_returned_even_if_history_save_fails(agent_db, monkeypatch):
    """(#347) Сбой записи в agent_conversations не съедает уже сгенерированный
    (и оплаченный) ответ — текст всё равно доходит до пользователя.
    """
    from sqlalchemy.exc import OperationalError

    real_save = agent_chat._save_message

    def _failing_save(db, user_id, role, content, tool_use_id=None, source="botkinclaw"):
        if role == "assistant":
            raise OperationalError("INSERT INTO agent_conversations", {}, Exception("server closed the connection"))
        return real_save(db, user_id, role, content, tool_use_id=tool_use_id, source=source)

    monkeypatch.setattr(agent_chat, "_save_message", _failing_save)

    fake = FakeRequests([_anthropic_text("ЛФК начинай с изометрии, 2 подхода по 10 секунд.")])
    monkeypatch.setattr(agent_chat, "requests", fake)

    reply = agent_chat.ask_agent(895655, "как начать ЛФК для шеи?")

    assert "изометри" in reply
