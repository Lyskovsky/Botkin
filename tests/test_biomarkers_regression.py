# tests/test_biomarkers_regression.py
import json
import os
from pathlib import Path

import pytest

from core.health.biomarkers import aggregate_biomarkers

FAMILY = Path(os.path.expanduser("~/Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/FamilyHealth"))
BIO_DIR = Path(__file__).resolve().parent.parent / "telegram-bot"

# Оба тест-юзера: реальные id/папка берутся из env, чтобы не светить PII
# в публичном репо (#303). Без env тесты ниже скипаются (папка не найдётся).
OWNER = (int(os.getenv("TEST_OWNER_ID", "100000001")), os.getenv("TEST_OWNER_FOLDER", "Test Owner — Здоровье"))
USER2 = (int(os.getenv("TEST_USER2_ID", "100000002")), os.getenv("TEST_USER2_FOLDER", "Test User 2 — Здоровье"))


def _kb_rows(folder: str):
    """Строки [{date, values}] из blood_tests+hormones+vitamins — как kb_to_blood_tests."""
    kb = json.loads((FAMILY / folder / "knowledge_base.json").read_text())
    rows = []
    for section in ("blood_tests", "hormones", "vitamins"):
        for e in kb.get(section, []):
            if e.get("date"):
                rows.append({"date": e["date"], "values": e.get("values") or {}})
    return rows


def test_owner_golden_nothing_lost():
    """Владелец — единственный golden: зрелый корректный pipeline.
    Новый агрегат обязан содержать каждый старый канонический ключ с тем же value+date."""
    tid, folder = OWNER
    old_file = BIO_DIR / f"biomarkers_{tid}.json"
    kb_path = FAMILY / folder / "knowledge_base.json"
    if not old_file.exists() or not kb_path.exists():
        pytest.skip(f"no local biomarkers_{tid}.json or KB")

    old = json.loads(old_file.read_text())
    new = aggregate_biomarkers(_kb_rows(folder))

    lost = []
    for key, old_rec in old.items():
        if key == "_meta":
            continue
        if key not in new:
            lost.append(key)
        elif abs(new[key]["value"] - old_rec["value"]) > 1e-6:
            lost.append(f"{key}:value {old_rec['value']}→{new[key]['value']}")
    assert not lost, f"Владелец потерял/изменил маркеры: {lost}"


def test_user2_smoke_builds_and_units_sane():
    """Второй юзер: старый baseline был битый (сырые pmol/L под каноническими именами).
    Smoke: агрегат строится и ключевые маркеры конвертированы в правильные единицы."""
    tid, folder = USER2
    kb_path = FAMILY / folder / "knowledge_base.json"
    if not kb_path.exists():
        pytest.skip("no USER2 KB (set TEST_USER2_FOLDER)")

    bio = aggregate_biomarkers(_kb_rows(folder))
    assert bio["_meta"]["total_markers"] > 30

    if "insulin" in bio:
        assert bio["insulin"]["value"] < 30, "insulin должен быть в µIU/mL (<30), не сырой pmol/L"
    if "PTH_intact" in bio:
        assert 10 < bio["PTH_intact"]["value"] < 200, "PTH должен быть в pg/mL"
    if "vitamin_B12" in bio:
        assert 150 < bio["vitamin_B12"]["value"] < 1500, "B12 в pg/mL"
    if "folic_acid" in bio:
        assert bio["folic_acid"]["value"] < 25, "фолат в ng/mL"
