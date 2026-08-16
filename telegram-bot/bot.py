#!/usr/bin/env python3
"""
Главный файл для запуска Botkin Telegram Bot.
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent))

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update, BotCommand, BotCommandScopeAllPrivateChats, MenuButtonWebApp, WebAppInfo
from dotenv import load_dotenv
import os

from core._version import __version__
from bot_token import resolve_bot_token
from config.settings import public_base_url

# Загружаем переменные окружения
load_dotenv(override=True)
load_dotenv(Path(__file__).parent.parent / ".env", override=True)


# Настраиваем логирование
class ConsoleFilter(logging.Filter):
    """Фильтр для консоли: только свои логи и ошибки"""

    def filter(self, record):
        # Пропускаем свои логи (main, handlers.*, services.*)
        if record.name == "__main__" or record.name.startswith(("handlers.", "services.")):
            return True
        # Пропускаем предупреждения и ошибки ото всех
        return record.levelno >= logging.WARNING


def setup_logging():
    # Создаем директорию для логов
    Path("logs").mkdir(exist_ok=True)

    # Корневой логгер
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Очищаем существующие хендлеры
    root_logger.handlers.clear()

    # 1. Файловый хендлер — пишет ВСЁ (DEBUG и выше), ротация 5 МБ × 3 бэкапа = ~15 МБ макс
    file_handler = RotatingFileHandler("logs/bot.log", encoding="utf-8", maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root_logger.addHandler(file_handler)

    # 2. Консольный хендлер - только важное и красивое
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    console_handler.addFilter(ConsoleFilter())
    root_logger.addHandler(console_handler)

    # Заглушаем шумные библиотеки (чтобы они не захламляли даже файл слишком сильно, если там debug)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # Убираем спам о каждом апдейте


setup_logging()
logger = logging.getLogger(__name__)

# Создаем директорию для логов
Path("logs").mkdir(exist_ok=True)

# Получаем токен бота
BOT_TOKEN = resolve_bot_token()
if not BOT_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не найден в переменных окружения!")
    logger.error("Создайте файл .env и добавьте: TELEGRAM_BOT_TOKEN=ваш_токен")
    sys.exit(1)

if not os.getenv("TELEGRAM_WEBHOOK_SECRET"):
    logger.error(
        "⚠️  TELEGRAM_WEBHOOK_SECRET не задан — /telegram/webhook открыт для всех."
        " Сгенерируй: openssl rand -hex 32, добавь в .env и перерегистрируй"
        " webhook через setWebhook(secret_token=...)."
    )


# Инициализируем бота и диспетчер
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


def register_handlers(dp: Dispatcher):
    """Регистрация всех обработчиков"""
    handlers_count = 0
    registered_modules = []
    errors = []

    # Register authorization middleware FIRST
    try:
        from middlewares.auth import AuthMiddleware

        dp.message.middleware(AuthMiddleware())
        logger.info("✅ Authorization middleware registered")
    except Exception as e:
        logger.error(f"❌ Failed to register auth middleware: {e}")
        errors.append(f"Auth middleware: {e}")

    try:
        from handlers.commands import router as commands_router

        dp.include_router(commands_router)
        count = len(commands_router.observers) if hasattr(commands_router, "observers") else 0
        handlers_count += count
        registered_modules.append("команд")
    except Exception as e:
        errors.append(f"Обработчик команд: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика команд: {e}")

    try:
        from handlers.apple_health_connect import router as apple_health_router

        dp.include_router(apple_health_router)
        count = len(apple_health_router.observers) if hasattr(apple_health_router, "observers") else 0
        handlers_count += count
        registered_modules.append("apple-health-connect")
    except Exception as e:
        errors.append(f"Обработчик apple-health-connect: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика apple-health-connect: {e}")

    try:
        from handlers.doc_upload import router as doc_upload_router

        dp.include_router(doc_upload_router)
        count = len(doc_upload_router.observers) if hasattr(doc_upload_router, "observers") else 0
        handlers_count += count
        registered_modules.append("doc_upload")
    except Exception as e:
        errors.append(f"Обработчик doc_upload: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика doc_upload: {e}")

    try:
        from handlers.photo import router as photo_router

        dp.include_router(photo_router)
        count = len(photo_router.observers) if hasattr(photo_router, "observers") else 0
        handlers_count += count
        registered_modules.append("фото")
    except Exception as e:
        errors.append(f"Обработчик фото: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика фото: {e}")

    try:
        from handlers.voice import router as voice_router

        dp.include_router(voice_router)
        count = len(voice_router.observers) if hasattr(voice_router, "observers") else 0
        handlers_count += count
        registered_modules.append("голоса")
    except Exception as e:
        errors.append(f"Обработчик голоса: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика голоса: {e}")

    try:
        from handlers.text import router as text_router

        dp.include_router(text_router)
        count = len(text_router.observers) if hasattr(text_router, "observers") else 0
        handlers_count += count
        registered_modules.append("текста")
    except Exception as e:
        errors.append(f"Обработчик текста: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика текста: {e}")

    try:
        from handlers.setup import router as setup_router

        dp.include_router(setup_router)
        count = len(setup_router.observers) if hasattr(setup_router, "observers") else 0
        handlers_count += count
        registered_modules.append("профиля")
    except Exception as e:
        errors.append(f"Обработчик профиля: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика профиля: {e}")

    try:
        from handlers.sync_cmd import router as sync_router

        dp.include_router(sync_router)
        count = len(sync_router.observers) if hasattr(sync_router, "observers") else 0
        handlers_count += count
        registered_modules.append("/sync")
    except Exception as e:
        errors.append(f"Обработчик /sync: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика /sync: {e}")

    try:
        from handlers.connect_cgm import router as connect_cgm_router

        dp.include_router(connect_cgm_router)
        count = len(connect_cgm_router.observers) if hasattr(connect_cgm_router, "observers") else 0
        handlers_count += count
        registered_modules.append("/connect_cgm")
    except Exception as e:
        errors.append(f"Обработчик /connect_cgm: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика /connect_cgm: {e}")

    try:
        from handlers.connect_claude import router as connect_claude_router

        dp.include_router(connect_claude_router)
        count = len(connect_claude_router.observers) if hasattr(connect_claude_router, "observers") else 0
        handlers_count += count
        registered_modules.append("/connect_mcp")
    except Exception as e:
        errors.append(f"Обработчик /connect_mcp: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика /connect_mcp: {e}")

    try:
        from handlers.verified_products import router as verified_products_router

        dp.include_router(verified_products_router)
        count = len(verified_products_router.observers) if hasattr(verified_products_router, "observers") else 0
        handlers_count += count
        registered_modules.append("verified-products")
    except Exception as e:
        errors.append(f"Обработчик verified-products: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика verified-products: {e}")

    try:
        from handlers.feedback import router as feedback_router

        dp.include_router(feedback_router)
        count = len(feedback_router.observers) if hasattr(feedback_router, "observers") else 0
        handlers_count += count
        registered_modules.append("/feedback")
    except Exception as e:
        errors.append(f"Обработчик /feedback: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика /feedback: {e}")

    try:
        from handlers.food_audit import router as food_audit_router

        dp.include_router(food_audit_router)
        count = len(food_audit_router.observers) if hasattr(food_audit_router, "observers") else 0
        handlers_count += count
        registered_modules.append("/food_audit")
    except Exception as e:
        errors.append(f"Обработчик /food_audit: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика /food_audit: {e}")

    try:
        from handlers.persona_cmd import router as persona_router

        dp.include_router(persona_router)
        count = len(persona_router.observers) if hasattr(persona_router, "observers") else 0
        handlers_count += count
        registered_modules.append("/persona")
    except Exception as e:
        errors.append(f"Обработчик /persona: {e}")
        logger.error(f"❌ Ошибка регистрации обработчика /persona: {e}")

    # Apple Health handlers removed
    pass

    if registered_modules:
        modules_str = ", ".join(registered_modules)
        logger.info(f"✅ Обработчики {modules_str} зарегистрированы: {handlers_count} обработчиков")

    # УБИРАЕМ универсальный обработчик текстовых сообщений - он мешает нормальной обработке
    # Вместо этого используем обработчик в handlers/text.py
    # Оставляем только обработчик для диагностики фото, которые не обработались

    # Middleware для обеспечения идемпотентности (дедупликация)
    try:
        from middlewares.idempotency import IdempotencyMiddleware

        dp.update.outer_middleware(IdempotencyMiddleware())
        logger.info("✅ IdempotencyMiddleware подключен")
    except ImportError as e:
        logger.error(f"❌ Не удалось подключить IdempotencyMiddleware: {e}")
        errors.append(f"Middleware: {e}")

    # Middleware для сборки медиагрупп
    try:
        from middlewares.media_group import MediaGroupMiddleware

        dp.message.middleware(MediaGroupMiddleware())
        logger.info("✅ MediaGroupMiddleware подключен")
    except Exception as e:
        logger.error(f"❌ Не удалось подключить MediaGroupMiddleware: {e}")
        errors.append(f"MediaGroupMiddleware: {e}")

    # Middleware: индикатор "печатает..." в Telegram пока handler работает
    try:
        from middlewares.typing_indicator import TypingMiddleware

        dp.message.middleware(TypingMiddleware())
        logger.info("✅ TypingMiddleware подключен")
    except Exception as e:
        logger.error(f"❌ Не удалось подключить TypingMiddleware: {e}")
        errors.append(f"TypingMiddleware: {e}")

    # Middleware для логирования всех апдейтов
    @dp.update.outer_middleware()
    async def log_update_middleware(handler, event, data):
        # event is the Update object here
        if isinstance(event, Update):
            log_msg = f"📥 Update {event.update_id}:"
            if event.message:
                log_msg += f" MSG id={event.message.message_id} date={event.message.date}"
            elif event.edited_message:
                log_msg += f" EDIT id={event.edited_message.message_id} date={event.edited_message.edit_date}"
            elif event.callback_query:
                log_msg += f" CB id={event.callback_query.id} data={event.callback_query.data}"
            else:
                log_msg += " (other type)"
            logger.info(log_msg)
        return await handler(event, data)

    # Добавляем обработчик для всех обновлений (для диагностики необработанных)
    # catch_all_updates удален чтобы исключить побочные эффекты

    # Проверяем наличие необходимых директорий
    try:
        Path("data/media/nutrition").mkdir(parents=True, exist_ok=True)
        Path("logs/nutrition").mkdir(parents=True, exist_ok=True)
    except Exception as e:
        errors.append(f"Создание директорий: {e}")

    # Если были ошибки - выводим тревожное сообщение
    if errors:
        error_msg = "❌ ⚠️ 🚨 Botkin НЕ ЗАПУЩЕН!\n\n"
        error_msg += "Ошибки при инициализации:\n"
        for i, error in enumerate(errors, 1):
            error_msg += f"{i}. {error}\n"
        error_msg += "\n⚠️ Перешли это сообщение разработчику для разбора логов!"
        logger.error(error_msg)
        print(error_msg)
        raise Exception("Ошибки при инициализации бота")

    # Если всё хорошо - одна строка успеха
    print("\n" + "=" * 50)
    print(f"🚀  Botkin v{__version__}")
    print("✅  Бот успешно запущен и готов к работе")
    print("📝  Логи пишутся в файл: logs/bot.log")
    print("=" * 50 + "\n")

    # Запускаем бота


async def main():
    """Главная функция запуска бота"""

    errors = []

    # Register handlers and middleware
    register_handlers(dp)

    # Set bot commands in menu
    commands = [
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="day", description="Итоги дня"),
        BotCommand(command="week", description="Анализ недели"),
        BotCommand(command="vitamins", description="Чек-лист витаминов"),
        BotCommand(command="doc", description="Загрузить анализ или заключение врача"),
        BotCommand(command="share", description="Поделиться дашбордом здоровья"),
        BotCommand(command="profile", description="Настроить профиль (рост, возраст, цель)"),
        BotCommand(command="connect_mcp", description="Подключить AI-коннектор (MCP)"),
        BotCommand(command="agent_reset", description="Сбросить недавний контекст AI-ассистента"),
        BotCommand(command="feedback", description="Сообщить об ошибке / предложить идею"),
        BotCommand(command="help", description="Помощь"),
    ]

    # Register dispatcher in the webhook server (for /telegram/webhook endpoint)
    try:
        from webhook.apple_health import set_telegram_dispatcher, start_webhook_server

        set_telegram_dispatcher(bot, dp)
        webhook_enabled = True
        logger.info("✅ Telegram dispatcher registered in webhook server")
    except ImportError:
        webhook_enabled = False
        logger.warning("⚠️ Apple Health webhook не загружен (webhook/apple_health.py не найден)")

    # Регистрируем /android_health_v1 на том же FastAPI app (импорт достаточен — декоратор @app.post срабатывает при загрузке модуля)
    try:
        import webhook.android_health  # noqa: F401

        logger.info("✅ Android Health Connect webhook зарегистрирован (/android_health_v1)")
    except ImportError:
        logger.warning("⚠️ Android Health webhook не загружен (webhook/android_health.py не найден)")

    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        logger.info("✅ Команды бота установлены")
    except Exception as e:
        logger.error(f"❌ Не удалось установить команды: {e}")

    # Кнопка «Дневник» (Menu Button) — WebApp мини-приложение.
    # URL берётся из того же хелпера public_base_url() (env BOTKIN_PUBLIC_URL), что и
    # публичные ссылки /share, /report (#205) — единый источник, чтобы дев-стенд открывал
    # дев-вебапп, а не прод. Раньше кнопка читала отдельную PUBLIC_BASE_URL (дефолт
    # health.orangegate.cc) → дев-бот вёл мини-апп на прод, прод-сервер валидировал
    # дев-initData прод-токеном → 403 на вкладках. Идемпотентно: Telegram принимает повторные вызовы.
    webapp_url = f"{public_base_url()}/webapp/"
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Дневник", web_app=WebAppInfo(url=webapp_url)))
        logger.info(f"✅ Menu button 'Дневник' установлен → {webapp_url}")
    except Exception as e:
        logger.error(f"❌ Не удалось установить menu button: {e}")

    # Регистрируем webhook у Telegram. Идемпотентно: ставим только если
    # текущий URL отличается. Без этого после смены TELEGRAM_BOT_TOKEN
    # (или первого деплоя) Telegram не знает куда слать апдейты — сообщения
    # копятся в очереди и бот «молчит». Прецедент: 12.05.2026 при свитче
    # @HealthVault_bot → @Botkin_md_bot webhook не зарегистрировали вручную.
    # Default — legacy-домен: прод-.env не задаёт TELEGRAM_WEBHOOK_URL, а nginx
    # botkin.health не проксирует /telegram/ (проверено 11.06.2026). Перевод на
    # botkin.health — только вместе с nginx-location и сменой webhook у Telegram.
    # Дев-стенд без публичного TLS-эндпойнта: Telegram-апдейты тянем polling'ом,
    # но FastAPI-сервер (/health, дашборд, /api/agent) ВСЁ РАВНО поднимаем —
    # поэтому отключаем только webhook-регистрацию, а не сервер целиком.
    # Включается переменной BOTKIN_FORCE_POLLING=1 (см. docker-compose.dev.yml).
    force_polling = os.getenv("BOTKIN_FORCE_POLLING") == "1"

    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "https://health.orangegate.cc/telegram/webhook")
    if webhook_enabled and not force_polling and webhook_url:
        try:
            info = await bot.get_webhook_info()
            if info.url != webhook_url:
                await bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"])
                logger.info(f"✅ Webhook зарегистрирован: {webhook_url}")
            else:
                logger.info(f"✅ Webhook уже актуален: {webhook_url}")
        except Exception as e:
            logger.error(f"❌ Не удалось установить webhook: {e}")
    elif force_polling:
        # polling и webhook у Telegram взаимоисключающи — снимаем webhook,
        # иначе getUpdates вернёт 409 и дев-бот «молчит».
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            logger.info("⚙️ BOTKIN_FORCE_POLLING=1 — webhook снят, Telegram через polling")
        except Exception as e:
            logger.error(f"❌ Не удалось снять webhook: {e}")

    # Запуск: FastAPI-сервер (/telegram/webhook + /apple_health + /webapp + /api/agent
    # + дашборд) и/или polling. В проде — только сервер (webhook). На дев-стенде —
    # сервер И polling одновременно (force_polling): сервер нужен для /health,
    # дашборда и agent-tools, а апдейты Telegram тянутся polling'ом без публичного TLS.
    try:
        tasks = []
        if webhook_enabled:
            logger.info("🌐 Запуск webhook-сервера на порту 8081...")
            tasks.append(start_webhook_server())
        if force_polling or not webhook_enabled:
            logger.info("📡 Запуск polling...")
            tasks.append(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))
        await asyncio.gather(*tasks)
    except Exception as e:
        error_msg = "❌ ⚠️ 🚨 Botkin НЕ ЗАПУЩЕН!\n\n"
        error_msg += f"Ошибка при запуске: {e}\n"
        error_msg += "\n⚠️ Перешли это сообщение разработчику для разбора логов!"
        logger.error(error_msg, exc_info=True)
        print(error_msg)
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️  Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
