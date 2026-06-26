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


def test_grid_two_images_at_2400():
    """2400₽ → 8 кампаний: 2 картинки × 4 возраста × 1 текст = 8."""
    plan = plan_test_grid(max_daily_spend_rub=2400, ages=AGES_4)
    assert plan.per_campaign_rub == 300
    assert plan.images == 2
    assert plan.variants == 1
    assert plan.total_campaigns == 8
    assert plan.total_cost_rub == 2400
    assert plan.full_age_coverage is True


def test_grid_four_images_at_4800():
    """4800₽ → 16 кампаний: 4 картинки × 4 возраста × 1 текст."""
    plan = plan_test_grid(max_daily_spend_rub=4800, ages=AGES_4)
    assert plan.images == 4
    assert plan.total_campaigns == 16


def test_grid_one_image_at_1200():
    """1200₽ → 4 кампании: 1 картинка × 4 возраста × 1 текст."""
    plan = plan_test_grid(max_daily_spend_rub=1200, ages=AGES_4)
    assert plan.images == 1
    assert plan.variants == 1
    assert plan.total_campaigns == 4
    assert plan.full_age_coverage is True


def test_grid_partial_age_coverage_when_budget_too_small():
    """1000₽ → 3 кампании (1000//300), на 4 возраста не хватает → 3, флаг False."""
    plan = plan_test_grid(max_daily_spend_rub=1000, ages=AGES_4)
    assert plan.variants == 1
    assert plan.images == 1
    assert plan.total_campaigns == 3
    assert plan.full_age_coverage is False
    assert "из 4" in plan.note


def test_grid_caps_images_at_max():
    """Большой лимит не делает >4 картинок (max_images)."""
    plan = plan_test_grid(max_daily_spend_rub=100000, ages=AGES_4)
    assert plan.images == 4


def test_grid_respects_custom_min_budget():
    plan = plan_test_grid(
        max_daily_spend_rub=2400, ages=AGES_4, per_campaign_rub=200
    )
    # 2400/200 = 12 кампаний / 4 = 3 картинки × 4 × 1 = 12
    assert plan.per_campaign_rub == 200
    assert plan.total_campaigns == 12


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
    fake_by_age = {a: fake_copies for a in [(36, 40), (41, 46), (47, 52), (53, 58)]}
    with patch("src.telegram_bot.handlers.settings") as s, patch(
        "src.telegram_bot.handlers._generate_copies_by_age",
        AsyncMock(return_value=fake_by_age),
    ):
        s.vk_audience_segment_ids_parsed = [80507749]
        s.max_daily_spend_rub = 6000  # 4 картинки × 4 возраста × 1 текст = 16
        await launch_auto_command(update, context)

    assert context.user_data["launch_auto_mode"] is True
    plan = context.user_data["launch_auto_plan"]
    assert isinstance(plan, TestGridPlan)
    assert plan.total_campaigns == 16
    assert plan.images == 4
    assert "16" in update.message.reply_text.await_args.args[0]


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

    plan = plan_test_grid(max_daily_spend_rub=1200, ages=AGES_4)  # 1 картинка, 4 теста
    assert plan.images == 1
    copies = [AdCopy(title="t", text="x", about="a", cta="write")] * plan.variants
    copies_by_age = {age: copies for age in plan.ages}

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
        "launch_auto_copies_by_age": copies_by_age,
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
    assert kwargs["daily_budget_rub_per_campaign"] == MIN_TEST_BUDGET_RUB
    assert kwargs["days_duration"] == 1
    assert len(kwargs["images_by_age"]) == 4
    assert context.user_data["launch_auto_mode"] is False


@pytest.mark.asyncio
async def test_handle_launch_auto_photo_collects_multiple_creatives():
    """2 картинки в плане → запуск только после 2-го фото, batch вызван 2 раза."""
    from src.telegram_bot.handlers import handle_launch_auto_photo
    from src.services.ad_creator import AdCopy

    plan = plan_test_grid(max_daily_spend_rub=2400, ages=AGES_4)  # 2 картинки
    assert plan.images == 2
    copies_by_age = {age: [AdCopy(title="t", text="x", about="a", cta="write")] for age in plan.ages}

    def _mk_update():
        u = MagicMock()
        u.message.reply_text = AsyncMock()
        f = MagicMock()
        f.download_as_bytearray = AsyncMock(return_value=bytearray(b"img"))
        p = MagicMock()
        p.get_file = AsyncMock(return_value=f)
        u.message.photo = [p]
        return u

    context = MagicMock()
    context.user_data = {
        "launch_auto_mode": True,
        "launch_auto_plan": plan,
        "launch_auto_copies_by_age": copies_by_age,
    }

    fake_client = MagicMock()
    fake_creator = MagicMock()
    fake_creator.create_batch_campaigns = AsyncMock(return_value=[MagicMock(ad_plan_id=1)])

    with patch(
        "src.vk_ads.client.VKAdsClient.from_settings", return_value=fake_client
    ), patch("src.telegram_bot.handlers.AdCreator", return_value=fake_creator), patch(
        "src.config.settings"
    ) as s:
        s.vk_community_url = "https://vk.com/pomolimsy"
        # 1-е фото — копим, не запускаем
        await handle_launch_auto_photo(_mk_update(), context)
        assert fake_creator.create_batch_campaigns.await_count == 0
        assert context.user_data["launch_auto_mode"] is True
        # 2-е фото — запуск, batch вызван по разу на каждую картинку
        await handle_launch_auto_photo(_mk_update(), context)

    assert fake_creator.create_batch_campaigns.await_count == 2
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
    fake_by_age = {a: fake_copies for a in [(36, 40), (41, 46), (47, 52), (53, 58)]}
    with patch("src.telegram_bot.handlers.settings") as s, patch(
        "src.telegram_bot.handlers._generate_copies_by_age",
        AsyncMock(return_value=fake_by_age),
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


# ---- посегментная генерация: разные тексты под каждый возраст ----


@pytest.mark.asyncio
async def test_copies_by_age_passes_age_hint():
    """_generate_copies_by_age зовёт копирайтер с подсказкой жизненного этапа."""
    from src.telegram_bot.handlers import _generate_copies_by_age
    from src.services.ad_creator import AdCopy

    fake_cw = MagicMock()
    fake_cw.generate_copy_variants = AsyncMock(
        return_value=[AdCopy(title="t", text="x", about="a", cta="write")]
    )
    with patch("src.telegram_bot.handlers.ClaudeCopywriter", return_value=fake_cw):
        result = await _generate_copies_by_age("здравие", [(36, 40), (53, 58)], 1)

    assert set(result.keys()) == {(36, 40), (53, 58)}
    # Для разных возрастов передаётся разный extra_context (хинт этапа)
    hints = {c.kwargs.get("extra_context") for c in fake_cw.generate_copy_variants.await_args_list}
    assert len(hints) == 2  # два разных хинта


@pytest.mark.asyncio
async def test_copies_by_age_fallback_uses_batch_themes():
    """Если Claude упал — берём тематические BATCH_COPIES под возраст."""
    from src.telegram_bot.handlers import _generate_copies_by_age

    fake_cw = MagicMock()
    fake_cw.generate_copy_variants = AsyncMock(side_effect=Exception("Claude down"))
    with patch("src.telegram_bot.handlers.ClaudeCopywriter", return_value=fake_cw):
        result = await _generate_copies_by_age("тема", [(53, 58)], 2)

    # Должны прийти запасные тексты под 53-58 (про упокоение/внуков)
    assert len(result[(53, 58)]) == 2
    assert result[(53, 58)][0].title  # непустые
