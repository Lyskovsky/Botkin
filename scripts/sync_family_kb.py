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

Недоступность сервера (fail2ban, сеть, таймаут ssh) — это НЕ то же самое, что
"файла на сервере нет". fetch_server_kb_text() различает три исхода:
  - "ok"          — файл прочитан (может быть пустым, если реально пустой)
  - "absent"      — ssh отработал, но файла нет (легитимно, например первый синк)
  - "unreachable" — ssh сам упал (rc=255 и т.п.) — сеть, таймаут, бан fail2ban
При "unreachable" заливка ЗАПРЕЩЕНА: compute_upload_plan возвращает
should_upload=False со статусом "unreachable", main() печатает громкое
предупреждение и завершает прогон ненулевым exit code — тихо продолжать нельзя,
иначе на следующем пользователе тот же bad ssh-session даст ложное
"absent" → перезапись и потеря agent_corrections.
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

ABSENT_SENTINEL = "__KB_ABSENT__"

# ControlMaster мультиплексирует несколько ssh/scp-вызовов подряд через одно
# TCP-соединение — на 9 пользователей раньше уходило ~4 сессии на человека
# (fetch в compute_upload_plan + повторный fetch в upload + backup + scp),
# что на быстрой серии подключений само провоцирует бан fail2ban на порт 22.
SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPath=~/.ssh/cm-botkin-%r@%h:%p",
    "-o",
    "ControlPersist=60s",
]


def fetch_server_kb_text(telegram_id: int) -> tuple[str, str]:
    """Возвращает (статус, текст) для файла на сервере.

    Статус — один из:
      "ok"          — файл прочитан (text — его содержимое, может быть пустым)
      "absent"      — ssh отработал, файла нет (легитимный случай)
      "unreachable" — сам ssh не смог подключиться/выполниться (сеть, таймаут,
                       бан fail2ban) — text в этом случае пуст и НЕ означает
                       "файла нет"

    Различаем через сентинел: если файла нет, `cat` печатает сентинел вместо
    пустой строки, поэтому пустой stdout однозначно значит "ssh не выполнился".
    """
    result = subprocess.run(
        [
            "ssh",
            *SSH_OPTS,
            SERVER,
            f"test -f {SERVER_KB_DIR}/kb_{telegram_id}.json && "
            f"cat {SERVER_KB_DIR}/kb_{telegram_id}.json || echo {ABSENT_SENTINEL}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Транспортная ошибка ssh (сеть, таймаут, бан fail2ban и т.п.) —
        # НЕ путать с "файла нет": та легитимная ветка ниже возвращает
        # rc=0 и сентинел, а не ненулевой код.
        return "unreachable", ""
    if result.stdout.strip() == ABSENT_SENTINEL:
        return "absent", ""
    return "ok", result.stdout


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

    Статус "unreachable" — ssh не смог подключиться (сеть/таймаут/бан
    fail2ban), а не "файла на сервере нет". should_upload в этом случае
    ВСЕГДА False: заливать локальный файл вслепую, не зная, что реально
    лежит на сервере, значит рисковать стереть agent_corrections.
    """
    local_kb = json.loads(local.read_text(encoding="utf-8"))
    server_status, server_text = fetch_server_kb_text(telegram_id)

    if server_status == "unreachable":
        return False, "unreachable", None

    if server_status == "absent":
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


def upload(local: Path, telegram_id: int, _sshpass: str = "", merged: dict | None = None) -> bool:
    """Заливает на сервер уже смерженный KB, сохраняя agent_corrections сервера.

    Если `merged` передан (main() уже посчитал его через compute_upload_plan) —
    используется как есть, без повторного ssh-fetch: раньше main() выбрасывал
    посчитанный merged и заставлял upload() перечитывать всё заново, что на
    N пользователей удваивало число ssh-сессий и провоцировало бан fail2ban.

    Если `merged` не передан — вычисляется здесь же (обратная совместимость:
    `sync_user_health.py` вызывает `upload(local, tid)` без merged). В этом
    случае, если сервер недоступен (rc!=0 у ssh — сеть/таймаут/бан fail2ban),
    заливка НЕ производится: возвращается False, а не "грузим local поверх",
    иначе неотличимая от "файла нет" ошибка стёрла бы agent_corrections.
    """
    remote = f"{SERVER}:{SERVER_KB_DIR}/kb_{telegram_id}.json"

    if merged is None:
        local_kb = json.loads(local.read_text(encoding="utf-8"))
        server_status, server_text = fetch_server_kb_text(telegram_id)
        if server_status == "unreachable":
            print(f"    ✗ сервер недоступен по ssh (telegram_id={telegram_id}) — загрузка пропущена")
            return False
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
    unreachable = []
    for tid, folder in users.items():
        local = FAMILY_HEALTH / folder / "knowledge_base.json"
        if not local.exists():
            print(f"  ⊘ {tid} {folder}: knowledge_base.json не найден локально")
            continue

        should_upload, status, merged = compute_upload_plan(local, tid)

        if status == "unreachable":
            print(f"  ✗✗ {tid} {folder}: СЕРВЕР НЕДОСТУПЕН ПО SSH — пропущено, НЕ загружаю вслепую")
            unreachable.append((tid, folder))
        elif not should_upload:
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

    if unreachable:
        names = ", ".join(f"{tid} ({folder})" for tid, folder in unreachable)
        print(
            f"\n⚠️  ВНИМАНИЕ: сервер был недоступен по ssh для {len(unreachable)} пользователь(ей): {names}.\n"
            "Причина не диагностирована автоматически (сеть, таймаут, бан fail2ban на порт 22 и т.п.) — "
            "проверь вручную. Эти пользователи НЕ были синхронизированы в этом прогоне."
        )

    if not needs_upload:
        print("\nМенять нечего." if not unreachable else "\nМенять нечего среди доступных пользователей.")
        sys.exit(1 if unreachable else 0)

    if not args.apply:
        print("\nЭто dry-run. Чтобы реально загрузить — повтори с --apply.")
        print(f"Будет загружено: {len(needs_upload)} файла(ов).")
        sys.exit(1 if unreachable else 0)

    print(f"\nЗагружаю {len(needs_upload)} файлов…")
    for tid, folder, local, merged in needs_upload:
        # merged уже посчитан в compute_upload_plan — передаём его дальше, чтобы
        # upload() не делал повторный ssh-fetch (лишняя сессия на пользователя).
        ok = upload(local, tid, merged=merged)
        status = "✅" if ok else "❌"
        print(f"  {status} kb_{tid}.json ← {folder}")

    print("\nГотово. На сервере backup'ы старых версий в *.backup_YYYYMMDD.")
    print("Перезапусти контейнер если он не подхватит сразу (bind-mount должен работать live):")
    print("  ssh root@116.203.213.137 'docker restart healthvault_bot'")

    if unreachable:
        sys.exit(1)


if __name__ == "__main__":
    main()
