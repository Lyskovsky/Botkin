# Botkin MCP-коннектор для Claude Desktop (#228)

Отдаёт Claude Desktop серверные данные Botkin (питание, вес, биомаркеры, давление,
тренировки) по личному токену. **Локальные** файлы пользователя (КПТ-дневник, сканы
анализов) коннектор не читает — для них в Claude Desktop есть встроенный коннектор
файловой системы. Гибридная приватность: серверное — через Botkin, локальное — мимо.

## Файлы

| Файл | Что это |
|---|---|
| `botkin_client.py` | HTTP-клиент: PAT→JWT обмен, кэш JWT, 401-повтор. Без зависимости от `mcp`, юнит-тестируется (`tests/test_botkin_client.py`). |
| `botkin_pat_mcp.py` | stdio MCP-сервер: регистрирует инструменты-обёртки над `/api/agent/*`. |
| `manifest.json` | Манифест `.mcpb` (manifest_version 0.3): `user_config.pat` (sensitive→keychain) + `base_url`. |
| `requirements.txt` | Зависимости бандла (`mcp`, `requests`). |
| ~~`botkin_mcp.py`~~ | удалён — старый SSH-вариант без JWT (история в git). |

## Как пользователь получает токен

В боте `@Botkin_md_bot`: команда `/connect_mcp` → выбрать доступ
(полный / только чтение) → бот выдаёт строку токена. Список и отзыв — `/my_connections`.
ro-токеном можно поделиться с врачом: он читает данные, но ничего не меняет (write → 403).

## Сборка `.mcpb`

```bash
# из этой директории; mcpb CLI: npm i -g @anthropic-ai/mcpb
mcpb pack .            # соберёт botkin-connector.mcpb (manifest + скрипты)
```

Затем пользователь открывает `.mcpb` в Claude Desktop → Settings → Extensions,
вставляет токен из бота в поле «Токен Botkin». `base_url` менять не нужно
(дефолт — прод `https://health.orangegate.cc`; для дев-стенда — свой адрес).

> ⚠️ Python-`.mcpb` исполняется системным Python пользователя. Для нетехнических
> пользователей (напр. Ника) на след. шаге планируется PyInstaller-бинарь, чтобы не
> зависеть от наличия Python и пакетов. Технические пилоты (Олег/Андрей) ставят как есть.

Подробное руководство пользователя — `docs/user_guide/ru/claude-desktop-mcp.md` (Фаза 5).
Архитектура и обоснование — `docs/architecture/decisions/0006-mcp-connector-pat-jwt.md`.
