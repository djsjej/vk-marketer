"""Тесты Phase 5.9 — smoke test кампания (create_smoke_test_campaign + /launch_test)."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from src.services.ad_creator import AdCopy, AdCreator, AdCreatorError


def _make_tiny_jpeg() -> bytes:
    """Создаёт минимальный валидный JPEG для тестов."""
    img = Image.new("RGB", (100, 100), color=(120, 80, 40))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_smoke_test_rejects_empty_copies():
    """Пустой список AdCopy → AdCreatorError."""
    mock_client = MagicMock()
    creator = AdCreator(mock_client)

    with pytest.raises(AdCreatorError, match="copies пустой"):
        await creator.create_smoke_test_campaign(
            image_bytes=b"fake",
            copies=[],
            community_url="https://vk.com/pomolimsy",
        )


@pytest.mark.asyncio
async def test_smoke_test_rejects_too_many_copies():
    """Больше 4 AdCopy → отказ (бюджет не наберёт статзначимости)."""
    mock_client = MagicMock()
    creator = AdCreator(mock_client)

    copies = [
        AdCopy(title="t", text="x", about="a", cta="write")
        for _ in range(5)
    ]
    with pytest.raises(AdCreatorError, match="Слишком много креативов"):
        await creator.create_smoke_test_campaign(
            image_bytes=b"fake",
            copies=copies,
            community_url="https://vk.com/pomolimsy",
        )


@pytest.mark.asyncio
async def test_smoke_test_rejects_low_budget():
    """Бюджет < 100₽ → отказ (минимум VK)."""
    mock_client = MagicMock()
    creator = AdCreator(mock_client)

    copy = AdCopy(title="t", text="x", about="a", cta="write")
    with pytest.raises(AdCreatorError, match="меньше минимума"):
        await creator.create_smoke_test_campaign(
            image_bytes=b"fake",
            copies=[copy],
            community_url="https://vk.com/pomolimsy",
            daily_budget_rub=50,
        )


@pytest.mark.asyncio
async def test_smoke_test_creates_campaign_with_2_banners():
    """С 2 AdCopy создаётся один ad_group с 2 баннерами для A/B."""
    mock_client = MagicMock()
    mock_client.get_or_register_url = AsyncMock(return_value={"id": 999})
    mock_client.upload_media = AsyncMock(side_effect=[1001, 1002])  # image, icon
    mock_client.create_ad_plan = AsyncMock(return_value={
        "id": 50000,
        "campaigns": [{
            "id": 60000,
            "banners": [
                {"id": 70001},
                {"id": 70002},
            ],
        }],
    })

    creator = AdCreator(mock_client)
    copies = [
        AdCopy(title="A title", text="A text", about="A about", cta="write"),
        AdCopy(title="B title", text="B text", about="B about", cta="write"),
    ]

    result = await creator.create_smoke_test_campaign(
        image_bytes=_make_tiny_jpeg(),
        copies=copies,
        community_url="https://vk.com/pomolimsy",
        daily_budget_rub=500,
        days_duration=3,
    )

    assert result.ad_plan_id == 50000
    assert result.ad_group_ids == [60000]
    assert result.banner_ids == [70001, 70002]

    # Проверяем что payload содержит 2 баннера в одном ad_group
    create_call = mock_client.create_ad_plan.call_args
    payload = create_call[0][0]
    assert len(payload["ad_groups"]) == 1  # одна группа
    assert len(payload["ad_groups"][0]["banners"]) == 2  # два баннера


@pytest.mark.asyncio
async def test_smoke_test_uses_segments_from_settings():
    """Если в settings задан vk_audience_segment_ids — добавляется в targetings."""
    mock_client = MagicMock()
    mock_client.get_or_register_url = AsyncMock(return_value={"id": 999})
    mock_client.upload_media = AsyncMock(side_effect=[1001, 1002])
    mock_client.create_ad_plan = AsyncMock(return_value={
        "id": 50000,
        "campaigns": [{"id": 60000, "banners": [{"id": 70001}]}],
    })

    creator = AdCreator(mock_client)
    copy = AdCopy(title="t", text="x", about="a", cta="write")

    # Мокаем settings.vk_audience_segment_ids_parsed
    with patch("src.config.settings") as mock_settings:
        mock_settings.vk_audience_segment_ids_parsed = [80507749]

        await creator.create_smoke_test_campaign(
            image_bytes=_make_tiny_jpeg(),
            copies=[copy],
            community_url="https://vk.com/pomolimsy",
        )

    create_call = mock_client.create_ad_plan.call_args
    payload = create_call[0][0]
    targetings = payload["ad_groups"][0]["targetings"]
    assert targetings.get("segments") == [80507749]


@pytest.mark.asyncio
async def test_smoke_test_default_age_41_58():
    """По умолчанию age_list 41-58 (наша ЦА)."""
    mock_client = MagicMock()
    mock_client.get_or_register_url = AsyncMock(return_value={"id": 999})
    mock_client.upload_media = AsyncMock(side_effect=[1001, 1002])
    mock_client.create_ad_plan = AsyncMock(return_value={
        "id": 50000,
        "campaigns": [{"id": 60000, "banners": [{"id": 70001}]}],
    })

    creator = AdCreator(mock_client)
    copy = AdCopy(title="t", text="x", about="a", cta="write")

    await creator.create_smoke_test_campaign(
        image_bytes=_make_tiny_jpeg(),
        copies=[copy],
        community_url="https://vk.com/pomolimsy",
    )

    create_call = mock_client.create_ad_plan.call_args
    payload = create_call[0][0]
    age_list = payload["ad_groups"][0]["targetings"]["age"]["age_list"]
    assert age_list == list(range(41, 59))  # 41..58 включительно


@pytest.mark.asyncio
async def test_smoke_test_default_female():
    """По умолчанию sex=['female'] (наша ЦА)."""
    mock_client = MagicMock()
    mock_client.get_or_register_url = AsyncMock(return_value={"id": 999})
    mock_client.upload_media = AsyncMock(side_effect=[1001, 1002])
    mock_client.create_ad_plan = AsyncMock(return_value={
        "id": 50000,
        "campaigns": [{"id": 60000, "banners": [{"id": 70001}]}],
    })

    creator = AdCreator(mock_client)
    copy = AdCopy(title="t", text="x", about="a", cta="write")

    await creator.create_smoke_test_campaign(
        image_bytes=_make_tiny_jpeg(),
        copies=[copy],
        community_url="https://vk.com/pomolimsy",
    )

    create_call = mock_client.create_ad_plan.call_args
    payload = create_call[0][0]
    sex = payload["ad_groups"][0]["targetings"]["sex"]
    assert sex == ["female"]


@pytest.mark.asyncio
async def test_smoke_test_default_package_3127():
    """По умолчанию package_id=3127 (Написать сообщение)."""
    mock_client = MagicMock()
    mock_client.get_or_register_url = AsyncMock(return_value={"id": 999})
    mock_client.upload_media = AsyncMock(side_effect=[1001, 1002])
    mock_client.create_ad_plan = AsyncMock(return_value={
        "id": 50000,
        "campaigns": [{"id": 60000, "banners": [{"id": 70001}]}],
    })

    creator = AdCreator(mock_client)
    copy = AdCopy(title="t", text="x", about="a", cta="write")

    await creator.create_smoke_test_campaign(
        image_bytes=_make_tiny_jpeg(),
        copies=[copy],
        community_url="https://vk.com/pomolimsy",
    )

    create_call = mock_client.create_ad_plan.call_args
    payload = create_call[0][0]
    package_id = payload["ad_groups"][0]["package_id"]
    assert package_id == 3127


# ============================================================
# Тесты команды /launch_test
# ============================================================


@pytest.mark.asyncio
async def test_launch_test_command_rejects_without_segment():
    """Без VK_AUDIENCE_SEGMENT_IDS в env команда не запускается."""
    from src.telegram_bot.handlers import launch_test_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}

    with patch("src.config.settings") as mock_settings:
        mock_settings.vk_audience_segment_ids_parsed = []

        await launch_test_command(update, context)

    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "VK_AUDIENCE_SEGMENT_IDS" in args[0]
    assert context.user_data.get("awaiting_smoke_test_photo") is not True


@pytest.mark.asyncio
async def test_launch_test_command_sets_waiting_flag():
    """С сегментом в env — выставляется флаг ожидания фото."""
    from src.telegram_bot.handlers import launch_test_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}

    with patch("src.config.settings") as mock_settings:
        mock_settings.vk_audience_segment_ids_parsed = [80507749]

        await launch_test_command(update, context)

    assert context.user_data.get("awaiting_smoke_test_photo") is True
    update.message.reply_text.assert_called_once()
