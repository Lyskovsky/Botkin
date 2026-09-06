"""#407: префикс «план» — запись вносится авансом и потом сводится к факту."""

import re

_RE = re.compile(
    r"^[^\w]*(?:план(?:ирую)?(?:\s+(?:съесть|поесть|на\s+день))?|на\s+день)\b\s*[:\-—–]?\s*",
    re.IGNORECASE | re.UNICODE,
)


def strip_plan_prefix(text: str) -> tuple[str, bool]:
    """Вернуть (текст без префикса, is_plan). Префикс распознаётся только в начале строки."""
    if not text:
        return text, False
    m = _RE.match(text)
    if not m or not text[m.end() :].strip():
        return text, False
    return text[m.end() :].strip(), True
