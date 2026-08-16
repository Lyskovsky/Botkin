"""Минимальный in-memory rate-limiter для публичных эндпоинтов (#228).

Используется публичным POST /api/agent/exchange_pat_for_jwt, чтобы перебор PAT
по сети упирался в лимит. Sliding-window per-key (ключ — IP клиента).

Ограничение: состояние в памяти процесса. Бот крутится одним воркером (docker),
поэтому этого достаточно. При горизонтальном масштабировании понадобится Redis —
тогда заменить реализацию, контракт allow(key) сохранить.
"""

import time
from collections import defaultdict
from typing import Optional


class SlidingWindowRateLimiter:
    """Sliding-window счётчик: не более max_requests за window_seconds на ключ."""

    def __init__(self, max_requests: int, window_seconds: float):
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        """True если запрос в пределах лимита (и тогда он учитывается), иначе False.

        now — для тестов (по умолчанию монотонные часы процесса).
        """
        current = time.monotonic() if now is None else now
        window_start = current - self.window_seconds
        # отбрасываем хиты вне окна (заодно не даём списку расти бесконечно)
        recent = [t for t in self._hits[key] if t > window_start]
        if len(recent) >= self.max_requests:
            self._hits[key] = recent
            return False
        recent.append(current)
        if recent:
            self._hits[key] = recent
        else:
            self._hits.pop(key, None)
        return True

    def reset(self, key: Optional[str] = None) -> None:
        """Сбросить счётчик (весь или по ключу) — удобно в тестах."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)
