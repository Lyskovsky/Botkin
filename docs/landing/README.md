# 🌐 Landing page

Лендинг проекта. Хостится на `botkin.health` — статикой, nginx раздаёт файлы из
`/opt/botkin-site/` на сервере (Hetzner). Деплой — **ручной scp**, не git push:

```bash
scp docs/landing/index.html root@116.203.213.137:/opt/botkin-site/index.html
scp docs/landing/user_guide.html root@116.203.213.137:/opt/botkin-site/user_guide.html
scp docs/landing/mcp_guide.html root@116.203.213.137:/opt/botkin-site/mcp_guide.html
```

## Структура

```
docs/landing/
├── index.html          ← главный лендинг (single-file)
├── user_guide.html      ← короткий гайд по боту (10 минут на старт)
├── mcp_guide.html       ← гайд по подключению Claude Desktop через MCP
├── assets/              ← фото, скриншоты, фавиконы
└── README.md            ← этот файл
```

## Палитра

Совпадает с `telegram-bot/mc_template.html` (mc-dashboard):

| Переменная | Значение | Применение |
|---|---|---|
| `--bg` | `#0a0e17` | основной фон |
| `--card` | `#141a28` | карточки, секции |
| `--text` | `#e8eef7` | основной текст |
| `--muted` | `#7a879f` | вторичный текст |
| `--g` | `#00ff9d` | акцент (CTA, ссылки) |
| `--y` | `#ffb800` | предупреждения / tag-stub |

Менять везде одновременно в `index.html`, `user_guide.html`, `mcp_guide.html`,
`telegram-bot/mc_template.html`, `webhook/admin.py` (наследует ту же палитру).

## Как смотреть локально

```bash
cd docs/landing
python3 -m http.server 8000
# открыть http://localhost:8000/
```

## После правок — не забыть

1. Проверить баланс тегов (`<div>`, `<details>`, `<a>`) перед деплоем — файлы правятся руками, без сборки.
2. scp на сервер (см. выше) — git push репозиторий обновляет, сайт нет.
3. Если менялась версия/дата в футере — она нигде больше не подтягивается автоматически, только руками.

## Дальше можно

- [ ] EN-версия
- [ ] Видео-демо в hero
- [ ] Testimonials от пилотных пользователей (с их согласия)
