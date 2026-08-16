# Architecture Decision Records (ADR)

Один файл = одно архитектурное решение. Формат — нумерованный, неподвижный (новые решения добавляют новые файлы, старые не редактируются — только помечаются `Status: Superseded by ADR-NNNN`).

## Зачем

Когда через 6 месяцев Claude (или ты сам) спрашивает «почему мы выбрали X», ответ должен быть **записан**. Иначе повторим уже отвергнутый подход (как случилось 19.05 с persistent containers per user — мы это решение приняли 11.05, забыли, повторили).

## Шаблон

```markdown
# NNNN. Короткое название решения

**Status:** Accepted | Superseded by NNNN | Deprecated | Proposed
**Date:** YYYY-MM-DD
**Deciders:** кто участвовал
**Context:** что заставило принимать решение

## Решение

Что выбрали (одно предложение).

## Альтернативы

Что рассматривали и почему отвергли. Каждая — 1-2 предложения.

## Последствия

- Позитивные
- Негативные / trade-offs
- Что НЕ делать (anti-pattern)

## Ссылки

- Связанные ADR
- Спеки, планы, research-доки
- PR'ы которые этот ADR закрыл / реализовал
```

## Список ADR

| # | Название | Status |
|---|---|---|
| [0008](0008-doctor-report-ips-pdf.md) | Отчёт для врача: содержание по IPS, артефакт — человекочитаемый PDF | Accepted |
| [0007](0007-verified-products-catalog.md) | Справочник проверенных продуктов: двухступенчатый матчинг, автонаполнение вместо CRUD | Accepted |
| [0006](0006-mcp-connector-pat-jwt.md) | MCP-коннектор для Claude Desktop через PAT→JWT | Proposed |
| [0005](0005-cgm-librelinkup-integration.md) | Интеграция CGM (глюкоза) через LibreLinkUp | Accepted |
| [0004](0004-nightly-prod-to-dev-data-sync.md) | Ночной синк данных prod→dev | Accepted |
| [0003](0003-alembic-for-db-migrations.md) | Alembic как фреймворк миграций БД (вместо ручного SQL/Flyway) | Accepted |
| [0002](0002-rejecting-nanoclaw-for-simpler-agent.md) | Отказ от NanoClaw в пользу более простой схемы AI-агента | Accepted |
| [0001](0001-nanoclaw-ephemeral-not-persistent.md) | NanoClaw: ephemeral spawn-containers per session, не persistent per user | Accepted |

_Добавляй новые сверху списка по мере появления._
