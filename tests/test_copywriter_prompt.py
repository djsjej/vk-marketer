"""Тест-страж: копирайтер заякорен на православную нишу (регресс 26.06.2026).

Регресс: на расплывчатой подписи копирайтер уходил в мирской маркетинг
(онлайн-курсы, «сообщество 47000», кликбейт). Причина — «лазейка» в
промпте «если тема светская — обычный тон». Этот тест фиксирует, что
лазейки больше нет и промпт жёстко православный.
"""

from __future__ import annotations

from src.claude_brain.copywriter import (
    COPYWRITER_SYSTEM_PROMPT_SINGLE,
    COPYWRITER_SYSTEM_PROMPT_VARIANTS,
)

PROMPTS = [COPYWRITER_SYSTEM_PROMPT_SINGLE, COPYWRITER_SYSTEM_PROMPT_VARIANTS]


def test_prompts_are_orthodox_anchored():
    for p in PROMPTS:
        low = p.lower()
        assert "православ" in low
        # Главное действие — написать имена для поминовения
        assert "имена" in low
        assert "написать" in low


def test_prompts_have_no_secular_escape():
    """Не должно остаться «лазейки» в мирской маркетинг."""
    for p in PROMPTS:
        assert "тема светская" not in p
        assert "обычный рекламный тон" not in p


def test_prompts_forbid_mirskie_klishe():
    """Промпт явно запрещает мирские клише и кликбейт (источник регресса)."""
    for p in PROMPTS:
        low = p.lower()
        assert "клише" in low
        # Якоря-запреты, по которым модель уходила в сторону
        assert "курс" in low  # упомянут как запрет
        assert "кликбейт" in low


def test_default_cta_is_write():
    for p in PROMPTS:
        assert '"write"' in p or "write" in p
