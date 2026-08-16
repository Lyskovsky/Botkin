# launchd: ежедневный импорт состава тела с весов Withings

Мышечная масса, вода, костная масса и висцеральный жир **не имеют типов в HealthKit**,
поэтому через Apple Health / HAE в Botkin не доходят. Этот агент раз в сутки забирает
их напрямую из облака Withings и пишет через `POST /api/agent/log_body_composition`
(HTTPS + PAT) — доступ к серверу не нужен.

## Установка (macOS)

1. Заполнить `.env` в корне репозитория (chmod 600):

   ```
   BOTKIN_PAT=...              # personal access token Botkin, scope rw
   BOTKIN_API_BASE=https://... # базовый URL API
   WITHINGS_CLIENT_ID=...      # приложение Withings (можно то же, что у локального MCP)
   WITHINGS_CLIENT_SECRET=...
   WITHINGS_TOKENS_PATH=...    # опц.: общий токен-файл с Withings-MCP, чтобы refresh
                               # не ротировался вразнобой двумя клиентами
   WITHINGS_MIN_WEIGHT=90      # весы дома общие: замеры других членов семьи Withings
                               # относит к владельцу аккаунта — отсекаем по коридору
   ```

2. Подставить свои пути и telegram_id в plist, положить в `~/Library/LaunchAgents/`, затем:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/health.botkin.withings-sync.plist
   launchctl kickstart -k gui/$(id -u)/health.botkin.withings-sync   # разовый прогон
   ```

Лог: `~/Library/Logs/botkin-withings-sync.log`. Импорт идемпотентный — повторный
прогон не плодит дубли (ключ `(user_id, measured_at)`), `None` не затирает поля
других каналов.

Снять: `launchctl bootout gui/$(id -u)/health.botkin.withings-sync`.
