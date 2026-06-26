"""Тесты авто-сетки тестов (Шаг 2 продукта).

«Бот сам определяет количество тестов на все возрасты на минимальных
бюджетах под дневной потолок» — из видения Vizit'а. Ядро — чистая
функция plan_test_grid; плюс команда /launch_auto и обработчик фото.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.ad_creator import (
    MIN_TEST_BUDGET_RUB,
    TestGridPlan,
    plan_test_grid,
)

AGES_4 = [(36, 40), (41, 46), (47, 52), (53, 58)]


# ---- ядро: plan_test_grid ----


def test_grid_full_at_6000_gives_20():
    """При 300₽/тест: 6000₽ / 300 = 20 кампаний / 4 возраста = 5 текстов → 20."""
    plan = plan_test_grid(max_daily_spend_rub=6000, ages=AGES_4)
    assert plan.per_campaign_rub == 300
    assert plan.variants == 5
    assert plan.total_campaigns == 20
    assert plan.total_cost_rub == 6000
    assert plan.full_age_coverage is True


def test_grid_scales_down_with_lower_cap():
    """2400₽ → 8 кампаний / 4 = 2 текста → 8 тестов = 2400₽."""
    plan = plan_test_grid(max_daily_spend_rub=2400, ages=AGES_4)
    assert plan.variants == 2
    assert plan.total_campaigns == 8
    assert plan.total_cost_rub == 2400
    assert plan.full_age_coverage is True


def test_grid_one_variant_per_age_at_1200():
    """1200₽ → 4 кампании / 4 = 1 текст → 4 теста (по одному на возраст)."""
    plan = plan_test_grid(max_daily_spend_rub=1200, ages=AGES_4)
    assert plan.variants == 1
    assert plan.total_campaigns == 4
    assert plan.full_age_coverage is True


def test_grid_partial_age_coverage_when_budget_too_small():
    """1000₽ → 3 кампании (1000//300), на 4 возраста не хватает → 3, флаг False."""
    plan = plan_test_grid(max_daily_spend_rub=1000, ages=AGES_4)
    assert plan.variants == 1
    assert plan.total_campaigns == 3
    assert plan.full_age_coverage is False
    assert "из 4" in plan.note


def test_grid_caps_variants_at_max():
    """Большой лимит не делает >5 текстов на возраст (max_variants)."""
    plan = plan_test_grid(max_daily_spend_rub=100000, ages=AGES_4)
    assert plan.variants == 5
    assert plan.total_campaigns == 20


def test_grid_respects_custom_min_budget_and_days():
    plan = plan_test_grid(
        max_daily_spend_rub=2000, ages=AGES_4, per_campaign_rub=200, days=2
    )
    # 2000/200 = 10 кампаний / 4 = 2 текста → 8 тестов
    assert plan.per_campaign_rub == 200
    assert plan.total_campaigns == 8
    assert plan.total_cost_rub == 8 * 200 * 2


def test_grid_raises_when_cap_below_one_test():
    """Ниже 300₽ (рекомендованный минимум) — нельзя даже один тест."""
    with pytest.raises(ValueError):
        plan_test_grid(max_daily_spend_rub=200, ages=AGES_4)


def test_min_test_budget_constant():
    assert MIN_TEST_BUDGET_RUB == 300


# ---- команда /launch_auto ----


@pytest.mark.asyncio
async def test_launch_auto_without_theme_shows_usage():
    from src.telegram_bot.handlers import launch_auto_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []
    context.user_data = {}

    with patch("src.telegram_bot.handlers.settings") as s:
        s.vk_audience_segment_ids_parsed = [80507749]
        await launch_auto_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "тему" in update.message.reply_text.await_args.args[0].lower()
    assert context.user_data.get("launch_auto_mode") is not True


@pytest.mark.asyncio
async def test_launch_auto_rejects_without_segment():
    from src.telegram_bot.handlers import launch_auto_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["молитва"]
    context.user_data = {}

    with patch("src.telegram_bot.handlers.settings") as s:
        s.vk_audience_segment_ids_parsed = []
        await launch_auto_command(update, context)

    assert "VK_AUDIENCE_SEGMENT_IDS" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_launch_auto_with_theme_sets_mode_and_plan():
    from src.telegram_bot.handlers import launch_auto_command
    from src.services.ad_creator import AdCopy

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["молитва", "о", "семье"]
    context.user_data = {}

    fake_copies = [AdCopy(title="t", text="x", about="a", cta="write")] * 5
    with patch("src.telegram_bot.handlers.settings") as s, patch(
        "src.telegram_bot.handlers._generate_auto_copies",
        AsyncMock(return_value=fake_copies),
    ):
        s.vk_audience_segment_ids_parsed = [80507749]
        s.max_daily_spend_rub = 6000  # при 300₽/тест даёт полные 20 тестов
        await launch_auto_command(update, context)

    assert context.user_data["launch_auto_mode"] is True
    plan = context.user_data["launch_auto_plan"]
    assert isinstance(plan, TestGridPlan)
    assert plan.total_campaigns == 20
    # В сообщении видно число тестов
    assert "20" in update.message.reply_text.await_args.args[0]


# ---- обработчик фото launch_auto ----


@pytest.mark.asyncio
async def test_handle_launch_auto_photo_not_in_mode_returns_false():
    from src.telegram_bot.handlers import handle_launch_auto_photo

    update = MagicMock()
    context = MagicMock()
    context.user_data = {}
    assert await handle_launch_auto_photo(update, context) is False


@pytest.mark.asyncio
async def test_handle_launch_auto_photo_launches_grid_at_min_budget():
    from src.telegram_bot.handlers import handle_launch_auto_photo
    from src.services.ad_creator import AdCopy

    plan = plan_test_grid(max_daily_spend_rub=2400, ages=AGES_4)  # 8 тестов, 300₽
    copies = [AdCopy(title="t", text="x", about="a", cta="write")] * plan.variants

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    photo = MagicMock()
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"jpegbytes"))
    photo.get_file = AsyncMock(return_value=tg_file)
    update.message.photo = [photo]

    context = MagicMock()
    context.user_data = {
        "launch_auto_mode": True,
        "launch_auto_plan": plan,
        "launch_auto_copies": copies,
    }

    fake_client = MagicMock()
    fake_creator = MagicMock()
    summaries = [MagicMock(ad_plan_id=i) for i in range(plan.total_campaigns)]
    fake_creator.create_batch_campaigns = AsyncMock(return_value=summaries)

    with patch(
        "src.vk_ads.client.VKAdsClient.from_settings", return_value=fake_client
    ), patch("src.telegram_bot.handlers.AdCreator", return_value=fake_creator), patch(
        "src.config.settings"
    ) as s:
        s.vk_community_url = "https://vk.com/pomolimsy"
        result = await handle_launch_auto_photo(update, context)

    assert result is True
    fake_creator.create_batch_campaigns.assert_awaited_once()
    kwargs = fake_creator.create_batch_campaigns.await_args.kwargs
    # Минимальный бюджет и 1 день
    assert kwargs["daily_budget_rub_per_campaign"] == MIN_TEST_BUDGET_RUB
    assert kwargs["days_duration"] == 1
    # Сетка = все 4 возраста
    assert len(kwargs["images_by_age"]) == 4
    # Режим сброшен
    assert context.user_data["launch_auto_mode"] is False


# ---- рука Тимура: Боба готовит запуск (launch_ads) ----


def test_launch_ads_tool_registered():
    """Инструмент Тимура есть в схеме Бобы."""
    from src.agents.boba_tools import BOBA_TOOLS_SCHEMA

    assert "launch_ads" in {t["name"] for t in BOBA_TOOLS_SCHEMA}


@pytest.mark.asyncio
async def test_boba_setup_launch_arms_photo_mode():
    """_boba_setup_launch считает сетку, пишет тексты и включает режим фото."""
    from src.telegram_bot.handlers import _boba_setup_launch
    from src.services.ad_creator import AdCopy

    context = MagicMock()
    context.user_data = {}

    fake_copies = [AdCopy(title="t", text="x", about="a", cta="write")] * 2
    with patch("src.telegram_bot.handlers.settings") as s, patch(
        "src.telegram_bot.handlers._generate_auto_copies",
        AsyncMock(return_value=fake_copies),
    ):
        s.vk_audience_segment_ids_parsed = [80507749]
        s.max_daily_spend_rub = 2400  # 8 тестов по 300₽
        msg = await _boba_setup_launch(context, "о здравии близких")

    assert context.user_data["launch_auto_mode"] is True
    assert context.user_data["launch_auto_theme"] == "о здравии близких"
    assert context.user_data["launch_auto_plan"].total_campaigns == 8
    assert "фото" in msg.lower()


@pytest.mark.asyncio
async def test_boba_setup_launch_blocks_without_segment():
    from src.telegram_bot.handlers import _boba_setup_launch

    context = MagicMock()
    context.user_data = {}
    with patch("src.telegram_bot.handlers.settings") as s:
        s.vk_audience_segment_ids_parsed = []
        msg = await _boba_setup_launch(context, "тема")

    assert "VK_AUDIENCE_SEGMENT_IDS" in msg
    assert context.user_data.get("launch_auto_mode") is not True
