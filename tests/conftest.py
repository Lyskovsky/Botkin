import os
import pytest
import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

# Add root to path so database module can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Add telegram-bot to path so handlers can be imported in tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

# Dummy-ключи ДО импорта модулей, требующих env — pytest можно запускать без
# ручных export'ов. setdefault не перетирает явно заданные значения.
_DUMMY_KEYS = {
    "TELEGRAM_BOT_TOKEN": "0000000000:CI_DUMMY_TOKEN_NOT_REAL",
    "ANTHROPIC_API_KEY": "sk-ant-ci-dummy-not-real",
    "OPENAI_API_KEY": "sk-ci-dummy-not-real",
    "GEMINI_API_KEY": "ci-dummy-not-real",
    "GOOGLE_API_KEY": "ci-dummy-not-real",
    # Прод-движок (database/__init__.py) требует DATABASE_URL и падает без неё.
    # Тесты используют отдельный in-memory SQLite и патчат SessionLocal, поэтому
    # сюда достаточно ленивого dummy-URL — реального коннекта по нему не будет.
    "DATABASE_URL": "postgresql://botkin_ci:botkin_ci@localhost:5432/botkin_ci",
}
for _k, _v in _DUMMY_KEYS.items():
    os.environ.setdefault(_k, _v)

from database.models import Base


@pytest.fixture(autouse=True)
def _no_real_llm_keys(monkeypatch):
    """Юнит-тесты не должны ходить в реальные LLM API за деньги.

    config/settings.py делает load_dotenv(override=True) — при локальном прогоне
    реальные ключи из .env попадают в окружение и в кэшированный Settings.
    Принудительно подменяем на dummy в каждом тесте: забытый мок упадёт
    с invalid api key вместо тихого платного запроса.
    """
    for k, v in _DUMMY_KEYS.items():
        monkeypatch.setenv(k, v)
    try:
        from config.settings import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "anthropic_api_key", _DUMMY_KEYS["ANTHROPIC_API_KEY"], raising=False)
        monkeypatch.setattr(s, "openai_api_key", _DUMMY_KEYS["OPENAI_API_KEY"], raising=False)
        monkeypatch.setattr(s, "gemini_api_key", _DUMMY_KEYS["GEMINI_API_KEY"], raising=False)
        monkeypatch.setattr(s, "google_api_key", _DUMMY_KEYS["GOOGLE_API_KEY"], raising=False)
    except Exception:
        pass


# Setup in-memory SQLite database
# Use check_same_thread=False for SQLite with threaded tests if needed
TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture(scope="function")
def test_db():
    engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def mock_session_local(test_db):
    """Patches SessionLocal in all modules that use it in tests"""
    patches = [
        patch("core.health.supplements.SessionLocal", return_value=test_db),
        patch("services.nutrition_service.SessionLocal", return_value=test_db),
        patch("core.health.weekly_nutrition.SessionLocal", return_value=test_db),
        patch("core.health.garmin_data.SessionLocal", return_value=test_db),
    ]

    for p in patches:
        p.start()

    yield test_db

    for p in patches:
        p.stop()
