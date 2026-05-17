"""In-memory state «плохих» кампаний для команды /kill_bad.

Сторож (check_metrics_and_anomalies) после каждой проверки сохраняет
сюда ID кампаний которые пробили пороги. Команда /kill_bad читает этот
список и отключает все эти кампании одним проходом.

Состояние теряется при рестарте процесса — это ок, потому что Сторож
запускается каждый час и обновит список.
"""

import threading
import time
from typing import List

_lock = threading.Lock()
_bad_ids: List[int] = []
_last_update_ts: float = 0.0


def set_bad_ids(ids: List[int]) -> None:
    """Обновляет список плохих ID (вызывается Сторожем после проверки)."""
    global _bad_ids, _last_update_ts
    with _lock:
        _bad_ids = list(ids)
        _last_update_ts = time.time()


def get_bad_ids() -> tuple[List[int], float]:
    """Возвращает (список ID, timestamp последнего обновления).

    Если возраст обновления > 90 минут — данные устарели (Сторож сломался
    или ещё не запустился), вернётся пустой список.
    """
    with _lock:
        if not _bad_ids:
            return [], _last_update_ts
        # Данные старше 90 минут считаются устаревшими
        if time.time() - _last_update_ts > 90 * 60:
            return [], _last_update_ts
        return list(_bad_ids), _last_update_ts


def clear() -> None:
    """Очищает список (например после /kill_bad)."""
    global _bad_ids
    with _lock:
        _bad_ids = []
