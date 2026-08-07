"""Импортёр Withings: конвертация единиц, парсинг групп, апсерт, ротация refresh-токена.

Контекст: Apple Health не имеет типов для мышечной массы / воды / костной массы /
висцерального жира, поэтому эти поля идут в Botkin прямым каналом из облака Withings.
"""

import importlib.util
import json
import types
from datetime import timezone
from pathlib import Path

import pytest

# scripts/import/ — не пакет (import — ключевое слово), грузим по пути.
_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "import" / "withings_api.py"
_spec = importlib.util.spec_from_file_location("withings_api_import", _MOD_PATH)
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


# ── единицы ───────────────────────────────────────────────────────────────────


def test_measure_value_applies_exponent():
    # Withings отдаёт мантиссу + порядок: 72500 * 10^-3 = 72.5 кг
    assert wt.measure_value({"value": 72500, "unit": -3}) == 72.5
    assert wt.measure_value({"value": 2172, "unit": 0}) == 2172


# ── парсинг групп ─────────────────────────────────────────────────────────────


def _grp(ts, measures):
    return {"grpid": ts, "date": ts, "measures": measures}


def test_parse_measure_groups_maps_all_fields():
    groups = [
        _grp(
            1754373000,  # 2026-08-05 07:50 UTC
            [
                {"type": 1, "value": 109532, "unit": -3},
                {"type": 6, "value": 33214, "unit": -3},
                {"type": 76, "value": 69530, "unit": -3},
                {"type": 77, "value": 48360, "unit": -3},
                {"type": 88, "value": 3600, "unit": -3},
                {"type": 170, "value": 62, "unit": -1},
                {"type": 226, "value": 2172, "unit": 0},
            ],
        )
    ]
    (row,) = wt.parse_measure_groups(groups)
    assert row["weight"] == 109.532
    assert row["body_fat"] == 33.214
    assert row["muscle_mass"] == 69.53
    assert row["water"] == 48.36
    assert row["bone_mass"] == 3.6
    assert row["visceral_fat"] == 6.2
    assert row["bmr"] == 2172
    assert row["measured_at"].tzinfo is timezone.utc  # tz-aware → корректно в timestamptz


def test_parse_measure_groups_skips_group_without_weight():
    """Пульс весы пишут отдельной группой; weights.weight NOT NULL → такие группы мимо."""
    groups = [
        _grp(1754373000, [{"type": 11, "value": 103, "unit": 0}]),  # только пульс
        _grp(1754373100, [{"type": 1, "value": 109532, "unit": -3}]),
    ]
    rows = wt.parse_measure_groups(groups)
    assert len(rows) == 1
    assert rows[0]["weight"] == 109.532


def test_parse_measure_groups_sorted_by_time():
    groups = [
        _grp(1754460000, [{"type": 1, "value": 109000, "unit": -3}]),
        _grp(1754373000, [{"type": 1, "value": 110000, "unit": -3}]),
    ]
    rows = wt.parse_measure_groups(groups)
    assert [r["weight"] for r in rows] == [110.0, 109.0]  # старая запись первая


def test_parse_measure_groups_ignores_unknown_types():
    groups = [_grp(1754373000, [{"type": 1, "value": 109000, "unit": -3}, {"type": 999, "value": 5, "unit": 0}])]
    (row,) = wt.parse_measure_groups(groups)
    assert set(row) == {"weight", "measured_at"}


def test_parse_measure_groups_empty():
    assert wt.parse_measure_groups([]) == []


# ── апсерт ────────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self):
        self.calls = []
        self.sql = ""

    def execute(self, sql, params=None):
        self.sql = sql
        self.calls.append(params)

    def fetchone(self):
        return (True,)  # was_inserted


def test_upsert_rows_passes_fields_and_rounds_visceral():
    cur = _FakeCursor()
    rows = wt.parse_measure_groups(
        [
            _grp(
                1754373000,
                [
                    {"type": 1, "value": 109532, "unit": -3},
                    {"type": 170, "value": 62, "unit": -1},  # 6.2 → колонка Integer
                ],
            )
        ]
    )
    ins, upd = wt.upsert_rows(cur, 836757955, rows)
    assert (ins, upd) == (1, 0)
    params = cur.calls[0]
    assert params[0] == 836757955
    assert params[2] == 109.532
    assert params[7] == 6  # visceral_fat округлён под Integer-колонку


def test_upsert_uses_coalesce_to_not_clobber_apple_health():
    """Регресс: канал HAE и Withings делят (user_id, measured_at) — дополняем, не затираем."""
    cur = _FakeCursor()
    rows = wt.parse_measure_groups([_grp(1754373000, [{"type": 1, "value": 109532, "unit": -3}])])
    wt.upsert_rows(cur, 1, rows)
    assert "COALESCE(EXCLUDED.body_fat, weights.body_fat)" in cur.sql
    assert "COALESCE(EXCLUDED.muscle_mass, weights.muscle_mass)" in cur.sql


def test_upsert_rows_counts_updates():
    class _Existing(_FakeCursor):
        def fetchone(self):
            return (False,)  # запись уже была → update

    cur = _Existing()
    rows = wt.parse_measure_groups([_grp(1754373000, [{"type": 1, "value": 109000, "unit": -3}])])
    assert wt.upsert_rows(cur, 1, rows) == (0, 1)


# ── SQL-путь для запуска с Мака (ssh+psql) ────────────────────────────────────


def _rows_one(extra: dict | None = None):
    """Одна строка: вес + опционально доп. метрики {meastype: (value, unit)}."""
    measures = [{"type": 1, "value": 109532, "unit": -3}]
    measures += [{"type": t, "value": v, "unit": u} for t, (v, u) in (extra or {}).items()]
    return wt.parse_measure_groups([_grp(1754373000, measures)])


def test_build_upsert_sql_nulls_missing_fields():
    """Замер без состава тела (биоимпеданс не дочитал) → NULL, а не падение."""
    sql = wt.build_upsert_sql(836757955, _rows_one())
    assert sql.count("INSERT INTO weights") == 1
    assert "836757955" in sql and "109.532" in sql
    assert "NULL" in sql  # body_fat/muscle_mass/... отсутствуют
    assert "'withings'" in sql
    assert sql.rstrip().endswith(";")


def test_build_upsert_sql_rounds_visceral_and_keeps_coalesce():
    sql = wt.build_upsert_sql(1, _rows_one({170: (62, -1)}))  # висцеральный 6.2
    assert ", 6, 'withings')" in sql  # округлён под Integer-колонку
    assert "COALESCE(EXCLUDED.muscle_mass, weights.muscle_mass)" in sql


def test_build_upsert_sql_batches_rows():
    rows = wt.parse_measure_groups(
        [
            _grp(1754373000, [{"type": 1, "value": 109000, "unit": -3}]),
            _grp(1754460000, [{"type": 1, "value": 108500, "unit": -3}]),
        ]
    )
    sql = wt.build_upsert_sql(1, rows)
    assert sql.count("INSERT INTO weights") == 2


def test_build_upsert_sql_empty_rows():
    assert wt.build_upsert_sql(1, []) == ""


def test_push_via_ssh_counts_and_raises(monkeypatch):
    monkeypatch.setenv("BOTKIN_REMOTE_SSH", "user@example")
    monkeypatch.setenv("BOTKIN_REMOTE_PSQL", "psql -d db")
    calls = {}

    def fake_run(cmd, input=None, capture_output=None, text=None):
        calls["cmd"] = cmd
        calls["input"] = input
        return types.SimpleNamespace(returncode=0, stdout="INSERT 0 1\nINSERT 0 1\n", stderr="")

    monkeypatch.setattr(wt, "subprocess", types.SimpleNamespace(run=fake_run))
    assert wt.push_via_ssh("SQL") == (2, 0)
    assert "user@example" in calls["cmd"] and calls["input"] == "SQL"

    def fail_run(cmd, input=None, capture_output=None, text=None):
        return types.SimpleNamespace(returncode=255, stdout="", stderr="ssh: connect refused")

    monkeypatch.setattr(wt, "subprocess", types.SimpleNamespace(run=fail_run))
    with pytest.raises(wt.WithingsError):
        wt.push_via_ssh("SQL")


def test_push_via_ssh_requires_env(monkeypatch):
    """Реквизиты инфраструктуры только из .env — без них падаем с понятной ошибкой."""
    monkeypatch.delenv("BOTKIN_REMOTE_SSH", raising=False)
    monkeypatch.delenv("BOTKIN_REMOTE_PSQL", raising=False)
    with pytest.raises(wt.WithingsError, match="BOTKIN_REMOTE_SSH"):
        wt.push_via_ssh("SQL")


# ── токены ────────────────────────────────────────────────────────────────────


def test_cached_refresh_token_wins_over_env(monkeypatch, tmp_path):
    """После ротации env-значение протухает — берём диск, иначе логин ломается."""
    cache = tmp_path / "withings_tokens.json"
    cache.write_text(json.dumps({"refresh_token": "FROM_DISK"}))
    monkeypatch.setattr(wt, "TOKEN_CACHE", cache)
    monkeypatch.setenv("WITHINGS_REFRESH_TOKEN", "FROM_ENV")
    assert wt._current_refresh_token() == "FROM_DISK"


def test_env_refresh_token_used_for_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setattr(wt, "TOKEN_CACHE", tmp_path / "absent.json")
    monkeypatch.setenv("WITHINGS_REFRESH_TOKEN", "FROM_ENV")
    assert wt._current_refresh_token() == "FROM_ENV"


def test_corrupt_token_cache_falls_back_to_env(monkeypatch, tmp_path):
    cache = tmp_path / "withings_tokens.json"
    cache.write_text("{not json")
    monkeypatch.setattr(wt, "TOKEN_CACHE", cache)
    monkeypatch.setenv("WITHINGS_REFRESH_TOKEN", "FROM_ENV")
    assert wt._current_refresh_token() == "FROM_ENV"


def test_get_access_token_persists_rotated_refresh(monkeypatch, tmp_path):
    cache = tmp_path / "withings_tokens.json"
    monkeypatch.setattr(wt, "TOKEN_CACHE", cache)
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "cid")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("WITHINGS_REFRESH_TOKEN", "OLD_REFRESH")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": 0,
                "body": {"access_token": "ACC", "refresh_token": "NEW_REFRESH", "expires_in": 10800},
            }

    monkeypatch.setattr(wt, "requests", types.SimpleNamespace(post=lambda *a, **k: _Resp()))

    assert wt.get_access_token() == "ACC"
    saved = json.loads(cache.read_text())
    assert saved["refresh_token"] == "NEW_REFRESH"  # ротированный сохранён
    assert saved["access_token"] == "ACC"


def test_get_access_token_raises_on_api_error(monkeypatch, tmp_path):
    monkeypatch.setattr(wt, "TOKEN_CACHE", tmp_path / "t.json")
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "cid")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("WITHINGS_REFRESH_TOKEN", "R")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": 401}  # HTTP 200, ошибка в теле — квирк Withings

    monkeypatch.setattr(wt, "requests", types.SimpleNamespace(post=lambda *a, **k: _Resp()))
    with pytest.raises(wt.WithingsError):
        wt.get_access_token()


def test_get_access_token_requires_creds(monkeypatch, tmp_path):
    monkeypatch.setattr(wt, "TOKEN_CACHE", tmp_path / "t.json")
    monkeypatch.delenv("WITHINGS_CLIENT_ID", raising=False)
    monkeypatch.delenv("WITHINGS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("WITHINGS_REFRESH_TOKEN", raising=False)
    with pytest.raises(wt.WithingsError):
        wt.get_access_token()


def test_get_access_token_reuses_valid_cached(monkeypatch, tmp_path):
    """Действующий access_token из общего файла используется без refresh (не ротируем зря)."""
    import time

    cache = tmp_path / "withings_tokens.json"
    cache.write_text(json.dumps({"access_token": "LIVE", "refresh_token": "R", "expires_at": time.time() + 3600}))
    monkeypatch.setattr(wt, "TOKEN_CACHE", cache)

    def boom(*a, **k):
        raise AssertionError("refresh не должен вызываться при валидном токене")

    monkeypatch.setattr(wt, "requests", types.SimpleNamespace(post=boom))
    assert wt.get_access_token() == "LIVE"


def test_get_access_token_refreshes_when_expired(monkeypatch, tmp_path):
    """Протухший access_token (буфер 5 мин) → идём в refresh."""
    import time

    cache = tmp_path / "withings_tokens.json"
    cache.write_text(json.dumps({"access_token": "OLD", "refresh_token": "R", "expires_at": time.time() + 60}))
    monkeypatch.setattr(wt, "TOKEN_CACHE", cache)
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "cid")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "secret")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": 0, "body": {"access_token": "FRESH", "refresh_token": "R2", "expires_in": 10800}}

    monkeypatch.setattr(wt, "requests", types.SimpleNamespace(post=lambda *a, **k: _Resp()))
    assert wt.get_access_token() == "FRESH"


def test_save_tokens_preserves_foreign_keys(monkeypatch, tmp_path):
    """Регресс: общий файл с MCP — merge сохраняет чужие ключи (userid), не затирает."""
    cache = tmp_path / "withings_tokens.json"
    cache.write_text(json.dumps({"access_token": "OLD", "refresh_token": "R", "userid": 48719916}))
    monkeypatch.setattr(wt, "TOKEN_CACHE", cache)

    wt._save_tokens({"access_token": "NEW", "refresh_token": "R2", "expires_at": 123})
    saved = json.loads(cache.read_text())
    assert saved["access_token"] == "NEW" and saved["refresh_token"] == "R2"
    assert saved["userid"] == 48719916  # ключ MCP уцелел


# ── выборка (пагинация) ───────────────────────────────────────────────────────


def test_fetch_measure_groups_follows_pagination(monkeypatch):
    pages = [
        {"status": 0, "body": {"measuregrps": [{"date": 1, "measures": []}], "more": 1, "offset": 1}},
        {"status": 0, "body": {"measuregrps": [{"date": 2, "measures": []}], "more": 0}},
    ]
    seen = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_post(url, headers=None, data=None, timeout=None):
        seen.append(data["offset"])
        return _Resp(pages[len(seen) - 1])

    monkeypatch.setattr(wt, "requests", types.SimpleNamespace(post=fake_post))
    groups = wt.fetch_measure_groups("ACC", 0, 100)
    assert len(groups) == 2
    assert seen == [0, 1]  # второй запрос ушёл со сдвигом из ответа


def test_fetch_measure_groups_raises_on_status(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": 601}  # rate limit

    monkeypatch.setattr(wt, "requests", types.SimpleNamespace(post=lambda *a, **k: _Resp()))
    with pytest.raises(wt.WithingsError):
        wt.fetch_measure_groups("ACC", 0, 100)
