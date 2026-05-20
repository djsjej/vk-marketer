"""Structural regression test for /launch_20.

Защищает от случайного редактирования констант, которое может разломать
запуск 20 кампаний.
"""

from src.telegram_bot.handlers import (
    LAUNCH_20_AGE_ORDER,
    LAUNCH_20_COPIES,
    LAUNCH_20_PHOTO_HINTS,
)


def test_launch_20_yields_exactly_twenty_combinations():
    """5 текстов × 4 возраста = 20 кампаний."""
    assert len(LAUNCH_20_COPIES) == 5
    assert len(LAUNCH_20_AGE_ORDER) == 4
    assert len(LAUNCH_20_COPIES) * len(LAUNCH_20_AGE_ORDER) == 20


def test_launch_20_ages_are_women_only_without_men_segment():
    """После отмены мужского сегмента 17.05.2026 — только женские возраста.
    Шаг 5 лет (36-40, 41-46, 47-52, 53-58)."""
    expected = [(36, 40), (41, 46), (47, 52), (53, 58)]
    assert LAUNCH_20_AGE_ORDER == expected


def test_launch_20_copies_have_required_fields():
    """Каждый текст должен иметь title, text, about — иначе VK API упадёт."""
    for copy in LAUNCH_20_COPIES:
        assert "title" in copy, "title обязателен"
        assert "text" in copy, "text обязателен"
        assert "about" in copy, "about обязателен"
        # VK ограничения
        assert len(copy["title"]) <= 40, f"title > 40: {copy['title']!r}"
        assert len(copy["about"]) <= 115, f"about > 115: {copy['about']!r}"


def test_launch_20_copies_avoid_grief_triggers():
    """После провала прошлого батча — никаких триггеров боли/смерти/трагедии
    в заголовках. Защита от регрессии (например, копипаст из BATCH_COPIES_53_58
    обратно в LAUNCH_20_COPIES)."""
    forbidden_in_title = ["больном", "ушедших", "покойн", "тяжело", "память"]
    for copy in LAUNCH_20_COPIES:
        title_lower = copy["title"].lower()
        for word in forbidden_in_title:
            assert word not in title_lower, (
                f"Запрещённый триггер '{word}' в заголовке: {copy['title']!r}. "
                f"После 17.05.2026 заголовки только в зоне тепла."
            )


def test_launch_20_photo_hints_cover_all_ages():
    """Подсказки про фото должны быть для каждого из 4 возрастов."""
    for age in LAUNCH_20_AGE_ORDER:
        assert age in LAUNCH_20_PHOTO_HINTS, (
            f"Нет подсказки фото для возраста {age}"
        )
