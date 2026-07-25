"""
BotkinClaw — conversational AI-agent для @Botkin_md_bot (in-process handler).

Вызывается из `handlers/text.py` когда LLM router классифицирует сообщение как
`type=other` (т.е. не еда / добавка / АД и т.п. — свободный вопрос). Модуль
оборачивает Anthropic Messages API call с tools, используя:

- per-user system prompt из `users.agent_system_prompt`
- ~14 tools, которые HTTP+JWT-вызывают эндпоинты `webhook/agent_tools_api.py`
- история диалога из таблицы `agent_conversations` (HISTORY_WINDOW последних)

История архитектуры: см. [ADR-0002](docs/architecture/decisions/0002-rejecting-nanoclaw-for-simpler-agent.md)
— почему отказались от NanoClaw, и почему имя BotkinClaw (игра слов: бот сам
играет роль «контейнера» в JWT-контракте).
"""

from __future__ import annotations

import json
import os
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from typing import Any, Callable, Optional

import jwt as pyjwt
import requests

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config import get_settings
from database import SessionLocal
from database.models import User, log_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Выбор моделей и история откатов — config/models.py (env-переопределяемо).
# ⚠️ Sonnet 4.5 НЕ поддерживает output_config.effort → в fallback-ветке effort
# снимается (см. _post_with_overload_retry), иначе 400.
from config.models import AGENT_FALLBACK_MODEL as FALLBACK_MODEL
from config.models import AGENT_MODEL as MODEL

# effort=medium — документированный sweet spot Sonnet 4.6 для чата: дешевле и
# быстрее дефолтного high, без потери качества на разговорных задачах.
AGENT_EFFORT = "medium"
MAX_TOKENS = 4000  # 2000 обрезал развёрнутые многофакторные ответы (прецедент:
# разбор «алкоголь по всем диагнозам» Димы оборвался на полуслове). 4000 хватает
# на полный разбор с таблицей; короткие ответы лимит не трогает (бот сам краток).
MAX_TOOL_ITERATIONS = 6  # safety net against tool loops
HISTORY_WINDOW = 20  # last N messages from agent_conversations

# Tools API base URL — same container, FastAPI on 8081.
# When running inside healthvault_bot container, this is localhost:8081 directly.
TOOLS_API_BASE = "http://localhost:8081/api/agent"

# Единый TTL с webhook/jwt_auth.py (тот же env) — раньше тут было 24h против 1h
# у валидатора, политика противоречила сама себе. Токен регенерируется на каждый
# запрос пользователя, 1 часа хватает на самый длинный tool-loop с запасом.
JWT_TTL_HOURS = int(os.getenv("AGENT_JWT_TTL_HOURS", "1"))

# ---------------------------------------------------------------------------
# Tool schema (BotkinClaw tools — wrap webhook/agent_tools_api.py endpoints)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_user_profile",
        "description": "Базовый профиль: имя, возраст, рост, пол, когорта, pack_name. Обычно не нужен — контекст уже в system prompt.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_dashboard_summary",
        "description": "ГЛАВНЫЙ tool: сводка здоровья за последние 7 дней — средние шаги, пульс, активные ккал, ккал съеденные, последний вес+%жира. Также возвращает `dashboard_url` — прямая ссылка на персональный веб-дашборд пользователя. Используй для любых вопросов 'как мои дела/неделя/прогресс' и когда пользователь просит показать дашборд.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "meal_context",
        "description": (
            "Контекст для вопросов «что мне съесть / что ещё можно сейчас / что на ужин»: остаток "
            "КБЖУ на сегодня (target/consumed/remaining), съеденные макросы, ОГРАНИЧЕНИЯ-ДИАГНОЗЫ "
            "(constraints) и любимые продукты юзера — всё ОДНИМ вызовом. При таких вопросах зови "
            "ЭТОТ tool вместо нескольких отдельных. По результату дай СРАЗУ 2-3 конкретных варианта "
            "под остаток калорий и под constraints (диагнозы — критично: подагра, демпинг и т.п.); "
            "уточняющий вопрос («где ты», «что дома») задавай ТОЛЬКО если без него никак, не гоняй "
            "юзера по 3-4 репликам."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_meals",
        "description": (
            "Недавние приёмы пищи. days=1..90, по умолчанию 3. "
            "Для поиска по длинному периоду («ел ли я X за месяцы», «как часто пельмени») "
            "ставь большой days и compact=true — вернёт компактно (имена продуктов + калории), "
            "без раздувания контекста. days>14 авто-включает compact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 3},
                "compact": {
                    "type": "boolean",
                    "default": False,
                    "description": "Лёгкий формат (имена продуктов + калории) для поиска по длинному периоду.",
                },
            },
        },
    },
    {
        "name": "get_recent_glucose",
        "description": (
            "Глюкоза CGM (FreeStyle Libre 3): точки (ts/value mmol/L/trend) + сводка (avg, min, max, TIR%). "
            "Окно: либо hours=1..168 (последние N часов, по умолчанию 24), либо date='YYYY-MM-DD' "
            "(конкретный календарный день — для «глюкоза за вчера / за 17 июня»; приоритетнее hours). "
            "Точки прорежены равномерно по всему окну (downsampled=true если прорежено), "
            "min/max в stats — по ВСЕМ точкам. Для сопоставления еды с дневной кривой бери конкретный день через date. "
            "Для вопросов про сахар «какой сейчас / после еды / ночью / скачки». "
            "Точки в ответе = CGM подключён; пусто = нет свежих данных (НЕ значит «сенсора нет»). "
            "is_stale=true / refresh_skipped=true / большой last_point_age_min → данные несвежие, "
            "не выдавай за «сейчас/весь день» (см. правило свежести)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
                "date": {
                    "type": "string",
                    "description": "YYYY-MM-DD — конкретный день в TZ юзера; приоритетнее hours",
                },
            },
        },
    },
    {
        "name": "get_glucose_stats",
        "description": (
            "Сводная статистика глюкозы CGM за N дней: TIR% (время в диапазоне 3.9–10 mmol/L), "
            "среднее, разброс (std), min/max, % ниже/выше. days=1..90, по умолчанию 7. "
            "Для вопросов «как мой сахар за неделю/месяц», «какой TIR»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7},
            },
        },
    },
    {
        "name": "list_kb_keys",
        "description": (
            "Список реальных топ-уровневых ключей knowledge_base ЭТОГО пользователя "
            "(у разных людей схемы расходятся: у кого-то `echocardiogram`/`current_medications`, "
            "у кого-то `mrt`/`tumor_markers`, у кого-то `cardio`/`endoscopy`). "
            "ВСЕГДА зови это первым, если юзер спрашивает про специфичный вид обследования "
            "(ЭхоКГ, холтер, МРТ, КТ, операции, текущие препараты, диагнозы) и ты не уверен "
            "под каким именно ключом эти данные лежат. Для каждого ключа возвращает "
            "type (list/dict) и count — видно есть ли там данные или секция пустая."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_kb_value",
        "description": (
            "Значение из knowledge base по ключу (поддерживает dot-notation, например "
            "'blood_tests.0.values.cholesterol'). Распространённые ключи: 'blood_tests', "
            "'hormones', 'vitamins', 'ultrasound', 'ecg', 'echocardiogram', 'holter_ecg', "
            "'current_medications', 'medications', 'chronic_diagnoses', 'diagnoses', "
            "'operations', 'imaging', 'mrt', 'endoscopy', 'cardio'. ВАЖНО: если не уверен "
            "какой именно ключ есть у этого юзера — сначала позови `list_kb_keys`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "get_open_questions",
        "description": (
            "Открытые клинические вопросы и красные флаги пользователя из его KB. "
            "ОБЯЗАТЕЛЬНО зови этот tool в начале ЛЮБОГО медицинского диалога — "
            "разбор анализов, диагнозов, препаратов, симптомов, планирование "
            "обследований. Это «висящие» вопросы которые ждут решения врача или "
            "повторного анализа: пропущенные маркеры, не закрытые followup'ы, "
            "клинические red flags. Прецедент 25.05.2026: папа Александра "
            "спрашивал «какие у меня диагнозы», бот ответил списком из 8 — но "
            "не упомянул что K+/Mg+/ТТГ ни разу не сдавались при QTc 0.60 (это "
            "висело в open_questions). Цель этого tool — чтобы такое не повторялось. "
            "Если в ответе questions=[] и source='not-tracked' — у пользователя "
            "ещё нет ведённого списка, это нормально, просто продолжай. "
            "Когда questions есть — упомяни ХОТЯ БЫ ОДИН релевантный текущему "
            "вопросу пункт в своём ответе, даже если юзер про него не спрашивал. "
            "Не вываливай весь список — отбери 1-3 самых релевантных."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_bp",
        "description": "Измерения артериального давления за последние N дней (1-90, по умолчанию 14). Возвращает каждый замер + статистику: средние систолика/диастолика, max/min, % замеров выше 140/90 (порог гипертонии 1 ст).",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14}},
        },
    },
    {
        "name": "get_recent_sleep",
        "description": (
            "Сон за последние N дней (1-90, по умолчанию 14). Возвращает каждую ночь + средняя "
            "продолжительность, качество, deep/REM минуты, и поле `latest_available_date` — "
            "последняя ночь, по которой ЕСТЬ данные.\n\n"
            "ЕСЛИ за прошедшую ночь данных нет (count=0 или последняя ночь в items не сегодняшняя/"
            "вчерашняя) — НЕ говори расплывчато «подожди 10-15 минут». Скажи честно: назови "
            "`latest_available_date` («последние данные о сне — за DATE»), объясни, что данные с "
            "часов появляются у Garmin с задержкой и подтягиваются автоматически каждые ~30 минут, "
            "и предложи заглянуть позже или прислать `/sync`. Не выдумывай конкретные часы/минуты."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14}},
        },
    },
    {
        "name": "get_recent_supplements",
        "description": (
            "ЕДИНСТВЕННЫЙ источник для вопросов 'какие витамины/добавки я "
            "принимаю', 'сколько раз в неделю пил магний', 'когда последний "
            "раз принимал креатин', 'насколько я придерживаюсь схемы'. "
            "Возвращает агрегацию по каждой добавке за период: days_taken, "
            "total_intakes (несколько раз в день — норма), adherence_pct "
            "(% дней приёма от периода), last_date, last_dosage. "
            "Период по умолчанию 30 дней. НЕ используй контекст из system "
            "prompt — там может быть устаревший список, реальный лог здесь."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 180, "default": 30}},
        },
    },
    {
        "name": "get_supplement_daily_log",
        "description": (
            "Для КОРРЕЛЯЦИОННОГО анализа приёма добавки с метриками по дням "
            "(напр. «влияет ли магний на качество сна», «алкоголь и HRV»). "
            "В отличие от get_recent_supplements (только агрегаты), возвращает "
            "для каждой добавки список ДАТ приёма (taken_dates) за период + "
            "границы окна [start_date, end_date]. День без даты в списке = "
            "не принимал → можно сопоставлять с get_recent_sleep / get_recent_bp "
            "и т.п. по дням. Опц. параметр supplement — фильтр по одному "
            "препарату (подстрока имени). Период по умолчанию 30 дней."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 180, "default": 30},
                "supplement": {"type": "string", "description": "опц. фильтр по имени добавки (подстрока)"},
            },
        },
    },
    {
        "name": "get_recent_biomarkers",
        "description": (
            "ЕДИНСТВЕННЫЙ источник для вопросов про анализы крови, гормоны, "
            "витамины, биомаркеры. Возвращает строки blood_tests из Postgres: "
            "дата + тип + values (dict со значениями маркеров типа vitamin_d, "
            "LDL, HDL, glucose, HbA1c, testosterone, TSH, ferritin, ApoB и т.п.). "
            "Используй ВСЕГДА когда юзер спрашивает про любые лабораторные показатели: "
            "'какой у меня витамин Д', 'как менялся холестерин', 'какие гормоны сдавал', "
            "'покажи динамику ЛПНП', 'какие анализы за последний год'. "
            "limit по умолчанию 20 (история на год), увеличь до 50 для 'всё что есть'. "
            "НЕ ИСПОЛЬЗУЙ get_kb_value для анализов — оно ничего не вернёт."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
        },
    },
    {
        "name": "get_latest_biomarkers",
        "description": (
            "Актуальное состояние всех биомаркеров: последнее значение каждого маркера "
            "с полями days_ago, threshold_days, is_stale и stale_label. "
            "Используй вместо get_recent_biomarkers когда нужно текущее значение маркера "
            "(«какой у меня витамин D?», «мои анализы в норме?»). "
            "Если is_stale=true — ОБЯЗАТЕЛЬНО упомяни давность: "
            "'Последний анализ от [дата] — рекомендую обновить (прошло [N] мес)'. "
            "Используй get_recent_biomarkers для исторических трендов."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_phenoage",
        "description": (
            "Биологический возраст по формуле Levine 2018 (Aging Cell). "
            "Возвращает bio_age, chronological_age, delta_years, и 9 маркеров "
            "(albumin, creatinine, glucose, hs_CRP, lymphocytes, MCV, RDW, ALP, WBC) "
            "с пометкой 'younger'/'older' vs NHANES median. Используй для "
            "вопросов 'какой мой биологический возраст', 'PhenoAge', "
            "'на сколько я моложе/старше паспорта', 'состав панели биовозраста'. "
            "Если каких-то маркеров не хватает — вернёт error со списком."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_workouts",
        "description": (
            "Анализ тренировок по канонам Seiler 80/20 / Attia Z2 / норвежский 4x4. "
            "Возвращает: by_type (счётчик по Garmin type — running, walking, "
            "strength_training, yoga и т.п.), Z2 min/week, HIIT min/week, A:C "
            "load ratio (sweet spot 0.8-1.3), polarized distribution Z1+Z2/Z3/Z4+Z5 %, "
            "extremes_by_type (рекорды на каждый тип — longest_by_duration и "
            "longest_by_distance, считаются по ВСЕМ тренировкам в окне), "
            "список последних 15 тренировок с type+name+distance_km+duration_min. "
            "Используй для 'сколько раз я бегал', 'сколько Z2 в неделю', "
            "'каков мой A:C ratio', 'правильное ли распределение зон'. "
            "Для вопросов 'самая длинная пробежка / самая дальняя пробежка' — "
            "ВСЕГДА смотри в extremes_by_type[бег|ходьба|...].longest_by_distance "
            "(или longest_by_duration), НЕ в items[] — items обрезан до 15 свежих "
            "и редкие длинные сессии туда не попадают. "
            "ВАЖНО: для классификации (бег/ходьба/силовая) ВСЕГДА смотри поле "
            "`type` (Garmin classification), а НЕ `name` (user-set route label "
            "типа 'Москва - База' который может быть бегом ИЛИ ходьбой). "
            "Используй by_type для прямых вопросов типа 'сколько раз я бегал'. "
            "По умолчанию days=30; для вопросов 'за год' / 'в этом году' "
            "ставь days=180 (максимум). "
            "Multi-user: owner-cohort использует file-source (rich data со Z2/zones/load), "
            "остальные пользователи — DB-fallback (только базовые поля type/duration/distance), "
            "поле `source` в ответе показывает откуда взялись данные."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 180, "default": 30}},
        },
    },
    {
        "name": "get_menstrual_data",
        "description": (
            "Данные менструального цикла из Apple Health (таблица menstrual_log). "
            "Возвращает: список дней с flow (none/light/medium/heavy/spotting), "
            "вычисленные начала циклов (period_starts), длины циклов в днях, "
            "статистику: avg_cycle_days, variation_days, total_periods. "
            "Используй для вопросов 'насколько ровный у меня цикл', "
            "'какая длина цикла', 'регулярен ли цикл', 'когда началась последняя менструация'. "
            "По умолчанию months=6. Передавай months=12 для годовой статистики."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"months": {"type": "integer", "minimum": 1, "maximum": 24, "default": 6}},
        },
    },
    {
        "name": "get_recent_trends",
        "description": (
            "Per-day тренды HRV, Body Battery, Stress, Steps, RHR, Sleep + "
            "флаг alcohol (был ли в этот день алкоголь, из nutrition_log) "
            "за N дней (по умолчанию 14, до 180). В отличие от get_dashboard_summary "
            "который даёт только AVG за 7 дней — тут видно динамику ДЕНЬ ЗА ДНЁМ. "
            "Используй для 'падает ли мой HRV', 'когда у меня был стресс', "
            "'какой у меня body battery утром', 'как менялся пульс покоя', "
            "а также для корреляций 'алкоголь → HRV/стресс следующего дня'. "
            "Возвращает items (per-day, поле alcohol:bool) + stats (avg/min/max + alcohol_days). "
            "Для КОРРЕЛЯЦИЙ и ГРАФИКОВ на длинном окне ставь full_series=true "
            "(вернёт ВСЕ точки окна, а не последние 30) и days=90..180."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 180, "default": 14},
                "full_series": {
                    "type": "boolean",
                    "default": False,
                    "description": "Вернуть все точки окна (для корреляций/графиков). Дефолт false (последние 30).",
                },
            },
        },
    },
    {
        "name": "get_weight_history",
        "description": (
            "История веса и состава тела (жир %, мышечная масса, висцеральный жир, BMI). "
            "Источник — таблица `weights` (с 2015 у долгих пользователей). "
            "Возвращает: latest (текущий замер), all_time (рекорды за всю историю — "
            "min/max веса и жира с датами), и опционально in_window (рекорды в окне days). "
            "Используй для вопросов 'самый низкий жир за всё время', 'минимальный вес', "
            "'как изменился жир за полгода', 'когда был в лучшей форме'. "
            "\n\n"
            "Параметр `series=true` ДОБАВЛЯЕТ поле `points` с массивом всех "
            "ежедневных замеров {date, weight_kg, body_fat_pct} в окне. "
            "Используй когда собираешься рисовать график (render_chart / "
            "render_report). БЕЗ series ответ короткий — это нормально для "
            "текстовых вопросов."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 7,
                    "maximum": 365,
                    "description": "Окно в днях. Без параметра — only all_time.",
                },
                "series": {
                    "type": "boolean",
                    "default": False,
                    "description": "Вернуть все точки в `points` для рисования графика. Дефолт false.",
                },
            },
        },
    },
    {
        "name": "get_body_measurements",
        "description": (
            "Антропометрия: талия, шея, бёдра, грудь, бедро, бицепс (см). "
            "Источник — ручной ввод. Возвращает latest, extremes (min/max) "
            "по каждой метрике, и тренд талии (waist) за последние 6 замеров. "
            "Используй для 'какая у меня талия сейчас', 'как изменилась талия за полгода', "
            "'сравни мою фигуру до и после'. Талия — главная метрика метаболического "
            "здоровья (важнее BMI для ССЗ-риска)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_user_settings",
        "description": (
            "Настройки и цели пользователя: target_weight_kg + date (цель веса), "
            "supplements_regimen (JSONB список ежедневных добавок с дозами и слотами "
            "morning_with/evening), calorie_goal_pct (дефицит/профицит), BMR-источник, "
            "reminders (напоминания о добавках), profile (sex/height/birth_date/timezone). "
            "Используй для 'какие у меня цели', 'что я регулярно принимаю', 'какой "
            "дефицит', 'когда напоминания'. Полезно сверять `supplements_log` (что "
            "реально принял) против `supplements_regimen` (что планировал)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_profile_questionnaire",
        "description": (
            "Анкета пользователя из users table: sex, height_cm, birth_date (+ возраст), "
            "timezone, smoking_status (never/former/current/occasional), kb_status "
            "(приватность knowledge_base: shared = доступно AI, private = только юзеру), "
            "garmin_connected, pack_name, превью agent_system_prompt (500 симв). "
            "Используй для 'что я указывал в анкете', 'мои настройки приватности', "
            "'подключён ли Garmin', 'какой у меня возраст в системе'. "
            "Также вызывай ПЕРЕД update_profile_questionnaire чтобы показать что меняется."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_profile_questionnaire",
        "description": (
            "Изменить анкетные поля в users table. Все поля опциональны — обновятся "
            "только переданные. ПЕРЕД использованием — обязательно подтверди с юзером "
            "что именно меняешь (особенно agent_system_prompt — длинная строка, влияет "
            "на поведение агента). Допустимые значения: smoking_status ∈ "
            "{never,former,current,occasional}, kb_status ∈ {shared,private}, sex ∈ "
            "{male,female,other}, birth_date YYYY-MM-DD."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sex": {"type": "string", "enum": ["male", "female", "other"]},
                "height_cm": {"type": "integer", "minimum": 100, "maximum": 250},
                "birth_date": {"type": "string", "description": "YYYY-MM-DD"},
                "timezone": {"type": "string"},
                "smoking_status": {"type": "string", "enum": ["never", "former", "current", "occasional"]},
                "kb_status": {"type": "string", "enum": ["shared", "private"]},
                "pack_name": {"type": "string"},
                "agent_system_prompt": {
                    "type": "string",
                    "description": "Полная замена промпта. Длинная (5-10К). Опасное поле.",
                },
            },
        },
    },
    {
        "name": "update_user_settings",
        "description": (
            "Изменить настройки в user_settings table: target_weight_kg + target_weight_date "
            "(цель веса), calorie_goal_pct (-15 = дефицит 15%), bmr_override/bmr_source, "
            "supplements (ПОЛНАЯ замена списка добавок — каждая {name, dose?, slot?}, slots: "
            "morning_before / morning_with / evening), reminders. Все поля опциональны. "
            "ВАЖНО: чтобы добавить ОДНУ добавку — сначала get_user_settings, модифицируй "
            "список локально, потом update с полным новым списком. ПЕРЕД изменением — "
            "подтверди с юзером."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_weight_kg": {"type": "number", "minimum": 30, "maximum": 300},
                "target_weight_date": {"type": "string", "description": "YYYY-MM-DD"},
                "calorie_goal_pct": {"type": "integer", "minimum": -50, "maximum": 50},
                "bmr_override": {"type": "integer", "minimum": 500, "maximum": 5000},
                "bmr_source": {"type": "string", "enum": ["auto", "override", "fixed"]},
                "supplement_reminders_enabled": {"type": "boolean"},
                "supplement_reminder_time": {"type": "string", "description": "HH:MM"},
                "show_calorie_budget_bar": {"type": "boolean"},
                "supplements": {
                    "type": "array",
                    "description": "Полный новый список добавок (заменяет все).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "dose": {"type": "string"},
                            "slot": {"type": "string", "enum": ["morning_before", "morning_with", "evening"]},
                        },
                        "required": ["name"],
                    },
                },
            },
        },
    },
    {
        "name": "get_indoor_air",
        "description": (
            "Воздух дома: CO2 ppm, температура, влажность, шум. Источник — Netatmo "
            "Healthy Home Coach. Только для owner-cohort (Alex). Возвращает latest "
            "(текущий замер) + history по комнатам (агрегаты за N дней). "
            "Используй для 'CO2 в спальне', 'духота', 'температура дома'. "
            "Норма CO2 < 1000 ppm, >1400 — критично для сна/концентрации."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 60, "default": 7}},
        },
    },
    {
        "name": "get_outdoor_weather",
        "description": (
            "Погода снаружи (Open-Meteo, Москва): температура max/min/средняя, "
            "давление, влажность, UV, осадки, словесное описание. Без параметра — "
            "последний день; с date='YYYY-MM-DD' — конкретный. Используй для "
            "'какая погода сегодня', 'давление', 'был ли дождь', анализа корреляций "
            "(например 'влияет ли давление на пульс покоя')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD (optional)"}},
        },
    },
    {
        "name": "get_day_summary",
        "description": (
            "Точечная сводка за конкретный день (date='YYYY-MM-DD'): ккал/БЖУ, был ли "
            "воркаут, часов сна, вес, АД. Источник — таблица daily_summaries (агрегаты "
            "от ночного sync). В отличие от get_dashboard_summary который даёт AVG за 7 дней, "
            "тут конкретный день. Используй для 'что у меня было 14 марта', 'сравни этот "
            "понедельник с прошлым'. Если данных за день нет — вернёт no_data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["date"],
        },
    },
    {
        "name": "log_meal_text",
        "description": (
            "Залогировать приём пищи из текстового описания. ИСПОЛЬЗОВАТЬ ТОЛЬКО "
            "если юзер ЯВНО просит 'запиши' или 'залогируй'. Не пытайся логировать "
            "каждое упоминание еды.\n\n"
            "Если юзер указал ДЕНЬ приёма («вчера», «позавчера», «29 мая», «в "
            "понедельник») — вычисли конкретную дату и передай её в `date` "
            "(YYYY-MM-DD), опираясь на сегодняшнее число. Без указания дня `date` "
            "НЕ передавай (запишется на сегодня). После записи ОБЯЗАТЕЛЬНО назови "
            "дату в ответе, если она не сегодняшняя («записал на 29 мая»).\n\n"
            "When handling addendum messages ('забыл добавить', 'забыл упомянуть', etc.): "
            "first call get_recent_meals(days=1) to find the most recent meal slot, "
            "then use that same slot parameter when calling log_meal_text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "date": {
                    "type": "string",
                    "description": "Дата приёма YYYY-MM-DD. Только если юзер указал день; иначе не передавать (=сегодня).",
                },
                "slot": {
                    "type": "string",
                    "enum": ["breakfast", "lunch", "dinner", "snack"],
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "edit_meal",
        "description": (
            "Изменить УЖЕ залогированный приём пищи: перенести на другой день (new_date), "
            "сменить слот/время (new_slot), переименовать (new_name). "
            "meal_id бери из get_recent_meals (поле id). "
            "ИСПОЛЬЗОВАТЬ когда юзер просит «перенеси/исправь/измени/это был не обед а ужин» "
            "про уже записанную еду. Передавай только те поля, что меняются."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_id": {"type": "integer", "description": "id записи из get_recent_meals"},
                "new_date": {"type": "string", "description": "Новая дата YYYY-MM-DD (перенос на другой день)"},
                "new_slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                "new_name": {"type": "string", "description": "Новое название приёма"},
            },
            "required": ["meal_id"],
        },
    },
    {
        "name": "delete_meal",
        "description": (
            "Удалить залогированный приём пищи по meal_id (из get_recent_meals, поле id). "
            "ИСПОЛЬЗОВАТЬ когда юзер просит «удали/убери» запись о еде. "
            "Если нужно перенести на другой день — используй edit_meal с new_date, не delete+log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"meal_id": {"type": "integer", "description": "id записи из get_recent_meals"}},
            "required": ["meal_id"],
        },
    },
    {
        "name": "log_bp",
        "description": "Залогировать измерение давления.",
        "input_schema": {
            "type": "object",
            "properties": {
                "systolic": {"type": "integer"},
                "diastolic": {"type": "integer"},
                "pulse": {"type": "integer"},
            },
            "required": ["systolic", "diastolic"],
        },
    },
    {
        "name": "log_supplement",
        "description": (
            "Залогировать приём добавки/витамина. Если та же добавка уже логировалась "
            "в этот день, вернётся status=duplicate_warning и запись НЕ создастся — "
            "уточни у пользователя, реально ли это повторный приём, и при подтверждении "
            "повтори вызов с force=true. Описание схемы приёма («принимаю утром…») — "
            "НЕ повод логировать: логируй только фактический приём."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplement_name": {"type": "string"},
                "dosage": {"type": "string"},
                "force": {
                    "type": "boolean",
                    "description": "true — записать несмотря на duplicate_warning (подтверждённый повторный приём)",
                },
            },
            "required": ["supplement_name"],
        },
    },
    {
        "name": "render_chart",
        "description": (
            "Универсальный рендер графика через QuickChart → Telegram. Для всего что "
            "не покрывает render_report (тот быстрее для типовых случаев).\n\n"
            "КОГДА: «нарисуй», «график», «инфографика», «сравни X и Y», «корреляция».\n\n"
            "ФОРМАТ spec — Chart.js v4. Пиши МИНИМАЛЬНО, только данные и тип; "
            "стиль (цвета, шрифты, легенду, сетку, размеры) сервер ставит сам "
            "из Botkin-палитры. НЕ заполняй borderColor / backgroundColor / "
            "tension / pointRadius — сервер заполнит. Заполняй ТОЛЬКО:\n"
            "  • type ('line'|'bar'|'scatter'|'doughnut'|'pie'|'radar'|'polarArea')\n"
            "  • data.labels, data.datasets[].label, data.datasets[].data\n"
            "  • options.plugins.title.text — заголовок графика (обязательно)\n"
            "  • options.scales — только если нужны нестандартные оси (multi-axis: yAxisID на datasets + scales.y/y1)\n\n"
            "Минимальный пример:\n"
            "{type:'line', data:{labels:['янв','фев','мар'], datasets:[{label:'Вес', data:[82,81,80]}]}, "
            "options:{plugins:{title:{text:'Вес за квартал'}}}}\n\n"
            "Кириллица в labels/title работает нормально. После render — короткий "
            "комментарий (1-3 предложения), не пересказывай содержимое картинки."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chart": {
                    "type": "object",
                    "description": "Chart.js v4 config: {type, data, options}",
                },
                "caption": {
                    "type": "string",
                    "description": "Подпись под фото в Telegram (1 строка, без markdown).",
                },
                "width": {"type": "integer", "default": 600, "minimum": 200, "maximum": 1600},
                "height": {"type": "integer", "default": 400, "minimum": 200, "maximum": 1200},
            },
            "required": ["chart"],
        },
    },
    {
        "name": "render_report",
        "description": (
            "Сгенерировать и ОТПРАВИТЬ юзеру в Telegram PNG-инфографику. "
            "Tool сам делает sendPhoto — тебе НЕ надо ничего возвращать в тексте, "
            "Telegram уже получил картинку к моменту когда ты увидишь результат. "
            "После использования напиши КОРОТКИЙ комментарий-резюме к картинке "
            "(2-4 предложения), не пытайся пересказать всё что на ней. "
            "\n\n"
            "КОГДА ИСПОЛЬЗОВАТЬ: ВМЕСТО рисования markdown-таблицы с динамикой "
            "лабораторных значений. Триггеры: «динамика анализов», «разбери "
            "анализы», «график X», «нарисуй», «как менялся X», «покажи отчёт», "
            "«инфографика», «сделай разбор для врача». "
            "\n\n"
            "Два режима:\n"
            "  • report_type='biomarker_dynamics' (DEFAULT) — общая панель 2×3 "
            "    с 6 ключевыми маркерами (липиды + метаболизм + печень и т.п.). "
            "    Зови когда юзер просит общую картину / разбор / для врача.\n"
            "  • report_type='single_biomarker' + marker='<имя>' — большой график "
            "    одного маркера. Зови когда юзер спрашивает про конкретный "
            "    показатель: «график витамина Д», «как менялся ЛПНП», «динамика "
            "    глюкозы». Параметр marker принимает русские названия (витамин Д, "
            "    глюкоза, ЛПНП, холестерин, гликированный, ферритин, тестостерон) "
            "    и латинские (LDL, HDL, HbA1c, ALT, TSH, vitamin_d, и т.п.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report_type": {
                    "type": "string",
                    "enum": ["biomarker_dynamics", "single_biomarker"],
                    "default": "biomarker_dynamics",
                },
                "marker": {
                    "type": "string",
                    "description": ("Имя биомаркера для режима single_biomarker. Игнорируется для biomarker_dynamics."),
                },
            },
        },
    },
    {
        "name": "generate_doctor_report",
        "description": (
            "Сгенерировать и ОТПРАВИТЬ пользователю PDF-отчёт о здоровье для врача. "
            "Tool сам шлёт PDF Telegram-документом — тебе НЕ надо ничего прикладывать, "
            "файл уже в чате к моменту результата. После вызова напиши КОРОТКОЕ "
            "подтверждение (1-2 предложения): отчёт отправлен, его можно переслать врачу.\n\n"
            "КОГДА ИСПОЛЬЗОВАТЬ: пользователь просит «отчёт для врача», «сводку для доктора», "
            "«выгрузи мои данные для приёма», «PDF для врача». Отчёт — секции в клиническом "
            "порядке (проблемы, аллергии, лекарства, результаты анализов, витальные, образ жизни). "
            "Это НЕ график динамики — для инфографики используй render_report.\n\n"
            "ЯЗЫК: если пользователь просит отчёт на английском (или пишет по-английски) — "
            "передай language='en'; для русского — 'ru'. Не указан — язык по умолчанию."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["ru", "en"],
                    "description": "Язык отчёта: 'en' если пользователь просит на английском, иначе 'ru'.",
                },
            },
        },
    },
    {
        "name": "add_agent_correction",
        "description": (
            "Сохранить поправку или новый факт в KB пользователя. "
            "Вызывай СРАЗУ когда пользователь исправляет факт или сообщает новые данные — "
            "дату операции, диагноз, аллергию, новый препарат, любую другую медицинскую деталь. "
            "Ключ — короткое snake_case имя (surgery_year, diabetes_status, new_medication). "
            "Данные сохраняются в секцию agent_corrections KB и будут доступны при следующем разговоре."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Уникальный ключ факта (snake_case, ≤100 символов)"},
                "value": {"type": "string", "description": "Значение (≤2000 символов)"},
                "reason": {"type": "string", "description": "Откуда факт — цитата или пересказ слов пользователя"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "flag_for_devs",
        "description": (
            "Зафиксировать пожелание/багрепорт пользователя для разработчиков. "
            "Вызывай, когда: не смог закрыть запрос (нет тула/данных), пользователь "
            "недоволен/переспрашивает, или прямо указал на баг/пожелание."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["bug", "feature", "question"],
                    "description": "Тип: баг / пожелание фичи / вопрос.",
                },
                "user_msg": {"type": "string", "description": "Суть запроса словами пользователя."},
                "agent_note": {"type": "string", "description": "Твоя короткая заметка для разработчиков."},
            },
            "required": ["category", "user_msg"],
        },
    },
    {
        "name": "list_feedback",
        "description": (
            "АДМИН-ТУЛ (#269): показать записи инбокса обратной связи для триажа. "
            "Возвращает структурный список (id/kind/status/priority/text/agent_note/…). "
            "По умолчанию status='new'; status='all' — все статусы."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Фильтр статуса: new/triaged/in_progress/done/wontfix/duplicate или 'all'.",
                },
                "limit": {"type": "integer", "description": "Сколько записей (макс 100, дефолт 20)."},
            },
            "required": [],
        },
    },
    {
        "name": "triage_feedback",
        "description": (
            "АДМИН-ТУЛ (#269): триаж записи инбокса — сменить статус/приоритет/привязать "
            "GitHub-issue. Частичное обновление: передавай только меняемые поля. "
            "Возвращает обновлённую запись.\n"
            "Фаза 3 (#188): при переводе в done/wontfix бот САМ шлёт автору уведомление о "
            "разборе (один раз, guard по notified_at). Для вопроса (kind=question) передай "
            "`notify_text` — твой человеческий ответ уйдёт автору напрямую и закроет обращение. "
            "В ответе поля `notified` и `notify_skipped` (opt_out/already_notified/send_failed)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feedback_id": {"type": "integer", "description": "id записи из list_feedback."},
                "status": {
                    "type": "string",
                    "enum": ["new", "triaged", "in_progress", "done", "wontfix", "duplicate"],
                    "description": "Новый статус.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["P0", "P1", "P2", "P3"],
                    "description": "Приоритет.",
                },
                "github_issue": {"type": "string", "description": "Номер GitHub-issue (напр. '300')."},
                "notify_text": {
                    "type": "string",
                    "description": (
                        "Явный ответ автору (перекрывает авто-текст, уходит при любом статусе). "
                        "Используй для kind=question, чтобы ответить человеку по существу."
                    ),
                },
            },
            "required": ["feedback_id"],
        },
    },
]

# ---------------------------------------------------------------------------
# JWT generation (matches webhook/jwt_auth.py contract)
# ---------------------------------------------------------------------------


def agent_id_for(user: User) -> str:
    """Deterministic agent identifier embedded in JWT payload.

    BotkinClaw (in-process agent) — бот сам играет роль "контейнера", отдельных
    per-user контейнеров не существует. Если `users.container_id` уже выставлен
    (исторические значения вроде `in-process-andrey`) — используем его;
    иначе деривируем из telegram_id. И generate_jwt, и validate_jwt используют
    эту же функцию, поэтому подпись всегда сходится с проверкой.

    Legacy: до удаления NanoClaw (см. ADR-0002) `container_id` указывал на
    реальный onecli-контейнер. После выпила NanoClaw поле осталось как
    rudiment — может быть пустым для новых пользователей.
    """
    return user.container_id or f"botkinclaw-{user.telegram_id}"


def _generate_jwt(user: User) -> str:
    """Short-lived JWT для BotkinClaw tool calls."""
    if not user.jwt_secret:
        raise RuntimeError(f"User {user.telegram_id} missing jwt_secret; cannot generate agent JWT")
    payload = {
        "user_id": user.telegram_id,
        "container_id": agent_id_for(user),
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_TTL_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return pyjwt.encode(payload, user.jwt_secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _call_tool(name: str, args: dict, token: str) -> str:
    """Synchronous HTTP call to tools API. Returns raw response body as string."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        if name == "get_user_profile":
            r = requests.get(f"{TOOLS_API_BASE}/user_profile", headers=headers, timeout=10)
        elif name == "get_dashboard_summary":
            r = requests.get(f"{TOOLS_API_BASE}/dashboard_summary", headers=headers, timeout=15)
        elif name == "meal_context":
            r = requests.get(f"{TOOLS_API_BASE}/meal_context", headers=headers, timeout=15)
        elif name == "get_recent_meals":
            days = int(args.get("days", 3))
            params = {"days": days}
            if args.get("compact"):
                params["compact"] = "true"
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_meals",
                params=params,
                headers=headers,
                timeout=15,
            )
        elif name == "list_kb_keys":
            r = requests.get(
                f"{TOOLS_API_BASE}/list_kb_keys",
                headers=headers,
                timeout=10,
            )
        elif name == "get_kb_value":
            r = requests.get(
                f"{TOOLS_API_BASE}/kb_value",
                params={"key": args["key"]},
                headers=headers,
                timeout=10,
            )
        elif name == "get_open_questions":
            r = requests.get(
                f"{TOOLS_API_BASE}/open_questions",
                headers=headers,
                timeout=10,
            )
        elif name == "get_recent_bp":
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_bp",
                params={"days": int(args.get("days", 14))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_recent_sleep":
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_sleep",
                params={"days": int(args.get("days", 14))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_recent_glucose":
            glucose_params: dict = {}
            if args.get("date"):
                glucose_params["date"] = str(args["date"])
            else:
                glucose_params["hours"] = int(args.get("hours", 24))
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_glucose",
                params=glucose_params,
                headers=headers,
                timeout=15,
            )
        elif name == "get_glucose_stats":
            r = requests.get(
                f"{TOOLS_API_BASE}/glucose_stats",
                params={"days": int(args.get("days", 7))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_recent_supplements":
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_supplements",
                params={"days": int(args.get("days", 30))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_supplement_daily_log":
            params = {"days": int(args.get("days", 30))}
            if args.get("supplement"):
                params["supplement"] = str(args["supplement"])
            r = requests.get(
                f"{TOOLS_API_BASE}/supplement_daily_log",
                params=params,
                headers=headers,
                timeout=15,
            )
        elif name == "get_recent_biomarkers":
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_biomarkers",
                params={"limit": int(args.get("limit", 20))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_latest_biomarkers":
            r = requests.get(f"{TOOLS_API_BASE}/latest_biomarkers", headers=headers, timeout=15)
        elif name == "get_phenoage":
            r = requests.get(f"{TOOLS_API_BASE}/phenoage", headers=headers, timeout=15)
        elif name == "get_recent_workouts":
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_workouts",
                params={"days": int(args.get("days", 30))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_menstrual_data":
            r = requests.get(
                f"{TOOLS_API_BASE}/menstrual_data",
                params={"months": int(args.get("months", 6))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_recent_trends":
            r = requests.get(
                f"{TOOLS_API_BASE}/recent_trends",
                params={
                    "days": int(args.get("days", 14)),
                    "full_series": bool(args.get("full_series", False)),
                },
                headers=headers,
                timeout=20,
            )
        elif name == "get_weight_history":
            params: dict[str, Any] = {}
            if "days" in args and args["days"] is not None:
                params["days"] = int(args["days"])
            if args.get("series"):
                params["series"] = "true"
            r = requests.get(
                f"{TOOLS_API_BASE}/weight_history",
                params=params,
                headers=headers,
                timeout=15,
            )
        elif name == "get_body_measurements":
            r = requests.get(f"{TOOLS_API_BASE}/body_measurements", headers=headers, timeout=15)
        elif name == "get_user_settings":
            r = requests.get(f"{TOOLS_API_BASE}/user_settings", headers=headers, timeout=10)
        elif name == "get_profile_questionnaire":
            r = requests.get(f"{TOOLS_API_BASE}/profile_questionnaire", headers=headers, timeout=10)
        elif name == "update_profile_questionnaire":
            r = requests.post(
                f"{TOOLS_API_BASE}/update_profile_questionnaire",
                json=args,
                headers=headers,
                timeout=15,
            )
        elif name == "update_user_settings":
            r = requests.post(
                f"{TOOLS_API_BASE}/update_user_settings",
                json=args,
                headers=headers,
                timeout=15,
            )
        elif name == "get_indoor_air":
            r = requests.get(
                f"{TOOLS_API_BASE}/indoor_air",
                params={"days": int(args.get("days", 7))},
                headers=headers,
                timeout=15,
            )
        elif name == "get_outdoor_weather":
            weather_params: dict[str, Any] = {}
            if args.get("date"):
                weather_params["date"] = args["date"]
            r = requests.get(
                f"{TOOLS_API_BASE}/outdoor_weather",
                params=weather_params,
                headers=headers,
                timeout=10,
            )
        elif name == "get_day_summary":
            r = requests.get(
                f"{TOOLS_API_BASE}/day_summary",
                params={"date": args["date"]},
                headers=headers,
                timeout=10,
            )
        elif name == "log_meal_text":
            r = requests.post(f"{TOOLS_API_BASE}/log_meal_text", json=args, headers=headers, timeout=30)
        elif name == "edit_meal":
            r = requests.post(f"{TOOLS_API_BASE}/edit_meal", json=args, headers=headers, timeout=10)
        elif name == "delete_meal":
            r = requests.post(f"{TOOLS_API_BASE}/delete_meal", json=args, headers=headers, timeout=10)
        elif name == "log_bp":
            r = requests.post(f"{TOOLS_API_BASE}/log_bp", json=args, headers=headers, timeout=10)
        elif name == "log_supplement":
            r = requests.post(f"{TOOLS_API_BASE}/log_supplement", json=args, headers=headers, timeout=10)
        elif name == "render_report":
            # Side-effect tool — генерит PNG и шлёт юзеру sendPhoto.
            # Возвращает только статус (не саму картинку), чтобы агент
            # дальше отвечал коротким текстом-комментарием к картинке.
            payload = {"report_type": args.get("report_type", "biomarker_dynamics")}
            if "marker" in args:
                payload["marker"] = args["marker"]
            r = requests.post(
                f"{TOOLS_API_BASE}/render_report",
                json=payload,
                headers=headers,
                timeout=30,
            )
        elif name == "generate_doctor_report":
            # Side-effect tool — генерит PDF-отчёт для врача и шлёт sendDocument
            # (общий helper с кнопкой мини-аппа, #290/#291). language — #300.
            r = requests.post(
                f"{TOOLS_API_BASE}/doctor_report",
                headers=headers,
                json={"language": args.get("language")},
                timeout=60,
            )
        elif name == "render_chart":
            # Универсальный рендер через QuickChart.io. Side-effect — sendPhoto.
            r = requests.post(
                f"{TOOLS_API_BASE}/render_chart",
                json={
                    "chart": args.get("chart") or {},
                    "caption": args.get("caption"),
                    "width": args.get("width", 600),
                    "height": args.get("height", 400),
                },
                headers=headers,
                timeout=30,
            )
        elif name == "add_agent_correction":
            r = requests.post(
                f"{TOOLS_API_BASE}/add_agent_correction",
                json={
                    "key": args.get("key", ""),
                    "value": args.get("value", ""),
                    "reason": args.get("reason", ""),
                },
                headers=headers,
                timeout=10,
            )
        elif name == "flag_for_devs":
            r = requests.post(
                f"{TOOLS_API_BASE}/flag_for_devs",
                headers=headers,
                json={
                    "category": args.get("category", "question"),
                    "user_msg": args.get("user_msg", ""),
                    "agent_note": args.get("agent_note"),
                },
                timeout=10,
            )
        elif name == "list_feedback":
            r = requests.post(
                f"{TOOLS_API_BASE}/list_feedback",
                headers=headers,
                json={"status": args.get("status", "new"), "limit": args.get("limit", 20)},
                timeout=10,
            )
        elif name == "triage_feedback":
            r = requests.post(
                f"{TOOLS_API_BASE}/triage_feedback",
                headers=headers,
                json={
                    "feedback_id": args.get("feedback_id"),
                    "status": args.get("status"),
                    "priority": args.get("priority"),
                    "github_issue": args.get("github_issue"),
                    "notify_text": args.get("notify_text"),
                },
                timeout=10,
            )
        else:
            return json.dumps({"error": f"unknown tool: {name}"})

        if not r.ok:
            return json.dumps({"error": f"HTTP {r.status_code}", "body": r.text[:500]})
        return r.text
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Conversation history (Postgres)
# ---------------------------------------------------------------------------


TOOL_RESULT_TRUNCATE_CHARS = 1500
"""Cap on tool_result content length kept in HISTORY between turns.

Current turn always sees full tool_result (router builds it fresh).
But prior turns' tool_results bloat input_tokens — get_recent_biomarkers
can return 10KB JSON. After this turn, the agent's text answer summarised
what it needed; further turns can work from the summary + truncated tail.

1500 chars ≈ ~400 tokens. Keeps enough context (first table, a few rows)
without re-paying for full payload.
"""


def _truncate_tool_results_in_history(messages: list[dict]) -> list[dict]:
    """Shrink tool_result blocks in the historical messages.

    Only mutates blocks of type='tool_result'. The current turn's
    tool_result hasn't been appended yet by callers — it's safe.
    """
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            body = block.get("content")
            if isinstance(body, str) and len(body) > TOOL_RESULT_TRUNCATE_CHARS:
                block["content"] = body[:TOOL_RESULT_TRUNCATE_CHARS] + f"\n…[truncated, was {len(body)} chars]"
    return messages


def _load_history(db, user_id: int, limit: int = HISTORY_WINDOW) -> list[dict]:
    """Load last N messages from agent_conversations in chronological order.

    Returns list shaped for Anthropic API `messages` parameter.
    Each row's `content` is already a JSONB list of content blocks.
    Adjacent rows of same role are squashed by Anthropic-side; we don't.
    """
    from sqlalchemy import text

    # Фильтр source: NULL = легаси (до миграции add_agent_review_consent) или
    # реальный ход BotkinClaw. 'botkinclaw' = новые ходы агента. Любой
    # 'router_*' (raw-текст из food/vitamins/BP ветки) — не часть диалога с
    # Claude, в историю не подмешиваем, чтобы агент не пытался отвечать на
    # пищевые сообщения.
    sql = text(
        """
        SELECT role, content
        FROM agent_conversations
        WHERE user_id = :uid AND (source IS NULL OR source = 'botkinclaw')
        ORDER BY id DESC
        LIMIT :lim
        """
    )
    rows = db.execute(sql, {"uid": user_id, "lim": limit}).fetchall()
    rows = list(reversed(rows))  # chronological
    # Map our role values → Anthropic roles.
    # We store 'user' / 'assistant' (verbatim from API) and intermediary
    # 'tool_use' / 'tool_result' are part of assistant / user messages
    # respectively, but in our denormalised storage they're separate rows.
    # We re-group adjacent tool_use/tool_result rows back into assistant/user
    # turns.
    messages: list[dict] = []
    for role, content in rows:
        if role == "user":
            messages.append({"role": "user", "content": content})
        elif role == "assistant":
            messages.append({"role": "assistant", "content": content})
        elif role == "tool_use":
            # tool_use blocks are part of an assistant message — append.
            if messages and messages[-1]["role"] == "assistant":
                if isinstance(messages[-1]["content"], str):
                    messages[-1]["content"] = [{"type": "text", "text": messages[-1]["content"]}]
                messages[-1]["content"].extend(content)
            else:
                messages.append({"role": "assistant", "content": content})
        elif role == "tool_result":
            # tool_result blocks belong to a user message.
            if messages and messages[-1]["role"] == "user":
                if isinstance(messages[-1]["content"], str):
                    messages[-1]["content"] = [{"type": "text", "text": messages[-1]["content"]}]
                messages[-1]["content"].extend(content)
            else:
                messages.append({"role": "user", "content": content})

    # Truncate large tool_results in history to save input tokens on each call
    messages = _truncate_tool_results_in_history(messages)
    return _validate_history(messages)


def _recent_tracker_events(db, user_id: int, limit: int = 8) -> str:
    """Краткая сводка последних записей пользователя через быстрые парсеры
    (вес/еда/добавки/АД) — source `router_*`/`llm_text`.

    Эти сообщения намеренно исключены из истории диалога агента (`_load_history`),
    поэтому без сводки агент «не знает», что пользователь только что записал, и
    противоречит ему («ты не вносил вес»). Возвращает блок для system-prompt или
    '' если событий нет. Issue #169.
    """
    sql = text(
        """
        SELECT source, content
        FROM agent_conversations
        WHERE user_id = :uid AND role = 'user'
          AND (source LIKE 'router_%' OR source = 'llm_text')
        ORDER BY id DESC
        LIMIT :lim
        """
    )
    rows = db.execute(sql, {"uid": user_id, "lim": limit}).fetchall()
    if not rows:
        return ""

    labels = {
        "router_weight": "вес",
        "router_food": "еда",
        "router_vitamins": "добавки",
        "router_bp": "давление",
        "llm_text": "запись",
    }

    def _text_of(content) -> str:
        data = content
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return data.strip()[:120]
        if isinstance(data, list):
            parts = [b.get("text", "") for b in data if isinstance(b, dict) and b.get("type") == "text"]
            return " ".join(p for p in parts if p).strip()[:120]
        return ""

    lines = []
    for source, content in reversed(rows):  # chronological
        txt = _text_of(content)
        if txt:
            lines.append(f"• [{labels.get(source, 'запись')}] {txt}")
    if not lines:
        return ""

    return (
        "# 📝 ПОЛЬЗОВАТЕЛЬ НЕДАВНО ЗАПИСАЛ ЧЕРЕЗ ТРЕКЕР (вне истории чата)\n"
        "\n"
        "Эти записи прошли через быстрый обработчик и НЕ видны в истории диалога,\n"
        "но они РЕАЛЬНЫ и уже в БД. Учитывай их, НЕ переспрашивай и НЕ отрицай:\n"
        + "\n".join(lines)
        + "\nДля точных цифр всё равно вызывай соответствующий инструмент.\n"
        "\n---\n\n"
    )


def agent_last_turn_was_question(user_id: int, within_minutes: int = 10) -> bool:
    """#198: True если ПОСЛЕДНИЙ ход бота — вопрос агента (BotkinClaw), свежий и
    заканчивается «?».

    Нужно, чтобы короткий ответ пользователя («54», «120/80») после вопроса
    агента («сколько весишь?») уходил в агента, а не перехватывался weight/BP-
    парсером (диалог рвался). Берём АБСОЛЮТНО последний assistant-ход: если это
    парсер-подтверждение (source bp_fast_handler/llm_text/…) или старый ход
    (>within_minutes) — False, чтобы не спутать standalone-лог с ответом.
    """
    from sqlalchemy import text as sql_text
    from datetime import datetime, timezone, timedelta

    def _text_of(content) -> str:
        data = content
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return data
        if isinstance(data, list):
            return " ".join(b.get("text", "") for b in data if isinstance(b, dict) and b.get("type") == "text")
        return ""

    try:
        db = SessionLocal()
    except Exception:
        return False
    try:
        row = db.execute(
            sql_text(
                """
                SELECT content, source, created_at
                FROM agent_conversations
                WHERE user_id = :uid AND role = 'assistant'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"uid": user_id},
        ).fetchone()
        if row is None:
            return False
        content, source, created_at = row
        # Только реальный ход агента, не парсер-инъекция.
        if source not in (None, "botkinclaw"):
            return False
        if created_at is None:
            return False
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created_at > timedelta(minutes=within_minutes):
            return False
        # #198: вопрос агента часто кончается эмодзи/пунктуацией после «?»
        # («Какой вес записать? 😊») — простой endswith("?") давал False, и
        # короткий ответ юзера перехватывался парсером. Матчим «?», за которым до
        # конца строки идут только не-словесные символы (эмодзи, пробелы, скобки),
        # но НЕ буквы/цифры — так «? 😊» и «?» True, а «? Напиши число» False.
        return bool(re.search(r"\?[\s\W]*$", _text_of(content)))
    except Exception:
        return False
    finally:
        db.close()


def _validate_history(messages: list[dict]) -> list[dict]:
    """Strip orphan tool_use/tool_result blocks that violate Anthropic API.

    Bug fix for 400 'unexpected tool_use_id in tool_result blocks'. Reasons
    orphans happen at HISTORY_WINDOW boundary, after row deletions, or after
    save crashes between tool_use save and matching tool_result save.

    Algorithm:
      1. Collect all tool_use_id present anywhere in the window
      2. Drop any tool_result blocks whose tool_use_id isn't in that set
      3. Drop any tool_use blocks whose id isn't matched by a later tool_result
      4. Drop messages that become empty after block-stripping
      5. Strip leading message if its only content was orphan tool blocks
         (Anthropic requires first message to be 'user' with real text)
    """
    # First pass: collect all tool_use_id and tool_result tool_use_id present
    tool_use_ids: set[str] = set()
    tool_result_ids: set[str] = set()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                if tid := block.get("id"):
                    tool_use_ids.add(tid)
            elif block.get("type") == "tool_result":
                if tid := block.get("tool_use_id"):
                    tool_result_ids.add(tid)

    # Both must match — orphans on either side are stripped
    valid_ids = tool_use_ids & tool_result_ids

    # Second pass: strip blocks with id not in valid_ids
    cleaned: list[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            cleaned.append(m)
            continue
        if not isinstance(content, list):
            cleaned.append(m)
            continue
        new_blocks = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "tool_use":
                    if block.get("id") in valid_ids:
                        new_blocks.append(block)
                    continue
                if btype == "tool_result":
                    if block.get("tool_use_id") in valid_ids:
                        new_blocks.append(block)
                    continue
            new_blocks.append(block)
        if new_blocks:
            cleaned.append({"role": m["role"], "content": new_blocks})

    # Anthropic requires first message role == "user". If we somehow end up
    # with an assistant-first list (because user msg got fully orphaned),
    # drop leading assistants.
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    return cleaned


# ---------------------------------------------------------------------------
# P-003 — авто-инвалидация устаревших turn'ов истории при расхождении со
# свежими tool-данными текущего хода.
#
# Прецедент 09.06.2026: после фикса Z2-метрики тул отдаёт правильные числа
# (106 мин/нед, пробежка 36.7 мин), но агент продолжал парротить старые числа
# (61/38) и «Z2=0/баг» из накопленной истории, игнорируя свежий tool_result.
# Промпт-правило «свежие данные > история» (коммит ad73abf) оказалось
# недостаточным — при накопленной истории агент говорил «только что смотрел,
# ничего не изменилось» и не звал тул заново. Лечилось только ручной чисткой
# agent_conversations / командой /agent_reset.
#
# Механизм: ПЕРЕД следующим вызовом Claude сравниваем ключевые числа свежего
# tool_result этого хода с числами/утверждениями в недавних assistant-turn'ах
# и при ЯВНОМ конфликте по той же метрике нейтрализуем устаревший turn (текст
# заменяется маркером, tool_use/tool_result-блоки сохраняются — чтобы не
# нарушить парность для _validate_history).
#
# Консервативность (главный риск — выбросить валидный turn):
#  • Срабатываем только если свежий tool_result реально содержит значение по
#    этой метрике (есть ground truth этого хода).
#  • Числа из assistant-turn берём ТОЛЬКО рядом с keyword метрики и её unit.
#  • Числовой конфликт = turn содержит число по метрике, и НИ ОДНО из его
#    чисел по этой метрике не совпадает (в пределах tol) ни с одним свежим
#    значением. Если хоть одно число turn'а совпадает со свежим — turn НЕ
#    трогаем (он, видимо, обсуждает актуальное состояние / контекстуализирует).
#  • Fresh-значения собираем щедро (явные JSON-поля + числа рядом с keyword) —
#    чем больше «белый список» актуальных чисел, тем меньше ложных срабатываний.
#  • «Баг»-конфликт = в turn'е keyword + фраза вида «0/баг/не считается», а
#    свежее значение метрики строго > 0.
# См. docs/night-shift/2026-06-09.md (P-003).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StaleMetric:
    """Описание метрики, по которой ищем конфликт история↔свежий tool_result."""

    name: str
    label: str
    # keyword метрики — должен встретиться И в свежем tool-тексте, И в turn'е.
    keyword: re.Pattern
    # захват числа в контексте метрики (число + её unit). group(1) — число.
    number_re: re.Pattern
    # JSON-пути к актуальным значениям в свежем tool_result (для «белого списка»).
    # Поддержка `a.b` и `a[].b` (списки словарей).
    json_paths: tuple = ()
    abs_tol: float = 1.0  # допустимое расхождение (округление и т.п.)
    rel_tol: float = 0.0  # доп. относительный допуск от свежего значения


_NUM = r"(\d+(?:[.,]\d+)?)"
# unit-токены метрик. Z2: число + «мин» (минуты тренировки/неделю).
_RE_Z2_KW = re.compile(r"z2|зон[аеуы]\s*2|аэроб|aerobic", re.IGNORECASE)
_RE_WEIGHT_KW = re.compile(r"\bвес\b|\bweight\b|кг\b", re.IGNORECASE)
_RE_CAL_KW = re.compile(r"калор|ккал|kcal|калораж", re.IGNORECASE)
# NB: давление (систола/диастола, пара «120/80») и биомаркеры (десятки имён,
# разные единицы) — намеренно НЕ покрыты: безопасный generic-матчер для них
# заметно сложнее, а риск выкинуть валидный turn выше пользы. Follow-up — см.
# docs/night-shift/2026-06-09.md (P-003).

# Фразы «метрика сломана / ноль / не считается» — категоричные утверждения,
# которые становятся заведомо ложными, когда свежее значение метрики > 0.
_BUG_PHRASE_RE = re.compile(
    r"\bбаг\b|\bглюк|не\s+счита|не\s+работает|сломан|\b0\s*мин|\bноль\b|=\s*0\b|:\s*0\b",
    re.IGNORECASE,
)

# ⚠️ Известное ограничение (verify 09.06.2026): недельный агрегат Z2 зависит от
# окна (≈22 мин/нед за 30 дней vs 61 мин/нед за 7 дней — оба верны). Поэтому
# исторически-корректный turn про ДРУГОЕ окно может быть нейтрализован, если в
# нём нет ни одного числа, совпавшего со свежим. Вред мал (агент всё равно
# перезапрашивает тул), снижается за счёт: щедрого whitelist (per-workout
# aerobic_base_min/duration_min стабильны и не зависят от окна) + правила
# «оставить turn, если хоть одно число совпало». Follow-up — различать окна.
STALE_METRICS: tuple = (
    _StaleMetric(
        name="z2",
        label="Z2 / aerobic base (мин)",
        keyword=_RE_Z2_KW,
        number_re=re.compile(_NUM + r"\s*мин", re.IGNORECASE),
        json_paths=(
            "stats.z2_min_per_week",
            "stats.z2_target_attia",
            "stats.hiit_min_per_week",
            "items[].aerobic_base_min",
            "items[].duration_min",
        ),
        abs_tol=1.0,
    ),
    _StaleMetric(
        name="weight",
        label="вес (кг)",
        keyword=_RE_WEIGHT_KW,
        number_re=re.compile(_NUM + r"\s*кг", re.IGNORECASE),
        json_paths=("weight", "weight_kg", "latest.weight", "current.weight"),
        abs_tol=0.3,
    ),
    _StaleMetric(
        name="calories",
        label="калории (ккал)",
        keyword=_RE_CAL_KW,
        number_re=re.compile(_NUM + r"\s*к?кал", re.IGNORECASE),
        json_paths=("calories", "total_calories", "kcal", "totals.calories"),
        abs_tol=20.0,
        rel_tol=0.05,
    ),
)


def _try_json(text: str):
    """Распарсить tool_result как JSON; None если не получилось."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _to_float(v) -> Optional[float]:
    try:
        f = float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return None
    return f


def _json_path_numbers(obj, path: str) -> set:
    """Числа по dot-пути. Сегмент `key[]` означает «обойти список словарей»."""
    nodes = [obj]
    for seg in path.split("."):
        list_seg = seg.endswith("[]")
        key = seg[:-2] if list_seg else seg
        nxt = []
        for node in nodes:
            if not isinstance(node, dict) or key not in node:
                continue
            val = node[key]
            if list_seg:
                if isinstance(val, list):
                    nxt.extend(val)
            else:
                nxt.append(val)
        nodes = nxt
    out: set = set()
    for n in nodes:
        f = _to_float(n)
        if f is not None:
            out.add(f)
    return out


# Сегментируем текст ТОЛЬКО по границам предложений (точка/!/?/;/перевод
# строки). НЕ дробим по « — » и « , »: «Z2-база — 61 мин/нед, пробежка 38 мин»
# должно остаться одним сегментом, иначе keyword («Z2») отрывается от числа
# («61») и теряется. Прецедент 09.06.2026 (verify): дробление по « — »/«, »
# выкидывало 61 из связки с Z2 → инвалидация срабатывала на одном «38», хотя
# 61 был актуальным числом (консервативное правило «оставить turn, если хоть
# одно число совпало со свежим» не применялось).
_SEGMENT_SPLIT_RE = re.compile(r"[\n.!?;]+")


def _numbers_near_keyword(text: str, metric: _StaleMetric) -> set:
    """Числа метрики из тех сегментов текста, где упомянут её keyword."""
    out: set = set()
    for seg in _SEGMENT_SPLIT_RE.split(text):
        if not metric.keyword.search(seg):
            continue
        for m in metric.number_re.finditer(seg):
            f = _to_float(m.group(1))
            if f is not None:
                out.add(f)
    return out


def _fresh_values(metric: _StaleMetric, tool_text: str) -> set:
    """«Белый список» актуальных значений метрики из свежего tool_result.

    Собираем щедро: явные JSON-поля + числа рядом с keyword. Чем шире набор —
    тем меньше ложных инвалидаций (assistant-число, совпавшее с любым свежим,
    считается актуальным).
    """
    vals: set = set()
    obj = _try_json(tool_text)
    if obj is not None:
        for path in metric.json_paths:
            vals |= _json_path_numbers(obj, path)
    vals |= _numbers_near_keyword(tool_text, metric)
    return vals


def _matches_fresh(value: float, fresh: set, metric: _StaleMetric) -> bool:
    """value совпадает с каким-либо свежим значением в пределах допуска."""
    for f in fresh:
        if abs(value - f) <= max(metric.abs_tol, metric.rel_tol * abs(f)):
            return True
    return False


# В маркере НЕ цитируем устаревшие числа — иначе модель может их снова
# спарротить. Конкретику (какие числа выкинули) пишем только в лог.
_STALE_MARKER = (
    "[⚠️ P-003: предыдущий ответ убран из контекста — его числа по метрике "
    "«{label}» противоречат свежему tool_result этого хода и устарели. "
    "Опирайся на свежий tool_result, не пытайся вспомнить прошлый ответ.]"
)


def _invalidate_stale_history(messages: list[dict], fresh_tool_text: str) -> list[str]:
    """Нейтрализовать assistant-turn'ы, противоречащие свежему tool_result.

    Мутирует `messages` на месте: у конфликтных assistant-turn'ов текстовые
    блоки заменяются маркером (tool_use-блоки сохраняются, чтобы не осиротить
    парные tool_result — см. _validate_history). Возвращает список лог-строк о
    том, что выкинули (пусто = ничего не тронули).

    Консервативно: см. блок-комментарий выше. Любая внутренняя ошибка
    парсинга не должна ронять ход — вызывающий оборачивает в try/except.
    """
    if not fresh_tool_text:
        return []

    # Какие метрики реально присутствуют в свежем tool_result (есть ground truth).
    active: list[tuple] = []
    for metric in STALE_METRICS:
        if not metric.keyword.search(fresh_tool_text):
            continue
        fresh = _fresh_values(metric, fresh_tool_text)
        if not fresh:
            continue
        fresh_positive = any(f > 0 for f in fresh)
        active.append((metric, fresh, fresh_positive))

    if not active:
        return []

    notes: list[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if not text.strip():
            continue

        for metric, fresh, fresh_positive in active:
            if not metric.keyword.search(text):
                continue

            claim_nums = _numbers_near_keyword(text, metric)
            numeric_conflict = bool(claim_nums) and all(not _matches_fresh(n, fresh, metric) for n in claim_nums)
            bug_conflict = fresh_positive and bool(_BUG_PHRASE_RE.search(text))
            # bug-фразу засчитываем только если она в сегменте с keyword метрики.
            if bug_conflict:
                bug_conflict = any(
                    metric.keyword.search(seg) and _BUG_PHRASE_RE.search(seg) for seg in _SEGMENT_SPLIT_RE.split(text)
                )

            if not (numeric_conflict or bug_conflict):
                continue

            old = ", ".join(_fmt_num(n) for n in sorted(claim_nums)) if numeric_conflict else "0/сломано"
            marker = _STALE_MARKER.format(label=metric.label)
            # Заменяем текстовые блоки маркером, tool_use оставляем.
            new_content = []
            replaced = False
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    if not replaced:
                        new_content.append({"type": "text", "text": marker})
                        replaced = True
                    # дубли text-блоков отбрасываем
                else:
                    new_content.append(b)
            if not replaced:
                new_content.insert(0, {"type": "text", "text": marker})
            msg["content"] = new_content

            notes.append(
                f"metric={metric.name} old=[{old}] fresh_sample="
                f"{sorted(fresh)[:5]} reason="
                f"{'numeric' if numeric_conflict else 'bug-phrase'}"
            )
            break  # turn нейтрализован — другие метрики по нему не проверяем

    return notes


def _fmt_num(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _save_message(
    db,
    user_id: int,
    role: str,
    content: Any,
    tool_use_id: Optional[str] = None,
    source: str = "botkinclaw",
):
    """source='e2e_test' для тестовых сообщений с маркером 🧪 (task #62) —
    позволяет потом массово удалять через /admin/cleanup_e2e без риска
    задеть реальные диалоги."""
    from sqlalchemy import text

    db.execute(
        text(
            """
            INSERT INTO agent_conversations (user_id, role, content, tool_use_id, source)
            VALUES (:uid, :role, CAST(:content AS JSONB), :tid, :src)
            """
        ),
        {
            "uid": user_id,
            "role": role,
            "content": json.dumps(content),
            "tid": tool_use_id,
            "src": source,
        },
    )


def log_router_raw_text(user_id: int, raw_text: str, msg_type: str) -> None:
    """Логирует raw текст сообщения, ушедшего НЕ в BotkinClaw, а в роутер
    (food / vitamins / bp / weight / mixed / body_measurements).

    Цель: чтобы product-review мог увидеть формулировки пользователя даже
    когда сообщение распарсилось в структурку и положилось в спец-таблицу.
    Эти строки не подмешиваются в историю BotkinClaw (см. _load_history).

    Безопасно: ловит и логирует любую ошибку, чтобы не сломать основной
    хэндлер из-за проблемы с записью.
    """
    if not raw_text:
        return
    try:
        from sqlalchemy import text

        db = SessionLocal()
        try:
            db.execute(
                text(
                    """
                    INSERT INTO agent_conversations (user_id, role, content, source)
                    VALUES (:uid, 'user', CAST(:content AS JSONB), :src)
                    """
                ),
                {
                    "uid": user_id,
                    "content": json.dumps([{"type": "text", "text": raw_text}]),
                    "src": f"router_{msg_type}",
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning(f"log_router_raw_text failed for {user_id}: {e}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


# Маппинг tool name → короткая фраза для прогресс-индикатора (UI бот'a).
# Идея: пользователь видит «🎨 рисую график...» вместо 18-секундной тишины.
# Если tool здесь нет — фоллбэк на просто «📡 запрос данных».
_TOOL_PROGRESS_LABEL = {
    # Read tools
    "get_recent_meals": "🍽 собираю питание",
    "get_recent_supplements": "💊 смотрю добавки",
    "get_supplement_daily_log": "💊 сверяю приём добавок по дням",
    "get_recent_bp": "🩸 поднимаю давление",
    "get_recent_glucose": "🩸 смотрю глюкозу",
    "get_glucose_stats": "🩸 считаю TIR",
    "get_recent_sleep": "😴 проверяю сон",
    "get_recent_trends": "📊 собираю динамику",
    "get_recent_workouts": "🏃 поднимаю тренировки",
    "get_menstrual_data": "🌸 проверяю цикл",
    "get_recent_biomarkers": "🧪 смотрю анализы",
    "get_latest_biomarkers": "🧪 проверяю свежесть анализов",
    "get_kb_value": "📋 ищу в карте здоровья",
    "list_kb_keys": "🗂 смотрю что есть в карте",
    "get_open_questions": "🚩 свеяю с открытыми вопросами",
    "get_weight_history": "⚖️ собираю историю веса",
    "get_body_measurements": "📏 проверяю замеры",
    "get_dashboard_summary": "📊 свожу метрики",
    "meal_context": "🍽 подбираю что поесть",
    "get_user_profile": "👤 читаю профиль",
    "get_user_settings": "⚙️ читаю настройки",
    "get_indoor_air": "🏠 проверяю воздух",
    "get_outdoor_weather": "🌤 смотрю погоду",
    "get_phenoage": "🧬 считаю PhenoAge",
    "get_day_summary": "📅 свожу день",
    "get_profile_questionnaire": "📝 читаю анкету",
    # Write tools
    "log_meal_text": "✍️ записываю еду",
    "edit_meal": "✏️ правлю запись о еде",
    "delete_meal": "🗑 удаляю запись о еде",
    "log_supplement": "✍️ отмечаю добавку",
    "log_bp": "✍️ записываю давление",
    "regenerate_health_token": "🔑 пересоздаю токен",
    "update_profile_questionnaire": "📝 обновляю анкету",
    "update_user_settings": "⚙️ обновляю настройки",
    # Render tools
    "render_report": "🎨 рисую график",
    "render_chart": "🎨 рисую график",
    "generate_doctor_report": "📄 готовлю отчёт для врача",
}


def build_default_agent_prompt(user) -> str:
    """Лёгкий дефолтный per-user системный промпт для любого пользователя без
    индивидуального ``agent_system_prompt`` (само-онбординг через бота/сайт).

    Собирается из ``onboarding_data`` + полей ``users`` (имя, цель, возраст, пол),
    без вызова Claude и без PROFILE.md/KB. Никаких «семейных» привилегий — каждый
    зарегистрированный пользователь получает полный разговорный функционал (#165).
    Богатый промпт из ``onboard_family_user.py`` (если задан) имеет приоритет —
    этот билдер только fallback.
    """
    data = getattr(user, "onboarding_data", None) or {}
    name = (data.get("name") or getattr(user, "first_name", None) or "пользователь").strip() or "пользователь"
    goal = (data.get("goal") or "общее здоровье, профилактика и долголетие").strip()

    bits = []
    if data.get("age"):
        bits.append(f"{data['age']} лет")
    sex_ru = {"male": "муж.", "female": "жен."}.get(data.get("sex"))
    if sex_ru:
        bits.append(sex_ru)
    who = name + (f" ({', '.join(bits)})" if bits else "")

    # Медпрофиль приклеивается к промпту отдельным блоком (_health_profile_block).
    # Если он непустой — не утверждаем обратное строкой ниже (#340): агент верил
    # базовой фразе и советовал как здоровому, игнорируя блок с диагнозами.
    history_line = (
        "Подробной истории обследований в системе может не быть, но медицинский "
        "профиль пациента есть — он в блоке «Медпрофиль» ниже, учитывай его. "
        "Помогай освоиться и подсказывай, как пользоваться ботом, когда уместно.\n\n"
        if _has_health_profile(user)
        else "Это пользователь без подробной медицинской истории в системе — помогай "
        "освоиться и подсказывай, как пользоваться ботом, когда уместно.\n\n"
    )

    base = (
        f"Ты — личный AI-агент по теме здоровья для пользователя {name}. "
        "Часть проекта Botkin (botkin.health), канал Telegram @Botkin_md_bot.\n\n"
        "## Пользователь\n\n"
        f"**{who}.** Цель: {goal}.\n"
        f"{history_line}"
        "## Что ты умеешь и как пользователь это делает\n\n"
        "- **Логирование еды** — пользователь пишет «съел банан и кофе», шлёт фото "
        "тарелки или голосовое; ты распознаёшь и считаешь калории/БЖУ. На вопрос "
        "«как вносить еду» — объясни этими словами с примерами.\n"
        "- **Добавки и витамины** — «выпил витамин D» логируется как приём. Фото упаковки "
        "добавки распознаёт отдельный модуль (пользователь подтверждает кнопкой), сам снимок "
        "тебе не виден, но результат появляется в get_recent_supplements. При вопросах о "
        "добавках или схеме приёма СНАЧАЛА вызови get_recent_supplements и get_user_settings; "
        "не проси переписывать схему текстом, если она уже есть в данных или истории диалога.\n"
        "- **Анализы крови** — пользователь кидает PDF/фото анализов, ты разбираешь показатели.\n"
        "- **Дашборд и отчёты** — биологический возраст (PhenoAge), динамика по месяцам.\n"
        "- **Wearables и каналы данных** — реально существующие интеграции: Garmin (прямая), "
        "Apple Health (Health Auto Export или iOS Shortcut, команда /health_token), "
        "**Android Health Connect** (шаги/вес/пульс/давление/VO2; Mi Fitness, Samsung Health, "
        "Huawei Health и др. пишут в Health Connect — настройка через Александра), "
        "CGM-глюкоза FreeStyle Libre (/connect_cgm). НЕ говори, что интеграции с "
        "Google Health/Android нет — она есть через Health Connect.\n"
        "- **Вопросы о здоровье** — питание, сон, активность, корреляции в данных.\n\n"
        "## Данные пользователя — через tools\n\n"
        "Бери цифры из tools (get_recent_biomarkers, get_recent_meals, get_recent_supplements, "
        "вес/тело, сон, давление). Не угадывай. Если данных ещё нет (новый пользователь) — "
        "так и скажи и предложи начать логировать.\n"
    )

    from core.personas import get_persona

    persona = get_persona(data.get("persona"))
    tone_block = (
        "\n\n## Стиль общения\n\n"
        f"Общайся в манере «{persona.display}»: {persona.tone_prompt}.\n"
        "Это стиль, а не содержание — факты и цифры всегда бери из tools."
    )
    return base + tone_block


# users.smoking_status → человекочитаемо для промпта (#340). Курение спрашивает
# онбординг (шаг 8, нужен для PhenoAge), но до агента оно раньше не доходило:
# видели только дашборд и /user_profile.
SMOKING_RU: dict[str, str] = {
    "never": "не курит",
    "former": "бросил",
    "current": "курит",
    "occasional": "курит изредка",
}


def _has_health_profile(user) -> bool:
    """Есть ли у пользователя аллергии или хронические диагнозы в профиле.

    Курение сюда НЕ входит: оно есть почти у каждого (обязательный шаг
    онбординга) и само по себе медицинской историей не является.
    """
    from core.health.onboarding_lists import ALLERGY_KEYS, CONDITION_KEYS, onboarding_list

    data = getattr(user, "onboarding_data", None) or {}
    return bool(onboarding_list(data, ALLERGY_KEYS) or onboarding_list(data, CONDITION_KEYS))


def _health_profile_block(user) -> str:
    """Живой блок медпрофиля (аллергии/диагнозы/курение) для промпта.

    Пусто по всем источникам → пустая строка (не шумим). Аллергии и диагнозы
    читаются из тех же ключей ``onboarding_data``, куда пишет
    merge_onboarding_lists, поэтому агент видит свежие данные сразу после
    /doc-сохранения (промпт пересобирается на каждый вызов ask_agent).
    Курение — из ``users.smoking_status``.
    """
    from core.health.onboarding_lists import ALLERGY_KEYS, CONDITION_KEYS, onboarding_list

    data = getattr(user, "onboarding_data", None) or {}
    allergies = onboarding_list(data, ALLERGY_KEYS)
    conditions = onboarding_list(data, CONDITION_KEYS)
    smoking = SMOKING_RU.get(getattr(user, "smoking_status", None) or "")
    if not allergies and not conditions and not smoking:
        return ""
    lines = ["\n\n## Медпрофиль (со слов пациента / из документов)"]
    if allergies:
        lines.append(f"Аллергии: {', '.join(allergies)}")
    if conditions:
        lines.append(f"Хронические диагнозы: {', '.join(conditions)}")
    if smoking:
        lines.append(f"Курение: {smoking}")
    lines.append("Учитывай при советах (лекарства, продукты, триггеры).")
    return "\n".join(lines)


# Admin-контекст-блок системного промпта (#337). Admin-статус (BOTKIN_ADMIN_IDS) —
# отдельная ось от когорты данных (owner/family/…): когорта ограничивает видимость
# чужих данных, admin-статус даёт право на операционные действия. Без явного сигнала
# LLM гадает по family-персоне и отказывает админу в триаже фидбека.
ADMIN_CONTEXT_PROMPT = (
    "# 🛡 ТЫ ОБЩАЕШЬСЯ С АДМИНИСТРАТОРОМ BOTKIN\n"
    "\n"
    "Текущий пользователь — администратор системы (входит в BOTKIN_ADMIN_IDS). Его\n"
    "admin-статус НЕ зависит от когорты данных (owner/family/early_user/external):\n"
    "когорта ограничивает видимость чужих данных, а admin-статус даёт право на\n"
    "операционные действия с ботом.\n"
    "\n"
    "Поэтому: если админ просит показать очередь обратной связи или разобрать обращение\n"
    "(сменить статус/приоритет, ответить автору) — ВЫЗОВИ `list_feedback` / `triage_feedback`\n"
    "и выполни. НЕ отказывай со ссылкой на когорту («ты family-пользователь») — эти\n"
    "инструменты доступны тебе именно потому, что пользователь администратор.\n"
    "\n"
    "---\n"
    "\n"
)


def build_admin_context(is_admin: bool) -> str:
    """Admin-контекст-блок для системного промпта агента (#337).

    Пустая строка для обычных пользователей. Для админов — явный сигнал, что
    admin-статус (BOTKIN_ADMIN_IDS) — отдельная ось от когорты данных, чтобы LLM
    не отказывал в триаже фидбека, приняв себя за «family-only» ассистента.
    """
    return ADMIN_CONTEXT_PROMPT if is_admin else ""


def _log_first_question(db, user_id: int, is_e2e: bool) -> None:
    """E6: первое свободное сообщение агенту (once). Пропускаем E2E-прогоны."""
    if is_e2e:
        return
    try:
        log_event(db, user_id=user_id, event="first_agent_question", once=True)
    except Exception:
        logger.exception("E6 first_agent_question log failed for %s", user_id)


def ask_agent(
    user_id: int,
    user_text: str,
    progress_cb: Optional[Callable[[str], None]] = None,
    is_e2e: bool = False,
) -> str:
    """Synchronous — call from `run_in_executor`.

    Returns assistant's text reply. Empty string if nothing produced.
    Errors are raised; caller (handler) catches and replies "технически не вышло".

    is_e2e=True → все _save_message в agent_conversations помечаются
    source='e2e_test' (вместо 'botkinclaw') чтобы потом удалять через
    /admin/cleanup_e2e без риска задеть реальные диалоги. Task #62.
    """
    src = "e2e_test" if is_e2e else "botkinclaw"
    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.is_active:
            raise RuntimeError(f"User {user_id} not found or inactive")

        _log_first_question(db, user_id, is_e2e)

        token = _generate_jwt(user)

        # Прогресс «думаю» сразу после провижна — пользователь видит что бот
        # начал работать ещё ДО первого Claude-вызова (~3-5 сек).
        if progress_cb:
            try:
                progress_cb("🤔 думаю")
            except Exception:
                logger.exception("progress_cb start failed")

        # Build messages: history + new user turn
        history = _load_history(db, user_id)
        # P-003: всё ДО текущего user-сообщения — кандидаты на инвалидацию, если
        # свежий tool_result этого хода им противоречит (см. _invalidate_stale_history).
        prior_history_len = len(history)
        history.append({"role": "user", "content": user_text})

        # Persist user turn immediately so a crash mid-call doesn't lose it.
        _save_message(db, user_id, "user", user_text, source=src)
        db.commit()

        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        # Universal meta-directive: prepended to every per-user system prompt.
        # Закрывает повторяющийся класс ошибок — бот корректно отвечает на
        # вопрос пользователя, но НЕ всплывает открытые клинические вопросы
        # из его KB. Прецедент 25.05.2026: папа Александра спрашивал «какие
        # диагнозы» — бот ответил 8 пунктами из KB, но не упомянул что K/Mg/ТТГ
        # ни разу не сдавались при QTc 0.60 (это давно в open_questions).
        # После этой директивы — модель всегда дёргает get_open_questions
        # на любой мед-теме и интегрирует релевантное в ответ.
        UNIVERSAL_META_PROMPT = (
            "# 🚫 НЕ ЗАЯВЛЯЙ ЧТО ВИДИШЬ ДАННЫЕ БЕЗ ВЫЗОВА ИНСТРУМЕНТА (универсальный)\n"
            "\n"
            "У тебя НЕТ прямого доступа к базе данных пользователя — данные доступны\n"
            "ТОЛЬКО через инструменты. Это правило приоритетнее всех остальных.\n"
            "\n"
            "❌ ЗАПРЕЩЕНО без подтверждённого tool-результата в ЭТОМ ходе:\n"
            "- Утверждать «вижу / знаю / у тебя записано» любые числа или факты из БД\n"
            "- Заявлять «данных нет / ты не вносил(а)» — возможно, ты просто не вызвал\n"
            "  инструмент, а данные уже есть\n"
            "- Описывать «состояние» пользователя по system prompt или истории переписки —\n"
            "  там профиль и контекст, а НЕ свежие записи из БД\n"
            "\n"
            "✅ АЛГОРИТМ: получил вопрос → ВЫЗОВИ нужный инструмент → ответь по его результату.\n"
            "\n"
            "Прецедент 19–20.06.2026 (@maxurazaev): агент описывал данные пользователя\n"
            "без вызова инструментов, опираясь только на system prompt и историю переписки.\n"
            "\n"
            "---\n"
            "\n"
            "# ⚡ СВЕЖИЕ ДАННЫЕ ИНСТРУМЕНТА > ТВОИ ПРОШЛЫЕ ОТВЕТЫ (важнее всего)\n"
            "\n"
            "На ЛЮБОЙ вопрос про числа (тренировки, Z2/зоны, сон, вес, биомаркеры,\n"
            "калории, давление) — ВЫЗЫВАЙ соответствующий инструмент ЗАНОВО и отвечай\n"
            "по его свежему результату. НИКОГДА не повторяй число из своего прошлого\n"
            "ответа в истории («как я уже говорил», «ничего не изменилось», «оба числа\n"
            "уже были») — данные и расчёты могли быть исправлены между сообщениями.\n"
            "Если свежий tool-result противоречит тому, что ты говорил раньше, —\n"
            "верь ИНСТРУМЕНТУ и спокойно дай новое число (без «был баг»/самокопания).\n"
            "Особенно: НЕ утверждай, что какая-то метрика «не считается / это баг /\n"
            "приходит пустой», если ты не вызвал инструмент прямо сейчас и не увидел\n"
            "это сам в его ответе.\n"
            "\n"
            "---\n"
            "\n"
            "# 🍽 «ЧТО МНЕ СЪЕСТЬ СЕЙЧАС» — ЧЕРЕЗ meal_context (универсальный)\n"
            "\n"
            "При вопросах «что мне съесть / что ещё можно / что на ужин / перекус» —\n"
            "вызови `meal_context` (остаток КБЖУ + диагнозы-ограничения + любимые\n"
            "продукты одним вызовом) и СРАЗУ дай 2-3 конкретных варианта под остаток\n"
            "калорий и под ограничения (диагнозы — критично: подагра, демпинг и т.п.).\n"
            "НЕ гоняй юзера по 3-4 уточняющим репликам («где ты?», «что дома?») —\n"
            "уточняй только если без этого реально никак.\n"
            "\n"
            "## Демпинг / реактивная гипогликемия / постбариатрия — низкоГИ по умолчанию\n"
            "Если в `constraints` из `meal_context` (или в KB-диагнозах) есть демпинг-\n"
            "синдром, реактивная гипогликемия или постбариатрическое состояние —\n"
            "АКТИВНО предлагай низкогликемические варианты и замены, а не «привычное»:\n"
            "- цельное зерно вместо белого хлеба/белого риса, бобовые, НЕ-быстрый овёс,\n"
            "  цельный фрукт вместо сухофруктов/сока; белок и жир — вперёд углеводов.\n"
            "- избегай быстрых сахаров и высокоГИ на голодный желудок (запускают\n"
            "  реактивную гипо у таких пациентов).\n"
            "Это гейтится диагнозом: у кого таких ограничений в constraints/KB НЕТ —\n"
            "совет НЕ меняется, обычные варианты под остаток калорий.\n"
            "\n"
            "---\n"
            "\n"
            "# 🩺 ДАННЫЕ АД — ОБЯЗАТЕЛЬНО ЧЕРЕЗ ИНСТРУМЕНТ (универсальный)\n"
            "\n"
            "При ЛЮБОМ вопросе про артериальное давление, его динамику, замеры за\n"
            "день/неделю/период — ВСЕГДА сначала вызови инструмент `recent_bp`.\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Говорить «замеров нет» или «не вижу измерений» без вызова recent_bp\n"
            "- Опираться только на историю переписки для суждений о наличии/отсутствии замеров АД\n"
            "\n"
            "Причина: замеры АД поступают напрямую через быстрый regex-обработчик и\n"
            "могут не отражаться в истории чата как обычные сообщения. Инструмент\n"
            "recent_bp всегда показывает актуальную картину из БД.\n"
            "\n"
            "Прецедент 26.05.2026: пользователь сделал 5 замеров (все ✅ Записано),\n"
            "агент ответил «сегодняшних нет» — не вызвал recent_bp, доверился памяти.\n"
            "\n"
            "---\n"
            "\n"
            "# 🩸 ДАННЫЕ ГЛЮКОЗЫ (CGM) — ОБЯЗАТЕЛЬНО ЧЕРЕЗ ИНСТРУМЕНТ (универсальный)\n"
            "\n"
            "При ЛЮБОМ вопросе про сахар/глюкозу (текущий уровень, динамика за день,\n"
            "TIR, реакция на еду) — ВСЕГДА сначала вызови `get_recent_glucose`\n"
            "(или `get_glucose_stats` для периода).\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Утверждать «у тебя нет CGM-сенсора / нечего показать / данные неоткуда\n"
            "  взять», НЕ вызвав get_recent_glucose. Наличие точек в ответе = сенсор\n"
            "  подключён. Пусто → «свежих данных нет прямо сейчас», а НЕ «сенсора нет».\n"
            "- Судить о наличии/отсутствии сенсора по system-prompt/контексту: CGM мог\n"
            "  быть подключён ПОСЛЕ генерации твоего профиля — источник истины инструмент.\n"
            "\n"
            "Прецедент 17.06.2026: у пользователя 233 замера за сутки, агент ответил\n"
            "«нет CGM-сенсора» — не вызвал get_recent_glucose, доверился контексту.\n"
            "\n"
            "## Свежесть данных — НЕ выдавай несвежее за текущее\n"
            "`get_recent_glucose` возвращает `is_stale`, `last_point_age_min` и\n"
            "`last_point_local` (время последней точки). Если `is_stale=true` (разрыв\n"
            ">30 мин до сейчас) ИЛИ `refresh_skipped=true` (свежий замер не дотянулся —\n"
            "cooldown/бан) — НЕ описывай данные как «сейчас / весь день / на текущий\n"
            "момент». Явно проговори пробел: «данные есть только до HH:MM "
            "(`last_point_local`), дальше свежих нет». TIR/статистику за неполные сутки\n"
            "помечай «за период до HH:MM», а не «за день».\n"
            "\n"
            "Прецедент 17.06.2026: последняя точка 08:59, вопрос в 17:08 (разрыв 8ч,\n"
            "бан LLU), агент написал «TIR 99% — идеально весь день / сейчас» как будто\n"
            "данные актуальны на 17:08.\n"
            "\n"
            "---\n"
            "\n"
            "# ⚖️ ДАННЫЕ ВЕСА — ОБЯЗАТЕЛЬНО ЧЕРЕЗ ИНСТРУМЕНТ (универсальный)\n"
            "\n"
            "При ЛЮБОМ вопросе про вес (текущий, динамика, расчёт нормы белка/калорий\n"
            "по весу) — ВСЕГДА вызывай `get_weight_history` ЗАНОВО прямо сейчас.\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Говорить «веса нет / ты его не вносил(а)», НЕ вызвав get_weight_history\n"
            "  в ЭТОМ ходе. Вес поступает через быстрый regex-обработчик и может НЕ\n"
            "  отражаться в истории чата — но он уже в БД, инструмент его видит.\n"
            "- Если get_weight_history раньше в диалоге вернул пусто — это устарело,\n"
            "  пользователь мог записать вес ПОСЛЕ. Вызови инструмент заново.\n"
            "\n"
            "Прецедент 18.06.2026: пользователь написал «54» (✅ Записано), агент трижды\n"
            "ответил «веса нет, ты не вносила» — не перевызвал get_weight_history.\n"
            "\n"
            "---\n"
            "\n"
            "# 🙅 НЕ ПРОТИВОРЕЧЬ ПОЛЬЗОВАТЕЛЮ О ТОМ, ЧТО ОН ВВОДИЛ (универсальный)\n"
            "\n"
            "Если пользователь говорит «я же написал(а) выше / я уже вносил(а) это», а\n"
            "ты этого не видишь в истории — НЕ заявляй «ты не вводил / не называл». Его\n"
            "запись веса/еды/АД/добавок могла пройти через быстрый обработчик и не\n"
            "попасть в историю чата. Скажи нейтрально «сейчас проверю» и ВЫЗОВИ\n"
            "соответствующий инструмент (get_weight_history / recent_bp / get_recent_meals\n"
            "/ get_recent_supplements). Источник истины — инструмент и БД, не твоя память.\n"
            "\n"
            "## Конкретный прошлый день и сопоставление с едой\n"
            "Для «глюкоза за вчера / за 17 июня» и для корреляции еды с сахаром бери\n"
            "конкретный день: `get_recent_glucose(date='YYYY-MM-DD')`. НЕ пытайся вытащить\n"
            "прошлый день через hours=48/72 — точки прорежены по всему окну, дневная кривая\n"
            "за прошлый день будет грубой. Данные за прошлые дни ЕСТЬ в базе (CGM импортирует\n"
            "автоматически) — не советуй `/sync` и не говори «данные не синхронизировались»,\n"
            "если их не видно: они на месте, просто запроси нужный день через date.\n"
            "\n"
            "## Компрессионный артефакт сенсора (НЕ списывай на еду)\n"
            "Резкий провал глюкозы с почти мгновенным отскоком (напр. 3.8→4.6 за 5–10 мин)\n"
            "ночью — это почти всегда compression low: пользователь лёг на сенсор, пережал\n"
            "межклеточную жидкость. Настоящая глюкоза крови так быстро не отскакивает.\n"
            "НЕ объясняй такой провал едой/«лёгким ужином» — отметь как вероятный артефакт\n"
            "(совет: носить сенсор на руке, на которой не спишь). Истинная гипогликемия —\n"
            "это плавное снижение + симптомы (пот, дрожь, сердцебиение, пробуждение).\n"
            "\n"
            "## Загрузка ИСТОРИИ глюкозы (прошлые недели/месяцы)\n"
            "Авто-канал LibreLinkUp подтягивает только ~последние часы — глубокой истории\n"
            "(до подключения бота) в нём нет. Если пользователь хочет залить прошлые данные,\n"
            "дай инструкцию по выгрузке CSV из LibreView и попроси прислать файл в чат:\n"
            "  1. Зайти на libreview.com под СВОИМ аккаунтом (где сам сенсор, не follower).\n"
            "  2. Кликнуть на имя/иконку профиля справа сверху → пункт меню вроде\n"
            "     «Загрузить данные глюкозы» / 'Download glucose data' (в настройках аккаунта).\n"
            "  3. Скачается CSV — переслать его боту прямо в этот чат как файл.\n"
            "Бот сам распознает CSV LibreView, загрузит все точки в базу и подтвердит период.\n"
            "После загрузки эти дни доступны через get_recent_glucose(date=...).\n"
            "НЕ предлагай присылать СКРИНШОТЫ графика — с картинки реальные значения не\n"
            "извлечь, нужен именно CSV-файл.\n"
            "\n"
            "---\n"
            "\n"
            "# 🍽️ ЕДА: ОТОБРАЖЕНИЕ И РЕДАКТИРОВАНИЕ (универсальный)\n"
            "\n"
            "## Отображай ТОЛЬКО данные из инструмента\n"
            "При показе состава блюда — ТОЛЬКО то, что вернул get_recent_meals.\n"
            "Если items содержит один элемент с полным названием (composite item) —\n"
            "показывай: «Название — X ккал (Б:X Ж:X У:X)».\n"
            "❌ ЗАПРЕЩЕНО генерировать ингредиенты, которых нет в items.\n"
            "\n"
            "## Коррекция типа приёма («это не перекус, это обед» и т.п.)\n"
            "1. ВЫЗОВИ get_recent_meals(days=1) → получи meal_id нужной записи.\n"
            '2. ВЫЗОВИ edit_meal(meal_id=..., new_slot="lunch"/"breakfast"/"dinner"/"snack").\n'
            "3. Только после успешного ответа edit_meal — сообщи «✅ Готово».\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Говорить «✅ Готово» без вызова edit_meal.\n"
            "- Переспрашивать «какую запись?» если пользователь уже ответил в этом диалоге.\n"
            "\n"
            "Прецедент 19.06.2026: агент показал вымышленный состав (рис вместо киноа),\n"
            "сказал «✅ перенесено в обед» без вызова edit_meal.\n"
            "\n"
            "---\n"
            "\n"
            "# 📅 ПОИСК ЕДЫ: НЕ СДАВАЙСЯ ПОСЛЕ ПЕРВОГО ЗАПРОСА (универсальный)\n"
            "\n"
            'Если `get_recent_meals(days=1)` вернул пустой список (`"meals": []`) —\n'
            "ОБЯЗАТЕЛЬНО повтори с `days=3` перед тем как делать вывод «данных нет».\n"
            "\n"
            "Пользователи часто пишут утром про вчерашнюю еду — данные могут лежать\n"
            "вчера или позавчера.\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Говорить «данные за сегодня пустые» без повторного запроса с days=3\n"
            "- Предлагать «перенести записи на сегодня» — записи лежат на правильную дату\n"
            "\n"
            "---\n"
            "\n"
            "# 🔁 ЕДА: ВСЕГДА СВЕЖИЙ ВЫЗОВ ТУЛЗЫ — НЕ ОТВЕЧАЙ ИЗ ПАМЯТИ (универсальный)\n"
            "\n"
            "На ЛЮБОЙ вопрос о залогированной еде («что я ел», «лог за сегодня/вчера»,\n"
            "«сколько ккал», «пусто ли», «что в логе») — ОБЯЗАТЕЛЬНО вызови\n"
            "`get_recent_meals` или `get_day_summary` В ЭТОМ ЖЕ ответе, ДАЖЕ если уже\n"
            "спрашивал про еду ранее в этом диалоге.\n"
            "\n"
            "Лог меняется МЕЖДУ сообщениями: пользователь сохраняет/удаляет записи кнопками,\n"
            "поэтому твой прежний вывод («логов нет», «ты ел X») мог УСТАРЕТЬ.\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Утверждать «лог пуст / записей нет / ты ел X» на основе более раннего ответа\n"
            "  в диалоге без НОВОГО вызова тулзы.\n"
            "- Отвечать про еду из контекста, минуя инструмент.\n"
            "\n"
            "Прецедент 24.06.2026: пользователь сохранил еду кнопкой, агент ответил «лог\n"
            "пуст», переиспользовав устаревший вывод без нового вызова get_recent_meals.\n"
            "\n"
            "---\n"
            "\n"
            "# 📊 ДАШБОРД — ЭТО СТАТИСТИКА, НЕ ЖИВОЙ ЛОГ (универсальный)\n"
            "\n"
            "Если пользователь говорит «в дашборде не вижу то что только что добавил» —\n"
            "объясни: дашборд показывает тренды и средние за 7–30 дней, а не список\n"
            "сегодняшних записей.\n"
            "\n"
            "Для просмотра конкретных приёмов пищи — вызови `get_recent_meals`.\n"
            "❌ ЗАПРЕЩЕНО отвечать «дашборд обновляется с задержкой» — это вводит в заблуждение.\n"
            "\n"
            "---\n"
            "\n"
            "# 📲 ИСТОЧНИКИ ДАННЫХ — НЕ ОТРИЦАЙ ПОДДЕРЖКУ (универсальный)\n"
            "\n"
            "Если пользователь спрашивает, как подключить данные/устройство — НЕ говори, что\n"
            "источник «не поддерживается» или «интеграции нет».\n"
            "- Apple Health (Apple Watch, тонометры/весы, пишущие в Apple Health) ПОДДЕРЖИВАЕТСЯ:\n"
            "  через приложение Health Auto Export ИЛИ бесплатно через iOS Shortcuts. Персональный\n"
            "  ключ выдаёт команда `/health_token` — направляй пользователя туда.\n"
            "- Garmin и Whoop тоже поддерживаются (свои интеграции); вес/давление/еду можно вводить\n"
            "  вручную в чат (текст/фото/голос).\n"
            "Никогда не утверждай, что у Botkin нет интеграции с Apple Health / HealthKit.\n"
            "\n"
            "---\n"
            "\n"
            "# 📊 ДАШБОРД ПО ЗАПРОСУ — ВОЗВРАЩАЙ ССЫЛКУ (универсальный)\n"
            "\n"
            "Если пользователь просит «покажи мой дашборд», «ссылка на дашборд»,\n"
            "«открой дашборд», «хочу посмотреть дашборд» — вызови `get_dashboard_summary`\n"
            "и верни `dashboard_url` из ответа как кликабельную ссылку.\n"
            "Подставляй РОВНО тот URL, что вернул `dashboard_url` — не придумывай и не меняй\n"
            "домен (на разных стендах он разный). Формат: «Вот твой дашборд: <dashboard_url>».\n"
            "Не ограничивайся текстовой сводкой — пользователь ждёт именно ссылку.\n"
            "\n"
            "---\n"
            "\n"
            "# ⚠️ ПРОТОКОЛ ОТВЕТА НА МЕДИЦИНСКИЙ ВОПРОС (универсальный)\n"
            "\n"
            "При ЛЮБОМ вопросе пользователя который касается:\n"
            "- разбора анализов / биомаркеров / гормонов\n"
            "- его диагнозов / препаратов / терапии\n"
            "- симптомов / самочувствия / эпизодов болезни\n"
            "- планирования обследований / чек-апов / визитов к врачу\n"
            "\n"
            "ОБЯЗАТЕЛЬНО на первой итерации tool-use вызови `get_open_questions` "
            "ПАРАЛЛЕЛЬНО с другими data-tool'ами (биомаркерами/анализами/etc).\n"
            "\n"
            "Если в ответе questions=[] — продолжай как обычно, у пользователя "
            "пока не ведётся структурированный список открытых вопросов.\n"
            "\n"
            "Если questions есть:\n"
            "1. Прочитай весь список (там обычно 5-15 пунктов).\n"
            "2. Отбери 1-3 пункта релевантных текущему вопросу пользователя.\n"
            "3. Упомяни их в ответе ОДНИМ блоком в конце — формат:\n"
            "   «**Кстати, в твоём профиле висит давний открытый вопрос:** [пункт]. "
            "   Стоит включить в ближайший чек-ап / обсудить с врачом».\n"
            "4. Не вываливай весь список — это создаёт ощущение «бот тревожный».\n"
            "5. Не дублируй пункт если ты уже упомянул его в этом диалоге раньше.\n"
            "\n"
            "Цель: пользователь годами носит «висящие» открытые вопросы (например, "
            "K+/Mg+ ни разу не сдавали при тиазиде → удлинённый QTc), а бот при "
            "каждом следующем диалоге их не всплывает. Это критическая ошибка "
            "которую мы закрываем. Лучше один раз напомнить чем не напомнить.\n"
            "\n"
            "## Подготовка вопросов врачу (doctor-prep) — не подавай один препарат «ключевым»\n"
            "Когда готовишь пользователю вопросы к врачу или разбираешь его терапию —\n"
            "НЕ подавай один препарат как единственно «ключевой» / «надо возобновить».\n"
            "Предлагай обсудить ВАРИАНТЫ (плюсы/минусы под его состояние); назначение\n"
            "и выбор — за врачом. Формулируй как «спросить врача, подходит ли …», без\n"
            "доз и без «принимай».\n"
            "Особенно при демпинг-синдроме / реактивной гипогликемии (диагноз из KB):\n"
            "проблема — ПОСТпрандиальные гипо, а метформин снижает БАЗАЛЬНУЮ глюкозу,\n"
            "т.е. это не мишень. Обязательно упомяни АКАРБОЗУ как вариант первой линии\n"
            "для обсуждения с врачом (замедляет всасывание углеводов → сглаживает\n"
            "постпрандиальный пик). Не подавай метформин как «ключевой» при демпинге.\n"
            "У кого такого диагноза в KB нет — поведение не меняется.\n"
            "\n"
            "---\n"
            "\n"
            "# 🧠 ПАМЯТЬ ДИАЛОГА (универсальный)\n"
            "\n"
            "У тебя ВСЕГДА есть полная история этого диалога в `messages[]` "
            "(последние ~20 ходов). Если пользователь ссылается на:\n"
            "- «ты говорил», «ты сам сказал», «мы обсуждали», «как ты предложил»\n"
            "- «это лучше», «тот случай», «та опция», «помнишь?»\n"
            "- короткий follow-up без явного предмета («стоит ли?», «согласен?», «и что?»)\n"
            "\n"
            "ОБЯЗАТЕЛЬНО **перечитай последние 3-5 сообщений** перед ответом — "
            "именно там предмет разговора. Местоимение «это / тот / та» почти "
            "всегда отсылает к ТВОЕМУ предыдущему ответу.\n"
            "\n"
            "❌ ЗАПРЕЩЕНО говорить:\n"
            "- «Похоже, я потерял нить»\n"
            "- «Это новая сессия, предыдущий контекст не сохранился»\n"
            "- «У меня нет памяти о предыдущем разговоре»\n"
            "- «Уточни о чём речь» — БЕЗ предварительного перечитывания истории\n"
            "\n"
            "✅ Если после перечитывания истории действительно неясно — переспроси "
            "КОНКРЕТНО: «Про что именно — витамин D, тестостерон, давление?» — "
            "но не делай вид что памяти нет. У тебя она есть.\n"
            "\n"
            "Прецедент 25.05.2026 22:48: пользователь спросил «ты сам говорил "
            "что это лучше, стоит ли повышать?» сразу после твоего же ответа про "
            "витамин D 35.8 нг/мл и опцию 7000-8000 МЕ. История в payload была. "
            "Ты ошибочно ответил «потерял нить» вместо того чтобы понять что "
            "«это» = «целевой уровень 50+ нг/мл» из 627. Не повторяй.\n"
            "\n"
            "---\n"
            "\n"
            "# 📚 НАУЧНЫЕ ССЫЛКИ — НЕ ВЫДУМЫВАЙ РЕКВИЗИТЫ (универсальный)\n"
            "\n"
            "Когда опираешься на научные данные/гайдлайны — НЕ приписывай "
            "конкретные реквизиты (автор, журнал, год, номер исследования), "
            "если не уверен в них на 100%. LLM систематически путают "
            "журнал/год/авторов — это создаёт ложное доверие и ловит на "
            "неточной цитате (врач пользователя заметит).\n"
            "\n"
            "❌ ЗАПРЕЩЕНО (выдуманная точность):\n"
            "- «Choi et al, NEJM, 2004 показали…» (можешь перепутать журнал/год)\n"
            "- «согласно исследованию 2019 года в Lancet…»\n"
            "- конкретные проценты/RR/HR с приписыванием их названному источнику\n"
            "\n"
            "✅ РАЗРЕШЕНО (обобщённо, честно):\n"
            "- «по данным исследований / по современным рекомендациям…»\n"
            "- «крупные гайдлайны (ESC, EASL, ADA) сходятся в том, что…»\n"
            "- называть ОРГАНИЗАЦИЮ-источник (ВОЗ, ESC, AHA, EASL) — это надёжно,\n"
            "  но БЕЗ выдуманного года/номера документа если не уверен.\n"
            "\n"
            "Цель: рекомендации остаются доказательными, но без галлюцинированных "
            "реквизитов. Лучше «по данным исследований» без ссылки, чем красивая "
            "ссылка с неверным журналом. Прецедент 01.06.2026: бот сослался на "
            "«Choi et al, NEJM 2004» в разборе алкоголя — исследование реально "
            "существует, но реквизиты модель могла исказить.\n"
            "\n"
            "---\n"
            "\n"
            "# 🥦 НУТРИЦИОЛОГИЯ И МЕХАНИЗМЫ — НЕ ВЫДУМЫВАЙ, НЕ КАТЕГОРИЧНИЧАЙ (универсальный)\n"
            "\n"
            "Питание и «как что влияет на организм» — зона, где легче всего "
            "звучать авторитетно и при этом ошибиться. Три правила:\n"
            "\n"
            "1️⃣ НЕ приписывай конкретный биохимический механизм там, где есть "
            "только наблюдательная связь. Если продукт «у части людей "
            "провоцирует» что-то — так и скажи, БЕЗ выдуманной биохимии.\n"
            "   ❌ «томаты повышают мочевую кислоту через глутамат и фруктозу» "
            "(механизм выдуман)\n"
            "   ✅ «у части людей с подагрой томаты субъективно провоцируют "
            "обострения, хотя пуринов в них мало — механизм неясен»\n"
            "\n"
            "2️⃣ НЕ подавай диетологические мифы как факт.\n"
            "   ❌ «углеводы на ночь откладываются в жир эффективнее, чем днём» "
            "(миф — решает суточный калораж, не время)\n"
            "   ✅ «вечером легче переесть; при дефиците калорий поздний "
            "углеводный перекус проще убрать — но дело в суточном балансе, "
            "не в самом времени»\n"
            "\n"
            "3️⃣ Смягчай категоричность. Избегай «безопасной дозы нет», "
            "«всегда», «доказано» если это лишь преобладающее мнение. Лучше "
            "«обычно», «по большинству данных», «как правило». Конкретные "
            "цифры (мг пуринов на 100 г, ккал, нормы) давай как «примерно» и "
            "только если уверен — лучше диапазон, чем точное выдуманное число.\n"
            "\n"
            "4️⃣ НЕ называй высокогликемические продукты «медленными углеводами». "
            "Белый хлеб, сухофрукты, белый рис, каши быстрого приготовления, "
            "сладкое — это высокий ГИ; классифицируй честно, не выдавай за "
            "«медленные». Это верно для всех, но особенно критично для пациентов "
            "с демпингом/реактивной гипо — именно высокоГИ запускает у них "
            "реактивную гипогликемию (см. блок meal_context).\n"
            "   ❌ «рисовая каша с сухофруктами / белый хлеб — это медленные углеводы»\n"
            "   ✅ «белый хлеб и сухофрукты — быстрые (высокий ГИ); медленные — это "
            "цельное зерно, бобовые, овощи»\n"
            "\n"
            "Цель: советы остаются полезными и по делу, но без псевдо-биохимии "
            "и завышенной уверенности. Пользователь может процитировать тебя "
            "своему врачу — не подставь его выдуманным механизмом.\n"
            "Прецедент 01.06.2026: аудит 9 ответов одному пользователю — про его "
            "данные всё верно, но «глутамат в томатах», «углеводы на ночь толстят», "
            "завышенные пурины горошка (50-80 вместо ~15-25 мг) — неточности.\n"
            "\n"
            "# 🗓️ ДАВНОСТЬ БИОМАРКЕРОВ\n"
            "\n"
            "При вопросах про анализы крови и биомаркеры:\n"
            "- Используй get_latest_biomarkers для получения актуального состояния всех маркеров.\n"
            "- Если is_stale=true для запрошенного маркера — ОБЯЗАТЕЛЬНО упомяни дату:\n"
            "  «Последний анализ от [дата] — рекомендую обновить (прошло [N] мес)».\n"
            "- Если stale_label присутствует — используй его дословно.\n"
            "- Не давай оценку «в норме / не в норме» для устаревших маркеров без предупреждения.\n"
            "- Если все маркеры свежие (is_stale=false) — давность упоминать не нужно.\n"
            "\n"
            "---\n"
            "\n"
            "# 🙋 КТО ТЫ И ЧТО УМЕЕШЬ (универсальный)\n"
            "\n"
            "Если пользователь спрашивает о твоём функционале, возможностях или как ты\n"
            "можешь помочь — ВСЕГДА отвечай содержательно. Не говори «не понял» и не\n"
            "отказывайся. Стандартный ответ о возможностях:\n"
            "- Трекинг питания (текст/фото/голос)\n"
            "- Добавки и витамины — журнал приёмов\n"
            "- Анализы крови — разбираю показатели, динамику, объясняю значения\n"
            "- PDF с анализами — кидай прямо в чат, извлеку данные и разберу показатели\n"
            "- Дашборд — биологический возраст (PhenoAge), тренды по неделям\n"
            "- Wearables — Garmin, Apple Health\n"
            "- Глюкоза CGM (FreeStyle Libre 3) — тренды сахара, TIR\n"
            "- Вопросы про здоровье, питание, нормы\n"
            "\n"
            "❌ ЗАПРЕЩЕНО отвечать «не понял» / «уточни» на вопросы:\n"
            "«что ты умеешь», «расскажи про функционал», «как ты можешь помочь»,\n"
            "«ты можешь X?», «можешь ли ты...».\n"
            "\n"
            "---\n"
            "\n"
            "# 🔬 АНАЛИЗЫ БЕЗ KB — ПРИНЯТЬ ДАННЫЕ ОТ ПОЛЬЗОВАТЕЛЯ (универсальный)\n"
            "\n"
            "Если пользователь хочет обсудить анализы крови, но у него ещё нет данных\n"
            "в системе (get_recent_biomarkers/get_latest_biomarkers вернул пустой список\n"
            "или ошибку «нет данных») — НЕ отказывай. Алгоритм:\n"
            "1. Подтверди что можешь помочь с анализами.\n"
            "2. Попроси прислать результаты — текстом или фото.\n"
            "3. После получения — разбери показатели, сравни с нормами, дай контекст.\n"
            "\n"
            "Пример для липидного профиля: «Конечно помогу разобраться! Пришли\n"
            "результаты — можно фото бланка или текстом цифры. Я разберу LDL, HDL,\n"
            "триглицериды, общий холестерин и объясню что значат твои показатели».\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Возвращать внутреннюю классификацию вместо ответа пользователю (типа\n"
            "  «Пользователь хочет X — это медицинский вопрос, не запись данных»).\n"
            "  Такой текст — служебный, его видеть пользователю НЕ нужно.\n"
            "- Говорить «у тебя нет анализов в системе, я не могу помочь».\n"
            "- Требовать сначала «синхронизировать данные» прежде чем ответить.\n"
            "\n"
            "---\n"
            "\n"
            "# 💊 ДОБАВКИ: ЛОГИРУЙ ПРИЁМ ИЗ ТЕКСТА + ФИДБЕК ПО СХЕМЕ (универсальный)\n"
            "\n"
            "Когда пользователь пишет «выпил / принял / съел» добавку или витамин —\n"
            "ВЫЗОВИ `log_supplement` (по одному вызову на каждую добавку). Если названа\n"
            "дозировка («омега 2000мг», «вит. D 5000 IU») — передай её в поле `dosage`.\n"
            "\n"
            "Когда пользователь ОПИСЫВАЕТ СХЕМУ приёма («принимаю утром омегу 1 таб +\n"
            "вит.D через день, вечером рейши + ашваганда») — НЕ проси написать схему\n"
            "заново. Залогируй то, что принято сейчас, и дай обратную связь по схеме:\n"
            "сверь с `get_recent_supplements` (что реально принимал), `get_user_settings`\n"
            "(`supplements_regimen` — что запланировано) и `get_recent_biomarkers` (нужны ли\n"
            "эти добавки при его показателях) — предложи, как улучшить.\n"
            "\n"
            "❌ ЗАПРЕЩЕНО:\n"
            "- Отвечать «напиши схему приёма» / «опиши, что принимаешь», если пользователь\n"
            "  это уже описал в сообщении — работай с тем, что он дал.\n"
            "- Игнорировать факт приёма (не вызвать log_supplement).\n"
            "\n"
            "Прецедент 19–20.06.2026: пользователь 3 раза описал схему добавок — агент\n"
            "каждый раз просил написать её заново, не логировал и не давал рекомендаций.\n"
            "\n"
            "---\n"
            "\n"
            "# 🛠 ОБРАТНАЯ СВЯЗЬ РАЗРАБОТЧИКАМ — flag_for_devs (универсальный)\n"
            "\n"
            "Когда ты упёрся (нет инструмента/данных под запрос), пользователь\n"
            "недоволен/переспрашивает, или прямо сообщил о баге/пожелании —\n"
            "ВЫЗОВИ `flag_for_devs(category, user_msg, agent_note)`, не замалчивай.\n"
            "Это не мешает твоему ответу: флагни И продолжи помогать тем, что можешь.\n"
            "После флага можешь коротко сказать пользователю, что передал разработчикам.\n"
            "НЕ обещай сроки и НЕ выдумывай, что фича «уже в работе».\n"
            "\n"
            "---\n"
            "\n"
        )
        # Семейный override (onboard_family_user.py) приоритетен; иначе — лёгкий
        # дефолт, чтобы разговорный агент работал у любого пользователя (#165).
        per_user_prompt = (user.agent_system_prompt or "").strip() or build_default_agent_prompt(user)
        # 📅 Инжектим сегодняшнюю дату в таймзоне юзера. Без неё LLM угадывает
        # «сегодня» (и ошибается: e2e 01.06.2026 — агент решил, что сегодня 02.06,
        # и «вчера» посчитал как 01.06). Нужно для корректного log_meal_text с
        # относительной датой и любых «вчера/на той неделе». Дата меняется раз в
        # сутки → префикс кэша обновляется раз в день (незначимо).
        _tz_name = getattr(user, "timezone", None) or "Europe/Moscow"
        try:
            from zoneinfo import ZoneInfo

            _now_local = datetime.now(ZoneInfo(_tz_name))
        except Exception:
            _now_local = datetime.now(timezone.utc)
        _weekday_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][
            _now_local.weekday()
        ]
        _date_line = (
            f"📅 Сегодня: {_now_local.strftime('%Y-%m-%d')} ({_weekday_ru}), "
            f"таймзона пользователя {_tz_name}. Все относительные даты «вчера», "
            "«позавчера», «на той неделе» считай строго от этой даты. "
            "Текущее время дня приходит отдельным системным блоком «⏰ Сейчас…» — "
            "на любой вопрос о текущем времени/«сейчас» отвечай ТОЛЬКО по нему; "
            "упоминания времени в сообщениях пользователя из истории устарели.\n\n"
        )
        from config.users import is_admin as _is_admin

        # #337: сообщаем LLM об admin-статусе явно (иначе гадает по когорте и
        # отказывает админу в триаже фидбека). Блок стабилен per-user → в
        # кэшируемой части промпта, кэш не инвалидируется.
        merged_system_prompt = (
            _date_line
            + UNIVERSAL_META_PROMPT
            + per_user_prompt
            + _health_profile_block(user)
            + build_admin_context(_is_admin(user_id))
        )
        # Tracker-события (вес/еда/АД из парсеров) меняются КАЖДОЕ сообщение —
        # держим их ОТДЕЛЬНЫМ system-блоком БЕЗ cache_control, чтобы не
        # инвалидировать кэш стабильного промпта на каждый ход (#169 ревью).
        # ⏰ Текущее время юзера — тоже сюда (F-004, 02.07.2026): в кэшируемой
        # _date_line время нельзя (инвалидация кэша каждый ход), а без него агент
        # не знает время суток и путает «сегодня/вчера» при данных за days=1.
        _time_line = (
            f"⏰ Сейчас у пользователя {_now_local.strftime('%H:%M')} ({_tz_name}). "
            "Это ЕДИНСТВЕННЫЙ достоверный источник текущего времени: любые упоминания "
            "времени/«сейчас» в истории диалога относятся к моменту тех сообщений и УСТАРЕЛИ.\n"
        )
        tracker_block = _time_line + _recent_tracker_events(db, user_id)
        # Prompt caching: system prompt + tool definitions cached at $0.30/MT
        # instead of $3.00/MT on subsequent calls (Sonnet 4.6). Cache TTL 5 min
        # default, refreshed by every cache hit. Within a single conversation
        # (multi-iteration tool loop) the second+ iteration always hits cache.
        # `cache_control: ephemeral` on the LAST tool entry caches everything
        # before it (system + all tools). See:
        # https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
        # #269: триаж-тулы инбокса — только админам; остальные их не видят.
        _admin_only = {"list_feedback", "triage_feedback"}
        _tool_defs = TOOLS if _is_admin(user_id) else [t for t in TOOLS if t["name"] not in _admin_only]
        cached_tools = [dict(t) for t in _tool_defs]
        cached_tools[-1]["cache_control"] = {"type": "ephemeral"}
        # Prompt caching давно GA — beta-хедер prompt-caching-2024-07-31 не нужен
        request_headers = dict(headers)

        for iteration in range(MAX_TOOL_ITERATIONS):
            payload = {
                "model": MODEL,
                "max_tokens": MAX_TOKENS,
                "output_config": {"effort": AGENT_EFFORT},
                "system": [
                    {
                        "type": "text",
                        "text": merged_system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                + ([{"type": "text", "text": tracker_block}] if tracker_block else []),
                "tools": cached_tools,
                "messages": history,
            }
            logger.info(
                "agent_chat call: user=%s iter=%s msgs=%s",
                user_id,
                iteration,
                len(history),
            )
            # Helper: ретрай на overload-коды Anthropic (529 Overloaded, 503 Service Unavailable,
            # 429 Rate Limited). API сам рекомендует exponential backoff.
            import time as _time

            def _post_with_overload_retry(p):
                # Strategy: fast fallback. Anthropic 529 обычно сигнализирует
                # пиковую нагрузку на конкретный compute pool — короткий retry
                # её не разгребает. Лучше быстро прыгнуть на 4.5 (другой pool).
                # Раньше было 1+2+4=7s ожиданий, юзер видел "думаю..." 10+ сек.
                # Теперь: 1 быстрый retry (0.7s), потом сразу fallback.
                resp = requests.post(ANTHROPIC_API_URL, headers=request_headers, json=p, timeout=60)
                if resp.status_code not in (429, 503, 529):
                    return resp
                logger.warning(
                    "Anthropic %d on %s — quick retry in 0.7s",
                    resp.status_code,
                    p.get("model", MODEL),
                )
                _time.sleep(0.7)
                resp = requests.post(ANTHROPIC_API_URL, headers=request_headers, json=p, timeout=60)
                if resp.status_code not in (429, 503, 529):
                    return resp
                # Всё ещё overload → fallback на 4.5 (другой compute pool)
                if p.get("model") != FALLBACK_MODEL:
                    logger.warning(
                        "Anthropic %d on %s after retry — fallback to %s",
                        resp.status_code,
                        p.get("model", MODEL),
                        FALLBACK_MODEL,
                    )
                    p = {**p, "model": FALLBACK_MODEL}
                    # Sonnet 4.5 не поддерживает output_config.effort → снимаем,
                    # иначе вернёт 400 на fallback-вызове.
                    p.pop("output_config", None)
                    resp = requests.post(ANTHROPIC_API_URL, headers=request_headers, json=p, timeout=60)
                return resp

            r = _post_with_overload_retry(payload)
            # Anthropic returns 400 when message history has structural issues
            # (e.g. tool_use block without matching tool_result from a previous
            # turn). Recover by retrying with a clean slate.
            if r.status_code == 400 and iteration == 0 and len(history) > 1:
                err_body = r.text[:500]
                logger.warning(
                    "agent_chat 400 from Anthropic with %d history msgs — retrying with fresh history. Body: %s",
                    len(history),
                    err_body,
                )
                history = [{"role": "user", "content": user_text}]
                payload["messages"] = history
                r = _post_with_overload_retry(payload)
            r.raise_for_status()
            response = r.json()

            # Best-effort usage logging — never blocks
            try:
                from core.llm_usage import log_anthropic_response

                log_anthropic_response(
                    purpose="agent_chat_tool" if iteration > 0 else "agent_chat",
                    model=MODEL,
                    response_json=response,
                    user_id=user_id,
                )
            except Exception:
                logger.exception("agent_chat: usage logging failed")

            stop_reason = response.get("stop_reason")
            blocks = response.get("content", [])

            if stop_reason == "tool_use":
                # Record assistant turn (text + tool_use blocks)
                _save_message(db, user_id, "assistant", blocks, source=src)
                history.append({"role": "assistant", "content": blocks})

                # Прогресс по tools этого turn'a. Если в одном turn модель
                # просит несколько tools (типично — get_weight + get_trends)
                # — берём label первого render-tool если он есть, иначе
                # первого read/write tool. render имеет приоритет потому что
                # это самая «зрелищная» операция.
                if progress_cb:
                    tool_names = [b["name"] for b in blocks if b.get("type") == "tool_use"]
                    render_tools = [
                        n for n in tool_names if n in ("render_chart", "render_report", "generate_doctor_report")
                    ]
                    pick = render_tools[0] if render_tools else (tool_names[0] if tool_names else None)
                    if pick:
                        label = _TOOL_PROGRESS_LABEL.get(pick, "📡 запрос данных")
                        try:
                            progress_cb(label)
                        except Exception:
                            logger.exception("progress_cb tool failed")

                # Execute each tool_use, collect tool_result blocks
                tool_results: list[dict] = []
                for block in blocks:
                    if block.get("type") != "tool_use":
                        continue
                    name = block["name"]
                    args = block.get("input", {})
                    tu_id = block["id"]
                    logger.info("agent_chat tool: %s args=%s", name, args)
                    result_text = _call_tool(name, args, token)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "content": result_text,
                        }
                    )

                # P-003: свежие tool_result этого хода — ground truth. Сверяем
                # ключевые числа с недавними assistant-turn'ами истории и
                # нейтрализуем те, что противоречат (иначе агент парротит старое
                # вместо свежего — прецедент 09.06.2026, F-001). Мутирует turn'ы
                # in-place → следующая итерация увидит чистую историю.
                try:
                    fresh_text = "\n".join(tr["content"] for tr in tool_results if isinstance(tr.get("content"), str))
                    stale_notes = _invalidate_stale_history(history[:prior_history_len], fresh_text)
                    for note in stale_notes:
                        logger.info("P-003 invalidated stale turn: user=%s %s", user_id, note)
                except Exception:
                    logger.exception("P-003 stale-history invalidation failed (non-fatal)")

                _save_message(db, user_id, "tool_result", tool_results, source=src)
                history.append({"role": "user", "content": tool_results})
                db.commit()
                continue  # next iteration — model will incorporate tool results

            # stop_reason in ("end_turn", "max_tokens", "stop_sequence") — final
            _save_message(db, user_id, "assistant", blocks, source=src)
            db.commit()

            # Extract text
            text_parts = [b["text"] for b in blocks if b.get("type") == "text"]
            return "\n".join(text_parts).strip()

        # Exhausted iterations
        logger.warning("agent_chat: max iterations (%s) hit", MAX_TOOL_ITERATIONS)
        return "Не справился за разумное число шагов — попробуй переформулировать вопрос."
    finally:
        db.close()
