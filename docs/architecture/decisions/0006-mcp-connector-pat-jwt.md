# ADR-0006: MCP-коннектор для Claude Desktop через PAT→JWT

**Дата:** 2026-06-28
**Статус:** Accepted
**Автор:** Александр Лысковский
**Связи:** [#228](https://github.com/botkin-health/Botkin/issues/228), ресёрч `docs/researches/2026-06-28-mcp-connector.md`, [ADR-0002](0002-rejecting-nanoclaw-for-simpler-agent.md), [ADR-0003](0003-alembic-for-db-migrations.md)

## Контекст

В vision (CLAUDE.md) заложен «личный AI-агент на компе пользователя»: его Claude Desktop
видит и серверные данные Botkin (через MCP), и локальные приватные файлы, которых на сервере
нет (КПТ-дневники, не загруженные анализы, проприетарные выгрузки — напр. `.pdd` кардиомонитора
Medtronic Reveal LINQ Андрея, под который коннектор писать нерентабельно). ADR-0002 явно
зарезервировал переиспользование `agent_tools_api.py` (JWT+RLS) для личного Claude через MCP.

Открытые вопросы: где живёт логика чтения локальных файлов; как авторизовать MCP к серверу
без ручной выдачи ключей администратором; как дать пользователю поделиться своими данными
с врачом одной строкой.

## Решение

1. **Botkin строит только удалённый коннектор к серверным данным.** Локальные файлы читает
   встроенный Filesystem-коннектор самого Claude Desktop пользователя — наш код их не касается
   (privacy: приватное не проходит через Botkin).

2. **Авторизация — Personal Access Token (PAT) → краткоживущий JWT.**
   - PAT (`pat_<telegram_id>_<hex>`) — durable, shareable строка-ключ. Self-service: пользователь
     получает его в боте (`/connect_mcp`), без участия администратора. Отзываемый, именованный,
     со scope `ro`/`rw`. Поделиться с врачом/другим = отдать строку (`ro`).
   - MCP-проксик обменивает PAT на JWT (TTL 5 мин) через публичный `POST /api/agent/exchange_pat_for_jwt`,
     далее зовёт **существующие** endpoints `agent_tools_api.py` без их переписывания.
   - Scope кодируется в PAT → пробрасывается в claim JWT → `require_scope('rw')` отклоняет `ro`
     на write-эндпоинтах. Чтение разрешено всем валидным токенам, запись — только `rw`.

3. **Коннектор — stdio-сервер в `.mcpb`-бандле** (один клик в Claude Desktop). Токен и base_url
   пользователь вводит через `user_config` манифеста (sensitive → keychain). base_url по умолчанию
   `https://health.orangegate.cc` (НЕ `botkin.health` — там статический лендинг без `/api/agent/`).
   Для не-технических пользователей — `type: binary` (PyInstaller), без зависимости от системного Python.

4. **Один MCP-сервер.** Новый `scripts/mcp/botkin_pat_mcp.py` (token-проксик); старый отвязанный
   `scripts/mcp/botkin_mcp.py` (SSH + прямое чтение файлов, без JWT) → `archive/`.

5. **Схема БД — через Alembic** (по ADR-0003): модель `PersonalAccessToken` в `database/models.py`
   + Alembic-ревизия с RLS вручную в `upgrade()`; накат через `migrate.yml` с бэкапом.

## Последствия

**Плюсы:** переиспользуем готовый JWT+RLS-слой и 36 endpoints без изменений; self-service без
администратора; шаринг с врачом одной строкой; приватные файлы физически не попадают в Botkin;
закрывается долг отвязанного `botkin_mcp.py`.

**Минусы / риски:**
- PAT — это standing-доступ на чтение, пока не отозван. Митигировано: отзыв, именование (один PAT
  на получателя), JWT TTL 5 мин, `rw` только на личном токене владельца.
- Публичный `/exchange_pat_for_jwt` без Bearer — нужен rate-limit (базовый в v1).
- Python-`.mcpb` зависит от системного Python — для не-технических пользователей переходим
  на PyInstaller-бинарь (Фаза 4).

## Что НЕ делаем (anti-pattern)

- ❌ Не пишем коннектор к Medtronic CareLink / формату `.pdd` — закрытый проприетарный формат,
  публичного API нет; эти файлы остаются локальными у пользователя.
- ❌ Не читаем локальные приватные файлы пользователя серверным/нашим кодом — это работа его Claude.
- ❌ Не заводим таблицу PAT голым SQL в `database/migrations/*.sql` — только Alembic (ADR-0003).
- ❌ Не направляем проксик на `botkin.health` — там нет `/api/agent/*`.

## Ссылки

- Ресёрч-спайк: `docs/researches/2026-06-28-mcp-connector.md`
- MCPB manifest: https://github.com/anthropics/mcpb/blob/main/MANIFEST.md
- Реализация: PR к [#228](https://github.com/botkin-health/Botkin/issues/228)
