"""PDF-отчёт для врача (#290) — сборка данных и рендер HTML.

Модель содержания — International Patient Summary (ISO 27269): секции идут в
клиническом порядке Проблемы → Аллергии → Лекарства → Результаты → Витальные →
Образ жизни. Артефакт — человекочитаемый HTML (→ PDF в render_doctor_report_pdf, Ф2).

Инварианты:
- только чтение БД (никаких записей);
- SQL по `supplements_log` — всегда с фильтром user_id (иначе смешаются пользователи);
- биомаркеры канонизируются через kb_schema (`_load_biomarkers_from_db` → aggregate_biomarkers);
- частичные секции (проблемы/аллергии/лекарства из онбординга) помечаются «со слов пользователя».
"""

from __future__ import annotations

import html
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Presentation-слой биомаркеров (label/unit/референсы) — то, чего нет в kb_schema.
# Переиспользуем, чтобы не дублировать RU-названия и границы норм.
from core.health.onboarding_lists import (
    ALLERGY_KEYS,
    CONDITION_KEYS,
    onboarding_list as _onboarding_list,
    split_freetext as _split_freetext,  # noqa: F401 (re-export: tests import it from here)
)
from core.reports.biomarker_dynamics import MARKER_CONFIG
from services.report_i18n import (
    CHROME,
    FREE_TEXT_SECTIONS,
    translate_freetext,
    transliterate_ru_to_latin,
)

# Окно «недавних» добавок и активности.
_SUPPLEMENTS_WINDOW_DAYS = 90
_ACTIVITY_WINDOW_DAYS = 30

# Дисклеймер — единый источник в словаре каркаса (CHROME[lang]["disclaimer"], #300).

# Клинический порядок секций (IPS / ISO 27269).
SECTION_ORDER: list[tuple[str, str]] = [
    ("problems", "Проблемы и диагнозы"),
    ("allergies", "Аллергии и непереносимости"),
    ("medications", "Лекарства и добавки"),
    ("results", "Результаты исследований"),
    ("vitals", "Витальные показатели"),
    ("social", "Образ жизни"),
]


@dataclass
class ReportSection:
    key: str
    title: str
    items: list[str] = field(default_factory=list)
    self_reported: bool = False
    empty_note: str = "Нет данных"


@dataclass
class DoctorReport:
    patient_label: str
    generated_at: str
    period: str
    sections: list[ReportSection]


def _esc(value: object) -> str:
    return html.escape(str(value))


# ── Рендер (чистый, без БД) ──────────────────────────────────────────────────


def render_doctor_report_html(report: DoctorReport, lang: str = "ru") -> str:
    """DoctorReport → печатный A4 HTML в клиническом порядке секций (каркас на lang)."""
    chrome = CHROME.get(lang, CHROME["ru"])
    blocks: list[str] = []
    for section in report.sections:
        note = f' <span class="self">{_esc(chrome["self_reported"])}</span>' if section.self_reported else ""
        # Буллет — инлайн-текст внутри <li> (list-style:none), а не нативный ::marker:
        # WeasyPrint кладёт нативные маркеры в текстовый слой отдельными фрагментами,
        # оторванными от текста, и при извлечении они кластеризуются в «•••…» в конце
        # страницы (#297). Пустые/пробельные элементы отфильтровываем, чтобы не рождать
        # <li> без содержимого; секция из одних пустых → empty_note, а не пустой <ul>.
        items = [it for it in section.items if str(it).strip()]
        if items:
            lis = "\n".join(f"      <li>• {_esc(it)}</li>" for it in items)
            body = f"    <ul>\n{lis}\n    </ul>"
        else:
            body = f'    <p class="empty">{_esc(section.empty_note)}</p>'
        blocks.append(f"  <section>\n    <h2>{_esc(section.title)}{note}</h2>\n{body}\n  </section>")

    sections_html = "\n".join(blocks)
    return f"""<!doctype html>
<html lang="{chrome["html_lang"]}">
<head>
<meta charset="utf-8">
<title>{_esc(chrome["report_title"])} — {_esc(report.patient_label)}</title>
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{ font-family: "DejaVu Sans", sans-serif; font-size: 11pt; color: #1a1a1a; line-height: 1.4; }}
  header {{ border-bottom: 2px solid #333; padding-bottom: 8px; margin-bottom: 14px; }}
  header h1 {{ font-size: 15pt; margin: 0 0 4px; }}
  header .meta {{ font-size: 9.5pt; color: #555; }}
  section {{ margin: 12px 0; page-break-inside: avoid; }}
  h2 {{ font-size: 12pt; border-bottom: 1px solid #ccc; padding-bottom: 3px; margin: 0 0 6px; }}
  ul {{ list-style: none; margin: 4px 0 4px 18px; padding: 0; }}
  li {{ margin: 2px 0; padding-left: 1em; text-indent: -1em; }}
  .self {{ font-size: 8.5pt; font-weight: normal; color: #888; }}
  .empty {{ color: #999; font-style: italic; margin: 4px 0; }}
  .disclaimer {{ margin-top: 18px; padding-top: 8px; border-top: 1px solid #ccc;
                 font-size: 8.5pt; color: #777; }}
</style>
</head>
<body>
  <header>
    <h1>{_esc(chrome["report_title"])} — {_esc(report.patient_label)}</h1>
    <div class="meta">{_esc(chrome["generated"])}: {_esc(report.generated_at)} · {_esc(report.period)}</div>
  </header>
{sections_html}
  <p class="disclaimer">{_esc(chrome["disclaimer"])}</p>
</body>
</html>"""


# ── Сборка данных из БД ───────────────────────────────────────────────────────


def _ensure_bot_path() -> None:
    """telegram-bot/ в sys.path (там живёт dashboard_generator)."""
    bot_dir = Path(__file__).resolve().parent.parent / "telegram-bot"
    if str(bot_dir) not in sys.path:
        sys.path.insert(0, str(bot_dir))


def _coerce_dict(raw: object) -> dict:
    """onboarding_data может прийти dict (ORM/JSONB) или str (raw SQL в SQLite)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _problems(onboarding: Optional[dict]) -> list[str]:
    return _onboarding_list(onboarding, CONDITION_KEYS)


def _allergies(onboarding: Optional[dict]) -> list[str]:
    return _onboarding_list(onboarding, ALLERGY_KEYS)


def _medications(db: Session, user_id: int, onboarding: Optional[dict]) -> list[str]:
    """Лекарства из онбординга + недавние добавки из supplements_log (фильтр user_id)."""
    items = _onboarding_list(onboarding, ("current_medications", "medications"))
    since = date.today() - timedelta(days=_SUPPLEMENTS_WINDOW_DAYS)
    rows = db.execute(
        text(
            "SELECT supplement_name, dosage, MAX(date) AS last_date "
            "FROM supplements_log "
            "WHERE user_id = :uid AND date >= :since "
            "GROUP BY supplement_name, dosage "
            "ORDER BY last_date DESC"
        ),
        {"uid": user_id, "since": since},
    ).fetchall()
    for row in rows:
        name = row.supplement_name
        items.append(f"{name} — {row.dosage}" if row.dosage else str(name))
    return items


def _results(bio: dict, lang: str = "ru") -> list[str]:
    """Биомаркеры (канон) → строки label: value unit (date) [↑/↓ вне нормы]."""
    lines: list[tuple[str, str]] = []
    for canon, rec in bio.items():
        value = rec.get("value")
        if value is None:
            continue
        cfg = MARKER_CONFIG.get(canon, {})
        label = cfg.get("label_en", cfg.get("label", canon)) if lang == "en" else cfg.get("label", canon)
        unit = cfg.get("unit_en", cfg.get("unit", "")) if lang == "en" else cfg.get("unit", "")
        flag = ""
        low, high = cfg.get("ref_low"), cfg.get("ref_high")
        try:
            if high is not None and value > high:
                flag = " ↑"
            elif low is not None and value < low:
                flag = " ↓"
        except TypeError:
            flag = ""
        unit_part = f" {unit}" if unit else ""
        date_part = f" ({rec['date']})" if rec.get("date") else ""
        lines.append((label, f"{label}: {value}{unit_part}{date_part}{flag}"))
    return [line for _, line in sorted(lines, key=lambda x: x[0])]


def _vitals(db: Session, user_id: int, chrome: dict) -> list[str]:
    """Последние АД и вес/жир (по одному замеру — реальные, не средние)."""
    items: list[str] = []
    bp = db.execute(
        text(
            "SELECT systolic, diastolic, DATE(measured_at) AS d "
            "FROM blood_pressure_logs WHERE user_id = :uid "
            "ORDER BY measured_at DESC LIMIT 1"
        ),
        {"uid": user_id},
    ).fetchone()
    if bp and bp.systolic and bp.diastolic:
        items.append(f"{chrome['bp_label']}: {bp.systolic}/{bp.diastolic} {chrome['bp_unit']} ({bp.d})")
    w = db.execute(
        text(
            "SELECT weight, body_fat, DATE(measured_at) AS d "
            "FROM weights WHERE user_id = :uid ORDER BY measured_at DESC LIMIT 1"
        ),
        {"uid": user_id},
    ).fetchone()
    if w and w.weight is not None:
        fat = f", {chrome['fat_label']} {round(w.body_fat, 1)}%" if w.body_fat is not None else ""
        items.append(f"{chrome['weight_label']}: {round(w.weight, 1)} {chrome['weight_unit']}{fat} ({w.d})")
    return items


def _social(db: Session, user_id: int, chrome: dict) -> list[str]:
    """Лёгкая сводка образа жизни: тренировки за последний месяц."""
    since = date.today() - timedelta(days=_ACTIVITY_WINDOW_DAYS)
    n = db.execute(
        text("SELECT COUNT(*) AS n FROM workouts WHERE user_id = :uid AND date >= :since"),
        {"uid": user_id, "since": since},
    ).scalar()
    items: list[str] = []
    if n:
        items.append(chrome["workouts_tpl"].format(days=_ACTIVITY_WINDOW_DAYS, n=n))
    return items


def _load_biomarkers(db: Session, user_id: int) -> dict:
    """Канонизированные биомаркеры; при сбое импорта/чтения — пустой словарь."""
    try:
        _ensure_bot_path()
        from dashboard_generator import _load_biomarkers_from_db  # noqa: PLC0415

        return _load_biomarkers_from_db(db, user_id) or {}
    except Exception:
        return {}


def _translate_free_text_sections(sections: list[ReportSection], lang: str) -> None:
    """Один LLM-вызов на все free-text секции, затем разложить перевод обратно по индексам."""
    targets = [s for s in sections if s.key in FREE_TEXT_SECTIONS and s.items]
    flat: list[str] = []
    for s in targets:
        flat.extend(s.items)
    if not flat:
        return
    translated = translate_freetext(flat, lang)
    i = 0
    for s in targets:
        n = len(s.items)
        s.items = translated[i : i + n]
        i += n


def assemble_doctor_report(db: Session, user_id: int, lang: str = "ru") -> DoctorReport:
    """Собрать DoctorReport из БД в клиническом порядке секций IPS (каркас на lang)."""
    chrome = CHROME.get(lang, CHROME["ru"])
    user = db.execute(
        text("SELECT first_name, last_name, onboarding_data FROM users WHERE telegram_id = :uid"),
        {"uid": user_id},
    ).fetchone()
    onboarding = _coerce_dict(user.onboarding_data) if user else {}
    name_parts = [user.first_name, getattr(user, "last_name", None)] if user else []
    patient_label = " ".join(p for p in name_parts if p) or chrome["patient_fallback"]
    # В НЕ-русском отчёте имя не должно оставаться кириллицей (#1) — транслит в латиницу.
    if lang != "ru":
        patient_label = transliterate_ru_to_latin(patient_label)

    bio = _load_biomarkers(db, user_id)
    builders = {
        "problems": lambda: _problems(onboarding),
        "allergies": lambda: _allergies(onboarding),
        "medications": lambda: _medications(db, user_id, onboarding),
        "results": lambda: _results(bio, lang),
        "vitals": lambda: _vitals(db, user_id, chrome),
        "social": lambda: _social(db, user_id, chrome),
    }
    self_reported = {"problems", "allergies", "medications"}

    sections = [
        ReportSection(
            key=key,
            title=chrome["sections"][key],
            items=builders[key](),
            self_reported=key in self_reported,
            empty_note=chrome["no_data"],
        )
        for key, _title in SECTION_ORDER
    ]

    # Свободный текст (problems/allergies/medications) → один батч-LLM-перевод.
    if lang != "ru":
        _translate_free_text_sections(sections, lang)

    today = datetime.now(timezone.utc).date().isoformat()
    return DoctorReport(
        patient_label=patient_label,
        generated_at=today,
        period=f"{chrome['period_prefix']} {today}",
        sections=sections,
    )


def build_doctor_report_html(db: Session, user_id: int, lang: str = "ru") -> str:
    """БД → человекочитаемый HTML отчёта для врача (Проблемы→…→Образ жизни)."""
    return render_doctor_report_html(assemble_doctor_report(db, user_id, lang), lang)


# ── PDF-рендер и доставка ─────────────────────────────────────────────────────


def render_doctor_report_pdf(db: Session, user_id: int, lang: str = "ru") -> bytes:
    """HTML отчёта врачу → PDF (каркас на lang).

    weasyprint импортируется лениво: его рантайм требует системные GTK-libs
    (см. Dockerfile/Dockerfile.bot), которых нет в тест-среде без них.
    """
    html_str = build_doctor_report_html(db, user_id, lang)
    from weasyprint import HTML  # noqa: PLC0415

    return HTML(string=html_str).write_pdf()


def doctor_report_filename(today: Optional[date] = None) -> str:
    # ASCII-имя намеренно: кириллица в Content-Disposition multipart зависит от
    # RFC 2231-кодирования и часть Telegram-клиентов показывает её криво.
    d = (today or datetime.now(timezone.utc).date()).isoformat()
    return f"botkin_health_report_{d}.pdf"


def send_doctor_report_to_chat(db: Session, user_id: int, *, lang: str = "ru", timeout: int = 30) -> dict:
    """Сгенерировать PDF и отправить пользователю Telegram-документом (каркас на lang).

    Единый путь доставки для кнопки мини-аппа и агент-тула.
    Ошибки не пробрасываются — возвращаются в словаре {status, sent, error?}.
    """
    caption = CHROME.get(lang, CHROME["ru"])["caption"]
    try:
        pdf = render_doctor_report_pdf(db, user_id, lang)
    except Exception as e:
        logger.error("doctor_report render failed for %s: %s", user_id, e, exc_info=True)
        return {"status": "error", "error": f"render-failed: {e}", "sent": False}

    _ensure_bot_path()
    from bot_token import resolve_bot_token  # noqa: PLC0415

    token = resolve_bot_token()
    if not token:
        return {"status": "error", "error": "bot-token-missing", "sent": False}

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data={"chat_id": user_id, "caption": caption},
            files={"document": (doctor_report_filename(), pdf, "application/pdf")},
            timeout=timeout,
        )
        result = resp.json()
        if not result.get("ok"):
            logger.warning("sendDocument failed for %s: %s", user_id, result)
            return {"status": "error", "error": f"telegram: {result.get('description')}", "sent": False}
    except Exception as e:
        logger.error("sendDocument exception for %s: %s", user_id, e, exc_info=True)
        return {"status": "error", "error": f"telegram-exception: {e}", "sent": False}

    return {"status": "ok", "sent": True}
