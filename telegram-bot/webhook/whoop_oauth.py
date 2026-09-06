"""WHOOP OAuth 2.0 flow — мультиюзерное подключение носимого Whoop.

Endpoints (монтируются в apple_health.py через include_router):
  GET /whoop/connect?uid=<telegram_id>&sig=<hmac>
      → редирект на страницу авторизации WHOOP. sig защищает от подделки uid.
  GET /whoop/callback?code=...&state=...
      → обмен code на токены, сохранение в whoop_tokens.json по ключу whoop:<uid>,
        показ пользователю «готово, вернись в бот».

Токены хранятся в data/cache/whoop_tokens.json (bind-mount, переживает рестарт):
  { "whoop:REDACTED_ID": {"access_token","refresh_token","expires_at","scope","connected_at"} }

WHOOP app (client_id/secret) регистрируется владельцем Whoop-устройства в
developer-dashboard.whoop.com. Один app обслуживает до 10 юзеров без одобрения
(development mode), дальше нужна заявка на approval.

Env: WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET, WHOOP_REDIRECT_URI, WHOOP_STATE_SECRET.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whoop", tags=["whoop"])

WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_SCOPES = "read:recovery read:sleep read:cycles read:profile read:body_measurement offline"

# Хранилище токенов — bind-mount data/cache (тот же том, что tokens.json у Zepp).
_TOKENS_PATH = Path(__file__).resolve().parents[1] / "data" / "cache" / "whoop_tokens.json"


def _client_id() -> str:
    return os.environ.get("WHOOP_CLIENT_ID", "")


def _client_secret() -> str:
    return os.environ.get("WHOOP_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    # Дефолт переведён на botkin.health (#387). Whoop-app ни разу не был активирован
    # в проде (WHOOP_CLIENT_ID/SECRET не заданы, ни одного подключения — проверено
    # 24.08.2026), так что менять redirect не с чем координировать: не зарегистрирован
    # ни на одном реальном Whoop-портале. Если Дима (см. todo.md) когда-нибудь создаст
    # app — вписывать сразу этот URL.
    return os.environ.get("WHOOP_REDIRECT_URI", "https://botkin.health/whoop/callback")


def _state_secret() -> str:
    # Секрет для подписи OAuth state. Отдельный WHOOP_STATE_SECRET, иначе
    # fallback на APPLE_HEALTH_TOKEN. НИКАКОГО публичного литерала: в open-source
    # репо рабочий секрет = возможность подделать state и привязать чужой uid.
    secret = os.environ.get("WHOOP_STATE_SECRET") or os.environ.get("APPLE_HEALTH_TOKEN")
    if not secret:
        raise RuntimeError("WHOOP_STATE_SECRET (или APPLE_HEALTH_TOKEN) не задан — нельзя подписать OAuth state")
    return secret


def _sign(uid: str) -> str:
    # Полный SHA-256 hexdigest (64 симв.). Усечение до 16 симв. (64 бита) делало
    # подпись brute-force-уязвимой при известном uid.
    return hmac.new(_state_secret().encode(), uid.encode(), hashlib.sha256).hexdigest()


def _make_state(uid: str) -> str:
    # state = uid.sig — проверяемый на callback, нельзя подделать чужой uid.
    return f"{uid}.{_sign(uid)}"


def _parse_state(state: str) -> str | None:
    try:
        uid, sig = state.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(uid)):
        return None
    return uid


def load_tokens() -> dict:
    if _TOKENS_PATH.exists():
        try:
            return json.loads(_TOKENS_PATH.read_text())
        except Exception:
            logger.exception("whoop_tokens.json unreadable")
    return {}


def save_tokens(all_tokens: dict) -> None:
    _TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKENS_PATH.write_text(json.dumps(all_tokens, indent=2, ensure_ascii=False))


def _store_user_tokens(uid: str, tok: dict) -> None:
    all_tokens = load_tokens()
    all_tokens[f"whoop:{uid}"] = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
        "scope": tok.get("scope", ""),
        "connected_at": int(time.time()),
    }
    save_tokens(all_tokens)


@router.get("/connect")
def whoop_connect(uid: str, sig: str):
    """Старт OAuth: бот формирует ссылку /whoop/connect?uid=..&sig=.. и шлёт юзеру.
    sig = _sign(uid) — чтобы нельзя было подсунуть чужой uid."""
    if not _client_id():
        raise HTTPException(503, "WHOOP_CLIENT_ID не настроен на сервере")
    if not hmac.compare_digest(sig, _sign(uid)):
        raise HTTPException(403, "Невалидная подпись ссылки")
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": WHOOP_SCOPES,
        "state": _make_state(uid),
    }
    url = WHOOP_AUTH_URL + "?" + "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return RedirectResponse(url)


@router.get("/callback")
def whoop_callback(code: str = "", state: str = "", error: str = ""):
    """WHOOP редиректит сюда после авторизации юзера."""
    if error:
        return HTMLResponse(f"<h2>Не удалось подключить Whoop</h2><p>{error}</p>", status_code=400)
    uid = _parse_state(state)
    if not uid:
        return HTMLResponse("<h2>Ошибка: невалидный state</h2>", status_code=403)
    if not code:
        return HTMLResponse("<h2>Ошибка: нет кода авторизации</h2>", status_code=400)

    resp = requests.post(
        WHOOP_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        logger.error("WHOOP token exchange failed %s: %s", resp.status_code, resp.text[:300])
        return HTMLResponse(f"<h2>Whoop вернул ошибку</h2><p>{resp.status_code}</p>", status_code=502)

    _store_user_tokens(uid, resp.json())
    logger.info("WHOOP connected for uid=%s", uid)
    return HTMLResponse(
        "<html><head><meta charset='utf-8'><title>Botkin × Whoop</title></head>"
        "<body style='font-family:-apple-system,sans-serif;max-width:480px;margin:60px auto;text-align:center'>"
        "<h2>✅ Whoop подключён!</h2>"
        "<p>Данные сна, восстановления и пульса теперь будут приходить в Botkin автоматически.</p>"
        "<p>Можешь вернуться в Telegram к боту 🩺</p>"
        "</body></html>"
    )


def refresh_token_for(uid: str) -> str | None:
    """Обновляет access_token по refresh_token. Возвращает свежий access_token или None."""
    all_tokens = load_tokens()
    rec = all_tokens.get(f"whoop:{uid}")
    if not rec or not rec.get("refresh_token"):
        return None
    resp = requests.post(
        WHOOP_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": rec["refresh_token"],
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "scope": "offline",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if resp.status_code != 200:
        logger.error("WHOOP refresh failed for uid=%s: %s", uid, resp.text[:200])
        return None
    _store_user_tokens(uid, resp.json())
    return resp.json().get("access_token")


def get_valid_access_token(uid: str) -> str | None:
    """Возвращает действующий access_token (рефрешит если протух). None если юзер не подключён."""
    rec = load_tokens().get(f"whoop:{uid}")
    if not rec:
        return None
    if int(time.time()) >= int(rec.get("expires_at", 0)):
        return refresh_token_for(uid)
    return rec["access_token"]


def make_connect_link(uid: int) -> str:
    """Хелпер для бота: собрать подписанную ссылку для кнопки «Подключить Whoop»."""
    base = _redirect_uri().rsplit("/whoop/callback", 1)[0]
    return f"{base}/whoop/connect?uid={uid}&sig={_sign(str(uid))}"
