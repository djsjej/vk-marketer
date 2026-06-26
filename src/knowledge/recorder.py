"""Авто-запись результатов кампаний в базу знаний (Шаг B автономии).

Раньше working_combos.md / failed_combos.md были пустыми шаблонами —
организация ничего не помнила. Теперь Сторож сам фиксирует исход каждой
кампании: дорогую (выключенную) → в провалы, дешёвую → в рабочие связки.
Это топливо для Шага C (новый раунд по победителям).

Дедуп: одна кампания пишется один раз (ищем её id в файле).
Падение записи не роняет вызывающего.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_DIR = Path(__file__).parent.parent.parent / "docs" / "knowledge_base"


def _already_recorded(path: Path, campaign_id: int) -> bool:
    try:
        return f"(id {campaign_id})" in path.read_text(encoding="utf-8")
    except Exception:
        return False


def record_campaign_result(
    *,
    campaign_id: int,
    name: str,
    impressions: int,
    clicks: int,
    spent_rub: float,
    leads: int,
    cpl_rub: float,
    won: bool,
    reason: str = "",
    today: str | None = None,
) -> bool:
    """Дописывает исход кампании в базу знаний. True если записал (False —
    если уже было, файла нет, или ошибка)."""
    file_name = "working_combos.md" if won else "failed_combos.md"
    path = KNOWLEDGE_BASE_DIR / file_name
    if not path.exists():
        logger.warning(f"[recorder] нет файла {path}")
        return False
    if _already_recorded(path, campaign_id):
        return False

    date = today or datetime.now().strftime("%Y-%m-%d")
    sample = "" if clicks >= 100 else " (выборка мала, n<100 кликов — предварительно)"
    head = "Связка" if won else "Провал"
    default_verdict = (
        "CPL в норме — кандидат на масштаб" if won
        else "CPL выше нормы — не масштабировать, переделать креатив/сегмент"
    )
    entry = (
        f"\n\n## {head}: {name} (id {campaign_id})\n"
        f"**Дата:** {date}\n"
        f"**Метрики:** показы {impressions}, клики {clicks}, "
        f"расход {spent_rub:.0f}₽, написавших {leads}, CPL {cpl_rub:.0f}₽{sample}\n"
        f"**Вывод:** {reason or default_verdict}\n"
        f"**Источник:** авто-запись Сторожа (VK Ads статистика)\n"
    )
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(entry)
        logger.info(f"[recorder] записал {'win' if won else 'fail'} кампании {campaign_id}")
        return True
    except Exception:
        logger.exception(f"[recorder] не смог записать {campaign_id}")
        return False
