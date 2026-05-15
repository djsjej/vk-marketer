"""Регрессионный тест что персоны команды агентов корректно загружаются."""

from src.agents import (
    ALINA_PERSONA,
    ALL_PERSONAS,
    BOBA_PERSONA,
    CLAUDE_DEVELOPER_ROLE,
    KIRILL_PERSONA,
    RITA_PERSONA,
    TIMUR_PERSONA,
)


def test_personas_are_not_empty():
    """Все персоны загружены и непустые."""
    personas = [
        BOBA_PERSONA,
        RITA_PERSONA,
        KIRILL_PERSONA,
        TIMUR_PERSONA,
        ALINA_PERSONA,
        CLAUDE_DEVELOPER_ROLE,
    ]
    for persona in personas:
        assert isinstance(persona, str)
        assert len(persona) > 500, "Persona слишком короткая — пустая или обрезана"


def test_personas_have_required_sections():
    """Каждая персона имеет имя и описание ролей в команде."""
    # Боба — упоминает свою специализацию
    assert "стратег" in BOBA_PERSONA.lower() or "стратеги" in BOBA_PERSONA.lower()
    assert "vk" in BOBA_PERSONA.lower()

    # Рита — аналитик ЦА
    assert "сегмент" in RITA_PERSONA.lower()
    assert "аудитори" in RITA_PERSONA.lower()

    # Кирилл — креативщик
    assert "креатив" in KIRILL_PERSONA.lower()
    assert "20%" in KIRILL_PERSONA  # правила VK Ads про текст

    # Тимур — таргетолог
    assert "ставк" in TIMUR_PERSONA.lower() or "бюджет" in TIMUR_PERSONA.lower()
    assert "ctr" in TIMUR_PERSONA.lower() or "cpc" in TIMUR_PERSONA.lower()

    # Алина — аналитик
    assert "отчёт" in ALINA_PERSONA.lower() or "отчет" in ALINA_PERSONA.lower()
    assert "база знаний" in ALINA_PERSONA.lower()


def test_all_personas_dict():
    """ALL_PERSONAS содержит все 5 бизнес-агентов."""
    assert set(ALL_PERSONAS.keys()) == {"boba", "rita", "kirill", "timur", "alina"}
    for name, persona in ALL_PERSONAS.items():
        assert isinstance(persona, str)
        assert len(persona) > 500


def test_boba_has_stop_rules():
    """Боба имеет стоп-правила (критически важно — он CMO)."""
    boba_lower = BOBA_PERSONA.lower()
    # Должен упомянуть запрет на бан/слив бюджета/этику
    assert "бан" in boba_lower or "правил" in boba_lower
    assert "бюджет" in boba_lower
    assert "этик" in boba_lower or "правосл" in boba_lower


def test_kirill_knows_vk_ads_image_rules():
    """Кирилл знает критичные правила VK Ads по изображениям."""
    # Правило 20% текста — обязательно (мы это проверяли на 15.05.2026)
    assert "20%" in KIRILL_PERSONA
    # Без эмодзи
    assert "эмодзи" in KIRILL_PERSONA.lower()
    # Без агрессивной цветокоррекции
    assert "цветокоррекци" in KIRILL_PERSONA.lower() or "фильтр" in KIRILL_PERSONA.lower()


def test_claude_developer_role_clear():
    """Описание моей роли явно отделяет разработку от рекламной стратегии."""
    claude_lower = CLAUDE_DEVELOPER_ROLE.lower()
    assert "разработчик" in claude_lower or "разработ" in claude_lower
    assert "vizit" in claude_lower.lower() or "viziт" in claude_lower
    # Явное правило — не подменять собой Бобу
    assert "боба" in claude_lower or "стратег" in claude_lower
