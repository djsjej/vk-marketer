"""Тесты Phase 5.10 — создание Segment (Аудитории) из RemarketingUsersList.

VK Ads разделяет на 2 объекта: users_list (только ID) и segment
(используется в targetings кампании). При первом запуске smoke test
16.05.2026 мы ошибочно передавали users_list_id в targetings.segments,
получили unallowed_value. Phase 5.10 это чинит.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx


VK_API_BASE = "https://ads.vk.com"
OAUTH_URL = f"{VK_API_BASE}/api/v2/oauth2/token.json"


def _mock_token(respx_mock):
    """Мок OAuth ответа чтобы _get_token() сработал."""
    respx_mock.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "test_token", "expires_in": 86400},
        )
    )


# ============================================================
# VKAdsClient.create_audience_segment_from_users_list
# ============================================================


@pytest.mark.asyncio
@respx.mock
async def test_create_segment_sends_correct_payload():
    """POST /segments.json получает правильный payload с relations."""
    from src.vk_ads.client import VKAdsAuthenticator, VKAdsClient

    _mock_token(respx.mock)
    route = respx.post(
        f"{VK_API_BASE}/api/v2/remarketing/segments.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "created": "2026-05-16 03:00:00",
                "updated": "2026-05-16 03:00:00",
                "name": "TargetHunter православные",
                "pass_condition": 1,
            },
        )
    )

    authenticator = VKAdsAuthenticator(
        client_id="cid", client_secret="csec"
    )
    client = VKAdsClient(authenticator=authenticator)

    result = await client.create_audience_segment_from_users_list(
        name="TargetHunter православные",
        users_list_id=80507749,
    )

    # Проверяем что POST был сделан с правильным payload
    assert route.called
    request = route.calls[0].request
    import json
    body = json.loads(request.read())

    assert body["name"] == "TargetHunter православные"
    assert body["pass_condition"] == 1
    assert len(body["relations"]) == 1
    relation = body["relations"][0]
    assert relation["object_type"] == "remarketing_users_list"
    assert relation["params"]["source_id"] == 80507749
    assert relation["params"]["type"] == "positive"

    # Проверяем возвращаемый результат
    assert result["id"] == 12345
    assert result["name"] == "TargetHunter православные"
    assert result["pass_condition"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_create_segment_raises_on_400():
    """При HTTP 400 — VKAdsAPIError."""
    from src.vk_ads.client import VKAdsAPIError, VKAdsAuthenticator, VKAdsClient

    _mock_token(respx.mock)
    respx.post(
        f"{VK_API_BASE}/api/v2/remarketing/segments.json"
    ).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "validation_failed",
                    "fields": {
                        "source_id": {
                            "code": "unknown_source",
                            "message": "Unknown remarketing_users_list",
                        }
                    },
                }
            },
        )
    )

    authenticator = VKAdsAuthenticator(
        client_id="cid", client_secret="csec"
    )
    client = VKAdsClient(authenticator=authenticator)

    with pytest.raises(VKAdsAPIError, match="HTTP 400"):
        await client.create_audience_segment_from_users_list(
            name="test",
            users_list_id=999999,
        )


@pytest.mark.asyncio
@respx.mock
async def test_create_segment_raises_if_no_id_in_response():
    """Если VK вернул ответ без id — ошибка."""
    from src.vk_ads.client import VKAdsAPIError, VKAdsAuthenticator, VKAdsClient

    _mock_token(respx.mock)
    respx.post(
        f"{VK_API_BASE}/api/v2/remarketing/segments.json"
    ).mock(
        return_value=httpx.Response(200, json={"name": "no_id"})
    )

    authenticator = VKAdsAuthenticator(
        client_id="cid", client_secret="csec"
    )
    client = VKAdsClient(authenticator=authenticator)

    with pytest.raises(VKAdsAPIError, match="без id"):
        await client.create_audience_segment_from_users_list(
            name="test",
            users_list_id=80507749,
        )


# ============================================================
# Команда /audience_from_list
# ============================================================


@pytest.mark.asyncio
async def test_audience_from_list_no_args_shows_help():
    """Без аргументов — показ help."""
    from src.telegram_bot.handlers import audience_from_list_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []

    await audience_from_list_command(update, context)

    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "Использование" in args[0]


@pytest.mark.asyncio
async def test_audience_from_list_invalid_id():
    """Если первый аргумент не число — ошибка."""
    from src.telegram_bot.handlers import audience_from_list_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["not_a_number"]

    await audience_from_list_command(update, context)

    update.message.reply_text.assert_called_once()
    args, kwargs = update.message.reply_text.call_args
    assert "должен быть числом" in args[0]


@pytest.mark.asyncio
async def test_audience_from_list_creates_segment():
    """Полный flow: команда → создание segment → возврат ID."""
    from src.telegram_bot.handlers import audience_from_list_command

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = ["80507749", "TargetHunter", "православные"]

    mock_client = MagicMock()
    mock_client.create_audience_segment_from_users_list = AsyncMock(
        return_value={
            "id": 12345,
            "name": "TargetHunter православные",
            "pass_condition": 1,
        }
    )

    with patch("src.vk_ads.client.VKAdsClient.from_settings", return_value=mock_client):
        await audience_from_list_command(update, context)

    # Должно быть 2 сообщения: "создаю..." и "готово, id=12345"
    assert update.message.reply_text.call_count == 2

    # Второе сообщение должно содержать новый segment_id
    last_call = update.message.reply_text.call_args_list[-1]
    assert "12345" in last_call[0][0]
    assert "VK_AUDIENCE_SEGMENT_IDS=12345" in last_call[0][0]
