FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей
# libpango/libgdk-pixbuf/libcairo + fontconfig + DejaVu (кириллица) — рантайм weasyprint (#290).
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libcairo2 \
    libffi8 fontconfig shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Копирование requirements и установка зависимостей
COPY requirements.txt .
RUN pip install -r requirements.txt

# Копирование кода
COPY telegram-bot/ ./telegram-bot/
COPY config/ ./config/
COPY core/ ./core/
COPY services/ ./services/
COPY database/ ./database/
COPY helpers/ ./helpers/
COPY infrastructure/ ./infrastructure/
COPY scripts/ ./scripts/

# Создание директорий для логов и данных
RUN mkdir -p /app/logs /app/data

# Порт (не используется для бота, но на будущее)
EXPOSE 8080

# Запуск бота
CMD ["python", "telegram-bot/bot.py"]
