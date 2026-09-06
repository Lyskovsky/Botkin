# Сервер Botkin (прод)

> Снимок на 2026-06-13. Hetzner, единый хост (пока и прод, и будущий dev на одной машине).

## Доступ

```bash
ssh botkin_server          # алиас в ~/.ssh/config (User igorn, key ~/.ssh/botkin/botkin_igorn)
# напрямую:
ssh -i ~/.ssh/botkin/botkin_igorn igorn@116.203.213.137
```

Пользователь `igorn` — в группах `sudo` и `docker` (деплой без root-пароля).

## Железо / ОС

| | |
|---|---|
| IP | `116.203.213.137` |
| ОС | Ubuntu 24.04 LTS (kernel 6.8) |
| CPU | 2 vCPU Intel Xeon (Skylake) |
| RAM | 3.7 GB (+ 4 GB swap) |
| Диск | 38 GB SSD (~14 GB свободно) |
| Docker | 29.x + compose v5 |

## Footprint Botkin

| Что | Где / как |
|---|---|
| Бот | контейнер `healthvault_bot` (FastAPI :8081, healthy) |
| БД | контейнер `healthvault_postgres` (postgres:15, volume) |
| Код | `/opt/botkin` — pull-only через GitHub Actions «Deploy prod» (образ из GHCR, без сборки на сервере). Исторически до cutover код жил в `/opt/healthvault` со сборкой на сервере |
| Бэкапы | `/opt/backups` — nightly `pg_dump` (cron), **root-only** |
| Ingress | **nginx на хосте** терминирует TLS для `botkin.health` → `127.0.0.1:8081` (legacy-домен `health.orangegate.cc` остаётся отдельным vhost — принимает только старый HAE-webhook, см. `apple_health.py`) |

Целевая pull-only модель кладёт стек в `/opt/botkin` (см. `docker-compose.prod.yml`,
`.github/workflows/deploy-prod.yml`).

> Переезд mini-app и API на бренд-домен `botkin.health` завершён (#387) — чеклист и целевой
> vhost как исторический артефакт: [migrate-webapp-to-botkin-health.md](migrate-webapp-to-botkin-health.md) (#212).

## ⚠️ Важно: хост общий (multi-tenant)

На машине крутятся и **другие, не связанные с Botkin, стеки** (включая отдельный
LLM-proxy на `127.0.0.1:4000`). Отсюда правила для деплоя Botkin:

- Чистка образов — **только** `ghcr.io/botkin-health/botkin-bot` (никаких `prune -af`).
- **nginx на хосте занимает порты 80/443** и обслуживает несколько vhost'ов. Перевод
  Botkin на Caddy — это НЕ drop-in: Caddy не сможет co-bind 443, пока там nginx.
  Варианты: (а) оставить nginx и для Botkin; (б) мигрировать все vhost'ы на Caddy
  (трогает другие проекты). Решение — открытый вопрос, см. ROADMAP/issue.
- Деплой не трогает контейнеры/volume/образы других стеков.

## Ночной синк данных прод → дев

Дев-стенд (`/opt/botkin-dev`, project `botkin-dev`, контейнеры `botkin_dev_*`)
еженощно получает данные с прода — workflow **«Sync prod → dev»**
(`.github/workflows/sync-prod-to-dev.yml`, cron `0 0 * * *` = 03:00 МСК) заходит
по SSH и запускает `scripts/ci/sync-prod-to-dev.sh`. Обоснование и детали —
[ADR-0004](../architecture/decisions/0004-nightly-prod-to-dev-data-sync.md).

| Что | Как |
|---|---|
| Направление | **строго prod → dev**, прод read-only (только `\copy … TO STDOUT`) |
| Стратегия | гибрид: natural-key таблицы — **upsert** (дев-тест цел), serial-only — **full-replace**, служебные/orphan — **skip** |
| БД-роль | `healthvault` (владелец/superuser → RLS bypass); заливка под `session_replication_role=replica` (глушит аудит-триггер + FK) |
| data-файлы | `rsync -a` без `--delete`, `/opt/botkin/data → /opt/botkin-dev/data` через privileged-контейнер (uid 10001) |
| Дев-бот | останавливается на время БД-заливки, поднимается обратно |

**Ручной запуск / проверка:**
- GitHub → Actions → «Sync prod → dev» → `Run workflow`, `dry_run=true` — сухой
  прогон (счётчики строк, без изменений). Затем `dry_run=false` — боевой.
- Локально из чекаута репо (скрипт стримится по SSH, на сервере не хранится):
  `ssh <user>@<host> bash -s -- --dry-run < scripts/ci/sync-prod-to-dev.sh`.

⚠️ `schedule`/`workflow_dispatch` активны только когда workflow на ветке `main`
(после `dev→main`). Дев одноразовый — pre-sync бэкап дева не делается.
