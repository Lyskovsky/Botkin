# Ресёрч: MCP-коннектор Botkin для Claude Desktop (спайк Фазы 0)

**Дата:** 2026-06-28
**Задача:** [#228](https://github.com/botkin-health/Botkin/issues/228)
**Автор:** Александр Лысковский (+ Claude)

## Цель

Дать пользователю Botkin подключить свой **Claude Desktop** к своим **серверным** данным
(питание, вес, биомаркеры) через MCP-коннектор. Локальные приватные файлы (КПТ-дневник
Ники, выгрузки `.pdd` кардиомонитора Medtronic Reveal LINQ Андрея, не загруженные анализы)
читает **сам Claude Desktop пользователя** (встроенный Filesystem-коннектор + vision) —
наш код их не касается. Это лучшая privacy-гарантия: приватное не проходит через Botkin.

## Архитектура (итог брейншторма)

```
Claude Desktop пользователя
 ├─ Filesystem-коннектор (встроенный) → локальные файлы на диске     ← мимо Botkin
 └─ Botkin MCP (stdio, .mcpb) → PAT→JWT обмен → /api/agent/* (RLS)   ← только свои данные
```

- **PAT** (`pat_<telegram_id>_<hex>`) — durable, shareable строка-ключ, выдаётся в боте.
  Отзываемый, именованный, со scope `ro`/`rw`. Чтобы поделиться с врачом — отдать строку (`ro`).
- **MCP-проксик** (stdio, на компе пользователя) обменивает PAT на короткоживущий JWT (5 мин)
  через новый публичный endpoint, далее зовёт **существующие** endpoints `agent_tools_api.py`.
- **Scope** живёт в PAT → пробрасывается в claim JWT → `ro` отклоняется write-эндпоинтами.

## Находки спайка

### 1. Доступность API снаружи

| Хост | `/api/agent/recent_meals` без токена | Вывод |
|---|---|---|
| `health.orangegate.cc` | **HTTP 422** (endpoint жив) | ✅ прод base_url для проксика |
| `dev.botkin.health` | **HTTP 422** (endpoint жив) | ✅ dev base_url (пилот тестируем тут) |
| `botkin.health` | **HTTP 404** | ⚠️ статический лендинг nginx, `/api/agent/` НЕ проксирует |

**Вывод:** base_url проксика — `https://health.orangegate.cc` (прод) и `https://dev.botkin.health`
(дев). **НЕ** `botkin.health`. Новый сервис/порт не нужен — router смонтирован в основном FastAPI
(`telegram-bot/webhook/apple_health.py:1113-1115`, `prefix="/api/agent"`).

### 2. Механика `.mcpb` (MCP Bundle, ex-DXT)

`manifest_version: 0.3`. Пользовательские настройки — в `user_config`; подстановка
`${user_config.KEY}` → в `env`/`args` команды stdio-сервера. Sensitive-поля маскируются
в UI и хранятся в системном keychain.

```json
{
  "manifest_version": "0.3",
  "name": "botkin-connector",
  "server": {
    "type": "python",
    "entry_point": "server/botkin_pat_mcp.py",
    "mcp_config": {
      "command": "python",
      "args": ["${__dirname}/server/botkin_pat_mcp.py"],
      "env": {
        "BOTKIN_PAT": "${user_config.pat}",
        "BOTKIN_BASE_URL": "${user_config.base_url}"
      }
    }
  },
  "user_config": {
    "pat": { "type": "string", "title": "Токен Botkin", "sensitive": true, "required": true },
    "base_url": { "type": "string", "title": "Сервер", "default": "https://health.orangegate.cc" }
  }
}
```

**⚠️ Риск:** `server.type: "python"` подставляет команду `python` — зависит от **системного Python**
на компе пользователя. Для технического пилота (Олег/Андрей) — ок. Для Ники (не-разработчик)
надёжнее `type: "binary"` (standalone-сборка PyInstaller, без зависимости от Python).
→ Решение для Фазы 4: MVP `python` для пилота; для Ники — PyInstaller-бинарь.

### 3. Миграция БД — через Alembic, НЕ голый SQL

По [ADR-0003](../architecture/decisions/0003-alembic-for-db-migrations.md): источник истины
схемы — `database/models.py` + Alembic-ревизии; старые `database/migrations/*.sql` — архив,
не накатываются. **Поправка к плану** (прожарка ошибочно предлагала `add_*.sql`):

- Модель `PersonalAccessToken` в `database/models.py` (SQLAlchemy 2.0, FK на `users.telegram_id`,
  `JSON→JSONB` через `with_variant` если понадобится JSONB — чтобы sqlite-тесты жили).
- Alembic-ревизия (slug-стиль id, напр. `pat0token01_add_personal_access_tokens.py`).
  RLS-политику добавить **руками в `upgrade()`** — `autogenerate` RLS не эмитит.
- Накат — только через GitHub Actions `migrate.yml` (env=dev → prod), с обязательным
  `pg_dump`-бэкапом. CI round-trip (`upgrade head → check → downgrade base → upgrade head`)
  должен быть зелёным.

### 4. Один MCP вместо двух

Существующий `scripts/mcp/botkin_mcp.py` — отвязан (нет в конфигах), ходит SSH-ом напрямую +
читает локальные файлы, без JWT. Под новую архитектуру (token-проксик, локальные файлы =
забота Claude) он не нужен. **Решение:** один новый `scripts/mcp/botkin_pat_mcp.py`,
старый `botkin_mcp.py` → `archive/`. Закрывает долг из `todo.md` («решить: register или archive»).

### 5. Авторизация (PAT → JWT)

JWT-слой (`telegram-bot/webhook/jwt_auth.py`) расширяется:
- `generate_agent_jwt(..., scope=...)` кладёт `scope` в payload.
- Новый dependency `require_scope('rw')` — читает claim, при `ro` на write → 403.
- Новый публичный `POST /api/agent/exchange_pat_for_jwt`: валидирует активный PAT,
  пишет `last_used`, выдаёт JWT 5 мин. Под rate-limit (public, без Bearer).
- `hvt_`-паттерн генерации (`database/crud.py`) переиспользуем для `pat_`.

## Открытые вопросы (на потом, не блокеры v1)

- Доп. защита для `rw`-токена (2FA) — для семейного пилота не нужна (short-TTL JWT + revoke хватает).
- Rate-limit на `/exchange_pat_for_jwt` — базовый в приложении; усиление на nginx/Cloudflare — позже.
- PyInstaller-бинарь для не-технических пользователей — Фаза 4.

## Источники

- MCPB manifest spec: https://github.com/anthropics/mcpb/blob/main/MANIFEST.md
- ADR-0003 (Alembic): `docs/architecture/decisions/0003-alembic-for-db-migrations.md`
- Существующий API: `telegram-bot/webhook/agent_tools_api.py`, `telegram-bot/webhook/jwt_auth.py`
