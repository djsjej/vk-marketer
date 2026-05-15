"""Тесты создания remarketing users lists (Phase 5.4)."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.vk_ads.client import VKAdsAPIError, VKAdsClient


def _make_client_with_static_token() -> VKAdsClient:
    """Создаём клиент с фиксированным токеном — обходим OAuth логику."""
    return VKAdsClient(static_token="test_token_xyz", account_id=12345)


@pytest.mark.asyncio
async def test_create_remarketing_list_requires_min_2000():
    """API требует минимум 2000 ID — проверяем заранее, до отправки."""
    client = _make_client_with_static_token()

    with pytest.raises(ValueError, match="минимум 2000"):
        await client.create_remarketing_users_list(
            name="test",
            user_ids=[1, 2, 3],  # всего 3 — мало
        )


@pytest.mark.asyncio
async def test_create_remarketing_list_rejects_over_5m():
    """API лимит 5 000 000."""
    client = _make_client_with_static_token()

    huge_list = list(range(5_000_001))
    with pytest.raises(ValueError, match="5 000 000"):
        await client.create_remarketing_users_list(name="huge", user_ids=huge_list)


@pytest.mark.asyncio
@respx.mock
async def test_create_remarketing_list_sends_correct_multipart():
    """Запрос идёт как multipart/form-data с полями file, name, type."""
    route = respx.post(
        "https://ads.vk.com/api/v3/remarketing/users_lists.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 2500000,
                "status": "receiving",
                "name": "test",
                "type": "vk",
                "entries_count": 2500,
                "ids_count": 2500,
                "base": 0,
            },
        )
    )

    client = _make_client_with_static_token()
    user_ids = list(range(1, 2501))  # 2500 — выше минимума 2000

    result = await client.create_remarketing_users_list(
        name="Горячая аудитория", user_ids=user_ids
    )

    assert result["id"] == 2500000
    assert result["status"] == "receiving"

    # Проверяем что в запросе был multipart
    request = route.calls[0].request
    content_type = request.headers.get("content-type", "")
    assert "multipart/form-data" in content_type
    assert "boundary=" in content_type

    # Body должно содержать поля name, type, и файл с ID
    body = request.read().decode("utf-8", errors="replace")
    assert 'name="name"' in body
    assert "Горячая аудитория" in body
    assert 'name="type"' in body
    assert "vk" in body
    assert 'name="file"' in body
    # ID должны быть в файле — проверим начало и конец
    assert "1\n" in body  # первый ID
    assert "2500" in body  # последний


@pytest.mark.asyncio
@respx.mock
async def test_create_remarketing_list_passes_authorization_header():
    """Запрос идёт с Authorization: Bearer <token>."""
    route = respx.post(
        "https://ads.vk.com/api/v3/remarketing/users_lists.json"
    ).mock(
        return_value=httpx.Response(
            200, json={"id": 1, "status": "receiving"}
        )
    )

    client = _make_client_with_static_token()
    await client.create_remarketing_users_list(
        name="test", user_ids=list(range(2000))
    )

    auth = route.calls[0].request.headers.get("authorization", "")
    assert auth == "Bearer test_token_xyz"


@pytest.mark.asyncio
@respx.mock
async def test_create_remarketing_list_raises_on_api_error():
    """HTTP не-2xx → VKAdsAPIError."""
    respx.post(
        "https://ads.vk.com/api/v3/remarketing/users_lists.json"
    ).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "fields": {
                        "file": [{"code": "not_enough_ids"}],
                    }
                }
            },
        )
    )

    client = _make_client_with_static_token()
    with pytest.raises(VKAdsAPIError, match="HTTP 400"):
        await client.create_remarketing_users_list(
            name="test", user_ids=list(range(2000))
        )


@pytest.mark.asyncio
@respx.mock
async def test_get_remarketing_list_returns_status():
    """GET для опроса статуса возвращает текущее состояние."""
    respx.get(
        "https://ads.vk.com/api/v3/remarketing/users_lists/12345.json"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 12345,
                "status": "ready",
                "name": "test",
                "entries_count": 2000,
                "matched_ids_count": 1850,
            },
        )
    )

    client = _make_client_with_static_token()
    result = await client.get_remarketing_users_list(12345)

    assert result["status"] == "ready"
    assert result["matched_ids_count"] == 1850
