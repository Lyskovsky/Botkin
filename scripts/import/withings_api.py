#!/usr/bin/env python3
"""Импорт веса и состава тела с умных весов Withings (Body Smart) → PostgreSQL.

Зачем отдельный канал, если вес уже приходит через Apple Health (HAE):
  в HealthKit ЕСТЬ типы только для веса, % жира и безжировой массы. Типов для
  мышечной массы, воды, костной массы, висцерального жира и основного обмена в
  HealthKit НЕТ — Health Mate их физически не может отдать, и до Botkin они не
  доходили (проверено на устройстве 05.08.2026). Этот импортёр берёт полный
  состав тела напрямую из облака Withings и досыпает недостающие поля в
  таблицу `weights` (колонки muscle_mass / water / bone_mass / visceral_fat
  в схеме уже есть — их просто некому было заполнять).

Апсерт по (user_id, measured_at) с COALESCE: канал HAE и этот импортёр
дополняют друг друга, а не перетирают. Если Apple-запись уже лежит с тем же
таймстампом — добавятся только пустые поля, вес/жир останутся как были.

Креды в .env: WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET / WITHINGS_REFRESH_TOKEN.
⚠️ Withings РОТИРУЕТ refresh_token при каждом обновлении — новый сохраняем в
data/cache/withings_tokens.json (env нужен только для первичного bootstrap).
Токен из кэша имеет приоритет над env: иначе после ротации env-значение
протухает и логин ломается.

Общий токен с Withings-MCP (рекомендуемый способ на Маке, без второго приложения):
задать WITHINGS_TOKENS_PATH на токен-файл MCP + client_id/secret того же приложения
(напр. из Keychain). Тогда MCP и импортёр делят ОДНО хранилище — ротация общая,
конфликта нет. _save_tokens мержит, чтобы не затереть ключи MCP (userid и пр.).

Квирк API: HTTP-код всегда 200, реальный статус — в теле (`status`, 0 = ok).

Использование:
    # основной путь — через Botkin API (нужен BOTKIN_PAT со scope rw):
    python scripts/import/withings_api.py --user 836757955 --days 90 --push-api --min-weight 90
    # изнутри контейнера / с DATABASE_URL:
    python scripts/import/withings_api.py --user 836757955 --days 90
    python scripts/import/withings_api.py --user 836757955 --dry-run
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / ".env")

TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"

# Кэш токенов (refresh ротируется — держим на диске, чтобы переживать рестарт).
# WITHINGS_TOKENS_PATH позволяет указать на ЧУЖОЙ токен-файл и делить его: так
# импортёр на Маке переиспользует токен локального Withings-MCP — единое хранилище,
# ротация общая → нет взаимной инвалидации, второе приложение не нужно.
# При общем файле _save_tokens мержит (не затирает чужие ключи вроде userid).
TOKEN_CACHE = Path(os.getenv("WITHINGS_TOKENS_PATH") or ROOT / "data" / "cache" / "withings_tokens.json")

# meastype → поле. Коды из официального API (сверено с рабочим клиентом).
# Берём только то, что нужно таблице weights; давление/SpO2 идут своим каналом.
MEASURE_TYPES = {
    1: "weight",  # кг
    5: "lean_mass_kg",  # безжировая масса, кг
    6: "body_fat",  # % жира
    8: "fat_mass_kg",  # жировая масса, кг
    11: "heart_rate",  # пульс при взвешивании, уд/мин
    76: "muscle_mass",  # кг
    77: "hydration_kg",  # КИЛОГРАММЫ; в weights.water ожидаются ПРОЦЕНТЫ → пересчёт в parse
    88: "bone_mass",  # кг
    170: "visceral_fat",  # индекс
    226: "bmr_kcal",  # основной обмен по составу тела, ккал
}
_MEASTYPES_PARAM = ",".join(str(k) for k in MEASURE_TYPES)


class WithingsError(RuntimeError):
    """Ошибка Withings API (status != 0 в теле ответа)."""


# ── Токены ────────────────────────────────────────────────────────────────────


def _load_cached_tokens() -> dict:
    """Токены с диска. {} если файла нет/битый — вызывающий уйдёт в env."""
    if not TOKEN_CACHE.exists():
        return {}
    try:
        return json.loads(TOKEN_CACHE.read_text())
    except (ValueError, OSError) as e:
        logger.debug("не смог прочитать withings-токены: %s", e)
        return {}


def _save_tokens(tokens: dict) -> None:
    """Записать токены, СОХРАНИВ прочие ключи существующего файла.

    Merge критичен при общем хранилище с Withings-MCP: у него в файле свои ключи
    (`userid` и др.). Перезаписать файл только token-полями = сломать MCP, поэтому
    читаем существующее и обновляем поверх.
    """
    try:
        merged = _load_cached_tokens()
        merged.update(tokens)
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps(merged))
        TOKEN_CACHE.chmod(0o600)  # oauth-токены мед-аккаунта — только владельцу
    except OSError as e:
        logger.warning("не смог сохранить withings-токены: %s", e)


def _current_refresh_token() -> str:
    """Кэш приоритетнее env: после ротации env-значение уже невалидно."""
    return _load_cached_tokens().get("refresh_token") or os.getenv("WITHINGS_REFRESH_TOKEN", "")


def get_access_token() -> str:
    """Валидный access_token из общего кэша; протух/нет — refresh (ротированный сохраняем).

    Сначала пробуем действующий access_token из файла (буфер 5 мин, как в MCP) — так
    при общем хранилище лишний раз не ротируем токен и не дёргаем сеть.
    """
    cached = _load_cached_tokens()
    access_cached = cached.get("access_token")
    exp = cached.get("expires_at") or 0
    if access_cached and (exp - time.time()) > 300:
        return access_cached

    client_id = os.getenv("WITHINGS_CLIENT_ID", "")
    client_secret = os.getenv("WITHINGS_CLIENT_SECRET", "")
    refresh = _current_refresh_token()
    if not (client_id and client_secret and refresh):
        raise WithingsError("нет WITHINGS_CLIENT_ID / WITHINGS_CLIENT_SECRET / WITHINGS_REFRESH_TOKEN в .env")

    resp = requests.post(
        TOKEN_URL,
        data={
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 0:
        raise WithingsError(f"обновление токена не удалось (status={payload.get('status')})")

    body = payload.get("body", {})
    access = body.get("access_token")
    if not access:
        raise WithingsError("в ответе нет access_token")
    _save_tokens(
        {
            "access_token": access,
            # refresh может не прийти — тогда остаётся прежний
            "refresh_token": body.get("refresh_token", refresh),
            "expires_at": int(time.time()) + int(body.get("expires_in", 0)),
        }
    )
    return access


# ── Выборка и парсинг ─────────────────────────────────────────────────────────


def fetch_measure_groups(access_token: str, start_ts: int, end_ts: int) -> list[dict]:
    """Все measuregrps за период (с пагинацией по more/offset). Сетевая часть."""
    groups: list[dict] = []
    offset = 0
    while True:
        resp = requests.post(
            MEASURE_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            data={
                "action": "getmeas",
                "meastypes": _MEASTYPES_PARAM,
                "category": 1,  # реальные замеры, не пользовательские цели
                "startdate": start_ts,
                "enddate": end_ts,
                "offset": offset,
            },
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != 0:
            raise WithingsError(f"getmeas вернул status={payload.get('status')}")
        body = payload.get("body", {})
        groups.extend(body.get("measuregrps", []))
        if body.get("more", 0) in (1, True):
            offset = body.get("offset", 0)
        else:
            return groups


def measure_value(measure: dict) -> float:
    """value * 10^unit — Withings отдаёт мантиссу и порядок (72500, -3 → 72.5)."""
    return measure["value"] * (10 ** measure["unit"])


# Пульс весы пишут ОТДЕЛЬНОЙ группой, без веса, и таймстамп у неё сдвинут на
# секунды-минуты относительно группы состава тела. Столько секунд считаем, что это
# одно и то же взвешивание.
_PULSE_MATCH_WINDOW_SEC = 180


def parse_measure_groups(groups: list[dict]) -> list[dict]:
    """measuregrps → строки для weights. Чистая функция (без сети/БД).

    Группы без веса не идут в результат: `weights.weight` NOT NULL. Но пульс из
    таких групп НЕ теряем — весы пишут его отдельной группой, и раньше он просто
    отбрасывался вместе с ней. Теперь приписываем его ближайшему по времени
    замеру состава тела (окно _PULSE_MATCH_WINDOW_SEC).

    Это не косметика: пульс на весах измеряется стоя и натощак, то есть близок к
    пульсу покоя. За 16–28.08.2026 весы зафиксировали 22 значения, 13 из них выше
    100 уд/мин с максимумом 127 — и ни одно не попадало в базу.

    Вода: Withings отдаёт КИЛОГРАММЫ (hydration), а `weights.water` и поле API —
    ПРОЦЕНТЫ (так пишет канал HAE, и валидатор эндпоинта ограничивает 0..100).
    Пересчитываем здесь, в одном месте на оба пути записи.
    """
    rows: list[dict] = []
    pulse_only: list[tuple[datetime, float]] = []
    for grp in groups:
        row: dict = {}
        for measure in grp.get("measures", []):
            field = MEASURE_TYPES.get(measure.get("type"))
            if field:
                row[field] = round(measure_value(measure), 3)
        measured_at = datetime.fromtimestamp(grp.get("date", 0), tz=timezone.utc)
        if "weight" not in row:
            if row.get("heart_rate") is not None:
                pulse_only.append((measured_at, row["heart_rate"]))
            continue
        hydration = row.pop("hydration_kg", None)
        if hydration is not None and row["weight"]:
            row["water"] = round(hydration / row["weight"] * 100, 1)
        row["measured_at"] = measured_at
        rows.append(row)

    # Склейка: каждый одиночный пульс — к ближайшему взвешиванию в пределах окна.
    # Если у замера пульс уже есть (весы отдали его в той же группе), не трогаем.
    for ts, hr in pulse_only:
        if not rows:
            break
        nearest = min(rows, key=lambda r: abs((r["measured_at"] - ts).total_seconds()))
        if abs((nearest["measured_at"] - ts).total_seconds()) <= _PULSE_MATCH_WINDOW_SEC:
            nearest.setdefault("heart_rate", hr)

    return sorted(rows, key=lambda r: r["measured_at"])


def filter_own_measurements(
    rows: list[dict], min_weight: float | None = None, max_weight: float | None = None
) -> tuple[list[dict], list[dict]]:
    """Отсечь замеры других людей. Возвращает (свои, чужие).

    Домашние весы общие: на них встают члены семьи, а Withings относит замер к
    владельцу аккаунта, если не распознал профиль. Без фильтра чужой вес попадает
    в историю владельца и ломает тренды/аналитику. Границы задаёт вызывающий —
    захардкоженный «нормальный вес» в открытом репозитории смысла не имеет.
    """
    if min_weight is None and max_weight is None:
        return rows, []
    own, foreign = [], []
    for r in rows:
        w = r.get("weight")
        if (min_weight is not None and w < min_weight) or (max_weight is not None and w > max_weight):
            foreign.append(r)
        else:
            own.append(r)
    return own, foreign


# ── Запись в БД ───────────────────────────────────────────────────────────────


def upsert_rows(cur, user_id: int, rows: list[dict]) -> tuple[int, int]:
    """Апсерт в weights по (user_id, measured_at). Возвращает (inserted, updated).

    COALESCE(EXCLUDED.x, weights.x): дополняем существующую запись (напр. из
    apple_health_v2) недостающими полями, но НЕ затираем уже записанные значения
    пустотой. source перезаписываем — эта запись теперь обогащена из Withings.
    """
    inserted = updated = 0
    for r in rows:
        cur.execute(
            """
            INSERT INTO weights
                (user_id, measured_at, weight, body_fat, muscle_mass, water,
                 bone_mass, visceral_fat, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'withings')
            ON CONFLICT (user_id, measured_at) DO UPDATE SET
                weight       = COALESCE(EXCLUDED.weight, weights.weight),
                body_fat     = COALESCE(EXCLUDED.body_fat, weights.body_fat),
                muscle_mass  = COALESCE(EXCLUDED.muscle_mass, weights.muscle_mass),
                water        = COALESCE(EXCLUDED.water, weights.water),
                bone_mass    = COALESCE(EXCLUDED.bone_mass, weights.bone_mass),
                visceral_fat = COALESCE(EXCLUDED.visceral_fat, weights.visceral_fat),
                source       = EXCLUDED.source
            RETURNING (xmax = 0) AS was_inserted
            """,
            (
                user_id,
                r["measured_at"],
                r.get("weight"),
                r.get("body_fat"),
                r.get("muscle_mass"),
                r.get("water"),
                r.get("bone_mass"),
                # колонка visceral_fat — Integer, индекс Withings дробный (6.2 → 6)
                round(r["visceral_fat"]) if r.get("visceral_fat") is not None else None,
                r.get("source", "withings"),
            ),
        )
        if cur.fetchone()[0]:
            inserted += 1
        else:
            updated += 1
    return inserted, updated


# ── Запись через Botkin API (основной путь) ───────────────────────────────────
# Прод-БД наружу не смотрит, а раздавать доступ к серверу под импорт весов нельзя:
# группа docker = root на хосте, а psql-суперюзер умеет COPY ... TO PROGRAM. Поэтому
# пишем по HTTPS в POST /api/agent/log_body_composition: user_id берётся из токена,
# RLS изолирует данные, доступ к серверу не нужен вообще.
API_BASE = os.getenv("BOTKIN_API_BASE", "https://botkin.health")
API_BATCH_LIMIT = 500  # ограничение эндпоинта на длину measurements[]


def to_api_measurement(row: dict) -> dict:
    """Строка парсера → объект measurements[] эндпоинта. Чистая функция.

    measured_at обязан нести офсет — это ключ идемпотентности эндпоинта: naive-время
    Postgres трактует по session TimeZone, и один момент, присланный то с офсетом то
    без, дал бы два ряда. parse_measure_groups отдаёт tz-aware UTC, поэтому isoformat
    даёт «+00:00». visceral_fat не округляем — округление под Integer-колонку делает
    сервер, здесь дробный индекс информативнее.
    """
    m = {"measured_at": row["measured_at"].isoformat(), "weight": row["weight"]}
    for field in ("body_fat", "muscle_mass", "water", "bone_mass", "visceral_fat", "fat_mass_kg", "lean_mass_kg"):
        if row.get(field) is not None:
            m[field] = row[field]
    # Пульс и основной обмен — целые: колонки SmallInteger, а весы отдают float.
    for field in ("heart_rate", "bmr_kcal"):
        if row.get(field) is not None:
            m[field] = round(row[field])
    return m


def get_agent_jwt(pat: str) -> str:
    """PAT → короткоживущий агентский JWT (единственный публичный эндпоинт API)."""
    resp = requests.post(f"{API_BASE}/api/agent/exchange_pat_for_jwt", json={"pat": pat}, timeout=30)
    if resp.status_code != 200:
        raise WithingsError(f"обмен PAT не удался: {resp.status_code} {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        raise WithingsError("в ответе обмена нет access_token")
    return token


def push_via_api(rows: list[dict], source: str = "withings") -> tuple[int, int]:
    """Отправить замеры в Botkin батчами. Возвращает (inserted, updated).

    PAT берём из env BOTKIN_PAT — в код и репозиторий он не попадает.
    """
    pat = os.getenv("BOTKIN_PAT", "")
    if not pat:
        raise WithingsError("нет BOTKIN_PAT в .env (personal access token Botkin со scope rw)")
    jwt = get_agent_jwt(pat)

    inserted = updated = 0
    for i in range(0, len(rows), API_BATCH_LIMIT):
        chunk = [to_api_measurement(r) for r in rows[i : i + API_BATCH_LIMIT]]
        resp = requests.post(
            f"{API_BASE}/api/agent/log_body_composition",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"source": source, "measurements": chunk},
            timeout=120,
        )
        if resp.status_code != 200:
            raise WithingsError(f"log_body_composition: {resp.status_code} {resp.text[:300]}")
        body = resp.json()
        inserted += body.get("inserted", 0)
        updated += body.get("updated", 0)
    return inserted, updated


def sync_user(user_id: int, days: int = 90, db_url: str | None = None) -> dict:
    """Полный цикл: токен → выборка → апсерт. Возвращает сводку для лога."""
    end_ts = int(time.time())
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp())

    rows = parse_measure_groups(fetch_measure_groups(get_access_token(), start_ts, end_ts))
    if not rows:
        return {"user_id": user_id, "rows": 0, "inserted": 0, "updated": 0}

    db_url = db_url or os.getenv("DATABASE_URL")
    if not db_url:
        raise WithingsError("DATABASE_URL не задан")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            ins, upd = upsert_rows(cur, user_id, rows)
        conn.commit()
    finally:
        conn.close()
    return {"user_id": user_id, "rows": len(rows), "inserted": ins, "updated": upd}


def _env_float(name: str) -> float | None:
    """Число из env или None. Мусорное значение не роняет импорт — просто игнорим."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r не число — игнорирую", name, raw)
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Импорт состава тела Withings → PostgreSQL")
    parser.add_argument("--user", type=int, required=True, help="telegram_id пользователя")
    parser.add_argument("--days", type=int, default=90, help="глубина истории (дней), по умолчанию 90")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL URL")
    parser.add_argument("--dry-run", action="store_true", help="только показать, без записи в БД")
    parser.add_argument(
        "--push-api",
        action="store_true",
        help="писать через Botkin API (HTTPS + BOTKIN_PAT) — основной путь, доступ к серверу не нужен",
    )
    # Дефолты из .env — чтобы ночной синк не тащил границы через аргументы шелла
    parser.add_argument(
        "--min-weight",
        type=float,
        default=_env_float("WITHINGS_MIN_WEIGHT"),
        help="отсечь замеры легче N кг (весы дома общие — на них встают другие члены семьи)",
    )
    parser.add_argument(
        "--max-weight", type=float, default=_env_float("WITHINGS_MAX_WEIGHT"), help="отсечь замеры тяжелее N кг"
    )
    args = parser.parse_args(argv)

    print("⚖️  Withings — импорт веса и состава тела...")
    end_ts = int(time.time())
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=args.days)).timestamp())
    rows = parse_measure_groups(fetch_measure_groups(get_access_token(), start_ts, end_ts))
    print(f"   Замеров получено: {len(rows)} за {args.days} дн.")

    rows, foreign = filter_own_measurements(rows, args.min_weight, args.max_weight)
    for r in foreign:
        print(f"   ⏭️  вне коридора веса (не владелец?): {r['measured_at']:%d.%m %H:%M} — {r['weight']} кг")
    if foreign:
        print(f"   Отфильтровано чужих замеров: {len(foreign)}")

    if args.dry_run:
        for r in rows[-5:]:
            print(
                f"   [DRY] {r['measured_at']:%Y-%m-%d %H:%M} "
                f"вес {r.get('weight')} кг · жир {r.get('body_fat')}% · "
                f"мышцы {r.get('muscle_mass')} кг · вода {r.get('water')} кг · "
                f"кости {r.get('bone_mass')} кг · висц. {r.get('visceral_fat')} · "
                f"BMR {r.get('bmr')} ккал"
            )
        print("   (BMR не пишется — колонки в weights нет)")
        return 0

    if args.push_api:
        ins, upd = push_via_api(rows)
        print(f"✅ Готово (Botkin API): {ins} новых, {upd} обновлено")
        return 0

    if not args.db_url:
        print("❌ DATABASE_URL не задан", file=sys.stderr)
        return 1

    conn = psycopg2.connect(args.db_url)
    try:
        with conn.cursor() as cur:
            ins, upd = upsert_rows(cur, args.user, rows)
        conn.commit()
    finally:
        conn.close()
    print(f"✅ Готово: {ins} новых, {upd} обновлено (user {args.user})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
