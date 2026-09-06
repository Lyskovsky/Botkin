#!/usr/bin/env python3
"""Синхронизация KB FamilyHealth/<имя>/knowledge_base.json → server kb_<telegram_id>.json.

Запуск ВРУЧНУЮ по запросу (не в cron) — данные меняются редко, автомат не нужен.

Использование:
    python3 scripts/sync_family_kb.py             # все известные пользователи, по умолчанию dry-run
    python3 scripts/sync_family_kb.py --apply     # реально загрузить на сервер
    python3 scripts/sync_family_kb.py --user 895655 --apply  # только один

Логика:
- Маппинг telegram_id → имя папки в FamilyHealth (см. config/users.py::KB_USERS)
- Локальный KB — source of truth для всего, КРОМЕ секции `agent_corrections`:
  её пишет BotkinClaw на сервере в рантайме (webhook/agent_tools_api.py) со слов
  пользователя, и на маке её нет и быть не должно. Перед перезаписью серверного
  файла секция `agent_corrections` вычитывается с сервера и вливается обратно —
  иначе каждый --apply тихо стирал бы поправки пользователя (см. merge_agent_corrections).
- Сравнение "нечего заливать" делается по канонической JSON-сериализации
  (смерженный локальный vs текущий серверный), а не по сырому sha256 файла —
  сырые байты никогда не совпадут, если сервер хранит agent_corrections, которых
  нет в локальном файле.
- Если разные → backup сервер-версии в *.backup_YYYYMMDD, перезаписывает kb_<id>.json
  смерженным содержимым (local + agent_corrections сервера).
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAMILY_HEALTH = Path.home() / "Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/FamilyHealth"
SERVER = "root@116.203.213.137"
SERVER_KB_DIR = "/opt/healthvault/data/kb"

# telegram_id → имя папки в FamilyHealth
sys.path.insert(0, str(PROJECT_ROOT))
from config.users import KB_USERS as USERS

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]


def fetch_server_kb_text(telegram_id: int) -> str:
    """Сырой JSON-текст файла на сервере, или "" если файла нет/ssh упал."""
    result = subprocess.run(
        ["ssh", *SSH_OPTS, SERVER, f"cat {SERVER_KB_DIR}/kb_{telegram_id}.json 2>/dev/null"],
        capture_output=True,
        text=True,
    )
    return result.stdout


def merge_agent_corrections(local_kb: dict, server_text: str) -> dict:
    """Возвращает копию local_kb с секцией `agent_corrections`, взятой с сервера.

    `agent_corrections` пишет BotkinClaw на сервере в рантайме (agent_tools_api.py) —
    структура {key: {"value": str, "reason": str, "updated_at": ISO-8601 UTC}}.
    На маке этой секции нет и быть не должно. Всё остальное содержимое остаётся
    из local_kb без изменений — local по-прежнему source of truth.
    """
    if not server_text.strip():
        return local_kb
    try:
        server_kb = json.loads(server_text)
    except json.JSONDecodeError:
        # Битый/пустой JSON на сервере — нечего сохранять, грузим local как есть.
        return local_kb
    corrections = server_kb.get("agent_corrections")
    if not corrections:
        return local_kb
    merged = dict(local_kb)
    merged["agent_corrections"] = corrections
    return merged


def canonical_bytes(data: dict) -> bytes:
    """Детерминированная сериализация для сравнения содержимого (не байтового вида файла)."""
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def compute_upload_plan(local: Path, telegram_id: int) -> tuple[bool, str, dict | None]:
    """Решает, нужно ли грузить, и что именно (уже смерженное с agent_corrections).

    Возвращает (нужно_грузить, статус_для_вывода, merged_kb_или_None).
    merged_kb — None когда грузить не нужно.
    """
    local_kb = json.loads(local.read_text(encoding="utf-8"))
    server_text = fetch_server_kb_text(telegram_id)

    if not server_text.strip():
        return True, "missing-on-server", local_kb

    merged = merge_agent_corrections(local_kb, server_text)
    try:
        server_kb = json.loads(server_text)
    except json.JSONDecodeError:
        return True, "server-invalid-json", merged

    if canonical_bytes(merged) == canonical_bytes(server_kb):
        return False, "match", None

    return True, "diff", merged


def _write_merged_to_tempfile(merged_kb: dict) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        tmp.write(json.dumps(merged_kb, ensure_ascii=False, indent=2))
        tmp.write("\n")
        return Path(tmp.name)


def upload(local: Path, telegram_id: int, _sshpass: str = "") -> bool:
    """Заливает локальный KB на сервер, сохраняя agent_corrections сервера.

    Перечитывает содержимое local с диска и то, что сейчас лежит на сервере,
    сливает (merge_agent_corrections), бэкапит текущую серверную версию и грузит
    результат вместо сырого local-файла. Совместимо с прежним вызовом
    `upload(local, telegram_id)` — используется и напрямую из sync_user_health.py.
    """
    remote = f"{SERVER}:{SERVER_KB_DIR}/kb_{telegram_id}.json"
    local_kb = json.loads(local.read_text(encoding="utf-8"))
    server_text = fetch_server_kb_text(telegram_id)
    merged = merge_agent_corrections(local_kb, server_text)

    # Backup существующей версии на сервере
    subprocess.run(
        [
            "ssh",
            *SSH_OPTS,
            SERVER,
            f"[ -f {SERVER_KB_DIR}/kb_{telegram_id}.json ] && "
            f"cp {SERVER_KB_DIR}/kb_{telegram_id}.json "
            f"{SERVER_KB_DIR}/kb_{telegram_id}.json.backup_$(date +%Y%m%d) || true",
        ],
        capture_output=True,
    )

    tmp_path = _write_merged_to_tempfile(merged)
    try:
        r = subprocess.run(
            ["scp", *SSH_OPTS, str(tmp_path), remote],
            capture_output=True,
            text=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Реально загружать (без флага — dry-run)")
    ap.add_argument("--user", type=int, help="Один пользователь по telegram_id")
    args = ap.parse_args()

    users = {args.user: USERS[args.user]} if args.user else USERS

    needs_upload = []
    for tid, folder in users.items():
        local = FAMILY_HEALTH / folder / "knowledge_base.json"
        if not local.exists():
            print(f"  ⊘ {tid} {folder}: knowledge_base.json не найден локально")
            continue

        should_upload, status, merged = compute_upload_plan(local, tid)

        if not should_upload:
            print(f"  ✓ {tid} {folder}: совпадает")
        elif status == "missing-on-server":
            print(f"  ✚ {tid} {folder}: на сервере нет, будет загружен ({local.stat().st_size // 1024} KB)")
            needs_upload.append((tid, folder, local, merged))
        elif status == "server-invalid-json":
            print(f"  ⚠ {tid} {folder}: на сервере битый JSON, будет перезаписан")
            needs_upload.append((tid, folder, local, merged))
        else:
            print(f"  ⚠ {tid} {folder}: РАЗНЫЕ")
            needs_upload.append((tid, folder, local, merged))

    if not needs_upload:
        print("\nВсё синхронизировано. Менять нечего.")
        return

    if not args.apply:
        print("\nЭто dry-run. Чтобы реально загрузить — повтори с --apply.")
        print(f"Будет загружено: {len(needs_upload)} файла(ов).")
        return

    print(f"\nЗагружаю {len(needs_upload)} файлов…")
    for tid, folder, local, _merged in needs_upload:
        # upload() сам заново читает local+сервер и мержит — используем единую точку входа,
        # чтобы не разъезжались две копии логики мержа.
        ok = upload(local, tid)
        status = "✅" if ok else "❌"
        print(f"  {status} kb_{tid}.json ← {folder}")

    print("\nГотово. На сервере backup'ы старых версий в *.backup_YYYYMMDD.")
    print("Перезапусти контейнер если он не подхватит сразу (bind-mount должен работать live):")
    print("  ssh root@116.203.213.137 'docker restart healthvault_bot'")


if __name__ == "__main__":
    main()
