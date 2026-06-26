"""Тесты планировщика кампании (Шаг 1 продукта).

«Говорю тему → бот даёт рекомендации, какие картинки нужны». Покрываем
парсинг ответа Claude в CampaignBrief, форматирование сообщения и команду
/plan (без темы → подсказка; с темой → бриф).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude_brain.campaign_planner import (
    CampaignBrief,
    CampaignPlanner,
    ImageIdea,
    PlannerError,
    format_brief_message,
)

_GOOD_JSON = json.dumps(
    {
        "analysis": "Бьём на память о близких перед Радоницей",
        "image_ideas": [
            {"description": "Свеча в полумраке храма", "mood": "тихая скорбь", "why": "цепляет память"},
            {"description": "Рассвет над монастырём", "mood": "надежда", "why": "тепло и покой"},
        ],
        "avoid": ["текст на картинке", "яркие фильтры"],
    },
    ensure_ascii=False,
)


def test_parse_good_json():
    brief = CampaignPlanner._parse("Радоница", _GOOD_JSON)
    assert isinstance(brief, CampaignBrief)
    assert brief.theme == "Радоница"
    assert len(brief.image_ideas) == 2
    assert brief.image_ideas[0].description.startswith("Свеча")
    assert "текст на картинке" in brief.avoid


def test_parse_strips_markdown_fence():
    fenced = f"```json\n{_GOOD_JSON}\n```"
    brief = CampaignPlanner._parse("тема", fenced)
    assert len(brief.image_ideas) == 2


def test_parse_rejects_no_ideas():
    bad = json.dumps({"analysis": "x", "image_ideas": [], "avoid": []})
    with pytest.raises(PlannerError):
        CampaignPlanner._parse("тема", bad)


def test_parse_rejects_non_json():
    with pytest.raises(PlannerError):
        CampaignPlanner._parse("тема", "это не json")


@pytest.mark.asyncio
async def test_make_brief_calls_claude():
    planner = CampaignPlanner()
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(text=_GOOD_JSON)]
    planner.client = MagicMock()
    planner.client.messages.create = AsyncMock(return_value=fake_msg)

    brief = await planner.make_brief("Радоница")
    assert len(brief.image_ideas) == 2
    planner.client.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_make_brief_empty_theme_raises():
    planner = CampaignPlanner()
    with pytest.raises(PlannerError):
        await planner.make_brief("   ")


def test_format_brief_message():
    brief = CampaignBrief(
        theme="Радоница",
        analysis="Память о близких",
        image_ideas=[
            ImageIdea(description="Свеча в храме", mood="тишина", why="цепляет"),
        ],
        avoid=["текст на картинке"],
    )
    msg = format_brief_message(brief)
    assert "Радоница" in msg
    assert "Свеча в храме" in msg
    assert "Чего избегать" in msg
    assert "текст на картинке" in msg


# ---- команда /plan ----


@pytest.mark.asyncio
async def test_plan_command_without_theme_shows_usage():
    from src.telegram_bot.handlers import plan_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []

    await plan_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "тему" in update.message.reply_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_plan_command_with_theme_sends_brief():
    from src.telegram_bot.handlers import plan_command

    update = MagicMock()
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=placeholder)
    context = MagicMock()
    context.args = ["поминовение", "на", "Радоницу"]

    brief = CampaignBrief(
        theme="поминовение на Радоницу",
        analysis="разбор",
        image_ideas=[ImageIdea(description="Свеча")],
        avoid=["текст"],
    )
    fake_planner = MagicMock()
    fake_planner.make_brief = AsyncMock(return_value=brief)

    with patch(
        "src.telegram_bot.handlers.CampaignPlanner", return_value=fake_planner
    ):
        await plan_command(update, context)

    fake_planner.make_brief.assert_awaited_once_with("поминовение на Радоницу")
    placeholder.edit_text.assert_awaited_once()
    assert "Свеча" in placeholder.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_plan_command_handles_planner_error():
    from src.telegram_bot.handlers import plan_command

    update = MagicMock()
    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=placeholder)
    context = MagicMock()
    context.args = ["тема"]

    fake_planner = MagicMock()
    fake_planner.make_brief = AsyncMock(side_effect=PlannerError("Claude недоступен"))

    with patch("src.telegram_bot.handlers.CampaignPlanner", return_value=fake_planner):
        await plan_command(update, context)

    # Должны мягко сообщить об ошибке, не упасть
    placeholder.edit_text.assert_awaited_once()
    assert "ещё раз" in placeholder.edit_text.await_args.args[0].lower()
