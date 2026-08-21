"""Единая точка доступа к импортёру LibreLinkUp (#135).

Проблема, которую решает: `scripts/import/librelinkup.py` — не пакет (import зарезервирован),
и раньше его грузили по пути в двух местах (connect_cgm, agent_tools_api) ДВУМЯ разными
module-объектами → у каждого свой `_cached_client`, токен не переиспользовался.

Здесь грузим импортёр ОДИН раз и регистрируем в sys.modules — все, кто импортирует этот
модуль, делят один и тот же объект импортёра (и общий кэш клиента/токена).
"""

import sys
import importlib.util
from pathlib import Path

_MODNAME = "librelinkup_import"

if _MODNAME in sys.modules:
    _llu = sys.modules[_MODNAME]
else:
    _path = Path(__file__).resolve().parents[2] / "scripts" / "import" / "librelinkup.py"
    _spec = importlib.util.spec_from_file_location(_MODNAME, _path)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Не удалось загрузить импортёр LibreLinkUp из {_path}")
    _llu = importlib.util.module_from_spec(_spec)
    sys.modules[_MODNAME] = _llu  # регистрируем ДО exec — повторные загрузки вернут этот объект
    _spec.loader.exec_module(_llu)

# Ре-экспорт публичного API импортёра (всё ходит через общий _cached_client/токен).
get_cached_client = _llu.get_cached_client
fetch_patient_ids = _llu.fetch_patient_ids
refresh_glucose_for_telegram = _llu.refresh_glucose_for_telegram
LoginOnCooldownError = _llu.LoginOnCooldownError
# Самообслуживание follower-аккаунтов (#381): валидация кред до записи в БД
# и список регионов для клавиатуры выбора.
validate_follower = _llu.validate_follower
available_regions = _llu.available_regions

__all__ = ["get_cached_client", "fetch_patient_ids", "refresh_glucose_for_telegram", "LoginOnCooldownError"]
