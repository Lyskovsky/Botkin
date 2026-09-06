# tests/test_sync_family_kb.py
"""Проверяет, что sync_family_kb.py не стирает agent_corrections на сервере.

Баг: sync_family_kb.py заливал local knowledge_base.json поверх серверного
kb_<id>.json без merge — любой --apply стирал секцию agent_corrections,
которую агент BotkinClaw пишет на сервере со слов пользователя (см. CLAUDE.md
проекта). Тесты работают на временных файлах и моках subprocess — сервер
и живые данные не трогаются.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_family_kb", ROOT / "scripts" / "sync_family_kb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sfk = _load_module()


def _fake_ssh_result(stdout: str = "", returncode: int = 0):
    class _Result:
        pass

    r = _Result()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


def test_merge_agent_corrections_preserves_server_section():
    local_kb = {"blood_tests": {"values": {"ldl": 3.1}}}
    server_text = json.dumps(
        {
            "blood_tests": {"values": {"ldl": 3.1}},
            "agent_corrections": {
                "weight_goal": {"value": "80", "reason": "сказал в чате", "updated_at": "2026-09-01T10:00:00Z"}
            },
        }
    )

    merged = sfk.merge_agent_corrections(local_kb, server_text)

    assert merged["blood_tests"] == local_kb["blood_tests"]
    assert merged["agent_corrections"]["weight_goal"]["value"] == "80"
    # local_kb сам не должен мутироваться
    assert "agent_corrections" not in local_kb


def test_merge_agent_corrections_no_server_data_returns_local_unchanged():
    local_kb = {"blood_tests": {"values": {"ldl": 3.1}}}

    assert sfk.merge_agent_corrections(local_kb, "") is local_kb
    assert sfk.merge_agent_corrections(local_kb, "not json") is local_kb
    assert sfk.merge_agent_corrections(local_kb, json.dumps({"blood_tests": {}})) is local_kb


def test_compute_upload_plan_no_upload_when_only_corrections_present_on_server(tmp_path, monkeypatch):
    local_kb = {"blood_tests": {"values": {"ldl": 3.1}}}
    local_path = tmp_path / "knowledge_base.json"
    local_path.write_text(json.dumps(local_kb), encoding="utf-8")

    server_kb = {
        "blood_tests": {"values": {"ldl": 3.1}},
        "agent_corrections": {"note": {"value": "x", "reason": "y", "updated_at": "2026-09-01T00:00:00Z"}},
    }
    monkeypatch.setattr(sfk, "fetch_server_kb_text", lambda tid: json.dumps(server_kb))

    should_upload, status, merged = sfk.compute_upload_plan(local_path, 123)

    assert should_upload is False
    assert status == "match"
    assert merged is None


def test_compute_upload_plan_detects_real_content_diff(tmp_path, monkeypatch):
    local_kb = {"blood_tests": {"values": {"ldl": 4.0}}}  # изменился локально
    local_path = tmp_path / "knowledge_base.json"
    local_path.write_text(json.dumps(local_kb), encoding="utf-8")

    server_kb = {
        "blood_tests": {"values": {"ldl": 3.1}},
        "agent_corrections": {"note": {"value": "x", "reason": "y", "updated_at": "2026-09-01T00:00:00Z"}},
    }
    monkeypatch.setattr(sfk, "fetch_server_kb_text", lambda tid: json.dumps(server_kb))

    should_upload, status, merged = sfk.compute_upload_plan(local_path, 123)

    assert should_upload is True
    assert status == "diff"
    assert merged["blood_tests"]["values"]["ldl"] == 4.0
    # секция agent_corrections сервера должна была сохраниться в плане на загрузку
    assert merged["agent_corrections"]["note"]["value"] == "x"


def test_upload_uploads_merged_content_not_raw_local_file(tmp_path, monkeypatch):
    local_kb = {"blood_tests": {"values": {"ldl": 4.0}}}
    local_path = tmp_path / "knowledge_base.json"
    local_path.write_text(json.dumps(local_kb), encoding="utf-8")

    server_kb = {
        "blood_tests": {"values": {"ldl": 3.1}},
        "agent_corrections": {"note": {"value": "x", "reason": "y", "updated_at": "2026-09-01T00:00:00Z"}},
    }

    uploaded_files = []

    def fake_run(cmd, capture_output=True, text=True):
        if cmd[0] == "ssh":
            # both "cat kb_..." (via fetch_server_kb_text) and the backup command land here
            joined = " ".join(cmd)
            if "cat " in joined:
                return _fake_ssh_result(stdout=json.dumps(server_kb))
            return _fake_ssh_result(stdout="")
        if cmd[0] == "scp":
            src = Path(cmd[-2])
            uploaded_files.append(json.loads(src.read_text(encoding="utf-8")))
            return _fake_ssh_result(returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(sfk.subprocess, "run", fake_run)

    ok = sfk.upload(local_path, 123)

    assert ok is True
    assert len(uploaded_files) == 1
    uploaded = uploaded_files[0]
    # новое значение из local сохранилось
    assert uploaded["blood_tests"]["values"]["ldl"] == 4.0
    # agent_corrections с сервера не потерялась
    assert uploaded["agent_corrections"]["note"]["value"] == "x"
    # local-файл на диске не был затронут
    assert json.loads(local_path.read_text(encoding="utf-8")) == local_kb
