"""Команда ИИ-агентов Рекламной Организации Vizit.

См. Конституцию (загружена 15.05.2026): docs/CONSTITUTION_v1.md

Иерархия:
- Уровень 1: Vizit (оркестратор, человек)
- Уровень 2: Боба (CMO, главный консультант)
- Уровень 3: Рита, Кирилл, Тимур, Алина (специалисты)
- Уровень 3': Биба (разведчик VK Ads API, см. src/biba/)
- Уровень -1: Claude (разработчик, не часть бизнес-команды)
- Уровень 4: База знаний (docs/knowledge_base/, на будущее)
"""

from src.agents.personas import (
    ALINA_PERSONA,
    BOBA_PERSONA,
    CLAUDE_DEVELOPER_ROLE,
    KIRILL_PERSONA,
    RITA_PERSONA,
    TIMUR_PERSONA,
    ALL_PERSONAS,
)

__all__ = [
    "BOBA_PERSONA",
    "RITA_PERSONA",
    "KIRILL_PERSONA",
    "TIMUR_PERSONA",
    "ALINA_PERSONA",
    "CLAUDE_DEVELOPER_ROLE",
    "ALL_PERSONAS",
]
