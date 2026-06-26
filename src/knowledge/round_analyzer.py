"""Анализ прошлого раунда и предложение следующего (Шаг C автономии).

Замыкает петлю обучения: читает базу знаний (рабочие/провальные связки,
которые Сторож записал на Шаге B), считает итоги и формулирует, что делать
дальше — перезапустить победителей, отбросить провалы. Каждый цикл
организация умнее, CPL падает.

Безопасно: только чтение. Сам запуск денег — через Бобу с подтверждением
(launch_ads), не отсюда.
"""

from __future__ import annotations

import logging
import re

from src.knowledge import recorder

logger = logging.getLogger(__name__)


def _parse_entries(file_name: str, head: str) -> list[dict]:
    """Парсит записи вида '## {head}: <name> (id N)' с CPL из метрик."""
    # Через модуль, чтобы патч recorder.KNOWLEDGE_BASE_DIR в тестах доходил.
    path = recorder.KNOWLEDGE_BASE_DIR / file_name
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []

    entries: list[dict] = []
    # Каждая запись: заголовок + строки до следующего '## '
    for block in re.split(r"\n## ", text):
        if not block.startswith(f"{head}:") and not block.startswith(f"## {head}:"):
            continue
        m_name = re.search(rf"{head}:\s*(.+?)\s*\(id\s*(\d+)\)", block)
        if not m_name:
            continue
        name, cid = m_name.group(1), int(m_name.group(2))
        m_cpl = re.search(r"CPL\s*(\d+)", block)
        cpl = int(m_cpl.group(1)) if m_cpl else None
        entries.append({"name": name, "id": cid, "cpl": cpl})
    return entries


def summarize_knowledge() -> dict:
    """Итоги накопленного опыта + рекомендация следующего раунда."""
    winners = _parse_entries("working_combos.md", "Связка")
    failures = _parse_entries("failed_combos.md", "Провал")

    winners_sorted = sorted(
        winners, key=lambda w: (w["cpl"] is None, w["cpl"] or 1e9)
    )

    if winners_sorted:
        top = winners_sorted[:3]
        top_str = ", ".join(
            f"«{w['name']}»" + (f" (CPL {w['cpl']}₽)" if w["cpl"] else "") for w in top
        )
        recommendation = (
            f"Есть победители ({len(winners)}). Лучшие: {top_str}. "
            f"Следующий раунд — масштабировать их и отбросить провалы. "
            f"Скажи Бобе «запускай раунд по победителям»."
        )
    elif failures:
        recommendation = (
            f"Пока только провалы ({len(failures)}) — рабочих связок нет. "
            f"Меняем креатив/сегмент, не масштабируем. Скажи Бобе, что пробуем дальше."
        )
    else:
        recommendation = "Данных пока нет — нужен первый завершённый тест."

    return {
        "winners": winners_sorted,
        "failures": failures,
        "recommendation": recommendation,
    }
