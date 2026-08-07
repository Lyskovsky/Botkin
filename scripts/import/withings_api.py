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
    # с Мака в прод-БД через ssh+psql (основной путь, как у zepp_csv.py):
    python scripts/import/withings_api.py --user 836757955 --days 90 --push-remote
    # изнутри контейнера / с DATABASE_URL:
    python scripts/import/withings_api.py --user 836757955 --days 90
    python scripts/import/withings_api.py --user 836757955 --dry-run
"""

import argparse
import json
import logging
import os
import subprocess
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
    5: "lean_mass",  # безжировая масса, кг (в weights не пишем, для диагностики)
    6: "body_fat",  # % жира
    76: "muscle_mass",  # кг
    77: "water",  # кг
    88: "bone_mass",  # кг
    170: "visceral_fat",  # индекс
    226: "bmr",  # основной обмен, ккал (колонки нет — только для --dry-run)
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


def parse_measure_groups(groups: list[dict]) -> list[dict]:
    """measuregrps → строки для weights. Чистая функция (без сети/БД).

    Группы без веса отбрасываем: weights.weight NOT NULL, а отдельные группы
    бывают только с пульсом (весы пишут его отдельной группой).
    """
    rows: list[dict] = []
    for grp in groups:
        row: dict = {}
        for measure in grp.get("measures", []):
            field = MEASURE_TYPES.get(measure.get("type"))
            if field:
                row[field] = round(measure_value(measure), 3)
        if "weight" not in row:
            continue
        row["measured_at"] = datetime.fromtimestamp(grp.get("date", 0), tz=timezone.utc)
        rows.append(row)
    return sorted(rows, key=lambda r: r["measured_at"])


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


# Прод-БД не смотрит наружу, поэтому вес с весов исторически льётся с машины
# владельца через ssh + psql (так же работает scripts/import/zepp_csv.py). Держим
# тот же путь: импортёр не требует ни деплоя, ни кред Withings на сервере.
# Адрес хоста и команду psql НЕ хардкодим — репозиторий публичный, реквизиты
# инфраструктуры живут в .env (он в .gitignore):
#   BOTKIN_REMOTE_SSH=user@host
#   BOTKIN_REMOTE_PSQL=docker exec -i <контейнер> psql -U <юзер> -d <база>
_ENV_SSH = "BOTKIN_REMOTE_SSH"
_ENV_PSQL = "BOTKIN_REMOTE_PSQL"


def _sql_literal(value) -> str:
    """Число/None → SQL-литерал. Строк из внешних источников здесь нет (только числа)."""
    return "NULL" if value is None else f"{value}"


def build_upsert_sql(user_id: int, rows: list[dict]) -> str:
    """Батч UPSERT-ов для psql. Чистая функция — та же семантика, что у upsert_rows.

    Отдельный путь нужен для запуска с Мака (ssh+psql), где psycopg2 к прод-БД не
    достаёт. Значения — только числа и таймстамп из API, подстановка безопасна.
    """
    statements = []
    for r in rows:
        visceral = round(r["visceral_fat"]) if r.get("visceral_fat") is not None else None
        statements.append(
            "INSERT INTO weights "
            "(user_id, measured_at, weight, body_fat, muscle_mass, water, bone_mass, visceral_fat, source) "
            f"VALUES ({user_id}, '{r['measured_at'].isoformat()}', "
            f"{_sql_literal(r.get('weight'))}, {_sql_literal(r.get('body_fat'))}, "
            f"{_sql_literal(r.get('muscle_mass'))}, {_sql_literal(r.get('water'))}, "
            f"{_sql_literal(r.get('bone_mass'))}, {_sql_literal(visceral)}, 'withings') "
            "ON CONFLICT (user_id, measured_at) DO UPDATE SET "
            "weight = COALESCE(EXCLUDED.weight, weights.weight), "
            "body_fat = COALESCE(EXCLUDED.body_fat, weights.body_fat), "
            "muscle_mass = COALESCE(EXCLUDED.muscle_mass, weights.muscle_mass), "
            "water = COALESCE(EXCLUDED.water, weights.water), "
            "bone_mass = COALESCE(EXCLUDED.bone_mass, weights.bone_mass), "
            "visceral_fat = COALESCE(EXCLUDED.visceral_fat, weights.visceral_fat), "
            "source = EXCLUDED.source;"
        )
    return "\n".join(statements)


def push_via_ssh(sql: str) -> tuple[int, int]:
    """Прогнать батч через ssh+psql. Возвращает (inserted, updated) по выводу psql.

    Хост и команда psql берутся из .env (в коде не хардкодим — репо публичный).
    """
    host = os.getenv(_ENV_SSH, "")
    psql_cmd = os.getenv(_ENV_PSQL, "")
    if not (host and psql_cmd):
        raise WithingsError(f"для --push-remote нужны {_ENV_SSH} и {_ENV_PSQL} в .env")
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", host, psql_cmd],
        input=sql,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WithingsError(f"psql через ssh упал: {result.stderr[:300]}")
    lines = [ln.strip() for ln in result.stdout.splitlines()]
    inserted = sum(1 for ln in lines if ln == "INSERT 0 1")
    # ON CONFLICT DO UPDATE тоже отдаёт «INSERT 0 1», поэтому обновления считаем как остаток
    return inserted, max(0, len([ln for ln in lines if ln.startswith("INSERT")]) - inserted)


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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Импорт состава тела Withings → PostgreSQL")
    parser.add_argument("--user", type=int, required=True, help="telegram_id пользователя")
    parser.add_argument("--days", type=int, default=90, help="глубина истории (дней), по умолчанию 90")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL URL")
    parser.add_argument("--dry-run", action="store_true", help="только показать, без записи в БД")
    parser.add_argument(
        "--push-remote",
        action="store_true",
        help="писать в прод-БД через ssh+psql (запуск с Мака, как zepp_csv.py) вместо DATABASE_URL",
    )
    args = parser.parse_args(argv)

    print("⚖️  Withings — импорт веса и состава тела...")
    end_ts = int(time.time())
    start_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=args.days)).timestamp())
    rows = parse_measure_groups(fetch_measure_groups(get_access_token(), start_ts, end_ts))
    print(f"   Замеров получено: {len(rows)} за {args.days} дн.")

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

    if args.push_remote:
        ins, upd = push_via_ssh(build_upsert_sql(args.user, rows))
        print(f"✅ Готово (ssh+psql): {ins} новых, {upd} обновлено (user {args.user})")
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
