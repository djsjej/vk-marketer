"""Тесты VK Ads клиента (read-методы)."""

import httpx
import pytest
import respx

from src.vk_ads.auth import OAUTH_URL, VKAdsAuthenticator
from src.vk_ads.client import VK_API_BASE, VKAdsAPIError, VKAdsClient


@pytest.fixture
def mock_oauth():
    """Мок OAuth-эндпоинта, всегда возвращает рабочий токен."""
    with respx.mock:
        respx.post(OAUTH_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "test_token", "expires_in": 86400}
            )
        )
        yield


def test_client_requires_auth_or_static():
    with pytest.raises(ValueError):
        VKAdsClient()  # ни того, ни другого


def test_client_static_token_mode():
    client = VKAdsClient(static_token="abc123", account_id=42)
    assert client.static_token == "abc123"
    assert client.auth is None
    assert client.account_id == 42


def test_client_oauth_mode():
    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    client = VKAdsClient(authenticator=auth, account_id=42)
    assert client.auth is auth
    assert client.static_token is None


@pytest.mark.asyncio
@respx.mock
async def test_get_account_info_success():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get(f"{VK_API_BASE}/users/current.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 25416756,
                "first_name": "Александр",
                "client_info": {"balance": 2083.5, "type": "individual"},
            },
        )
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    client = VKAdsClient(authenticator=auth)
    info = await client.get_account_info()

    assert info["id"] == 25416756
    assert info["client_info"]["balance"] == 2083.5


@pytest.mark.asyncio
@respx.mock
async def test_get_balance_returns_none_when_no_agency_endpoint():
    """Direct cabinet — нет /agency/clients.json, get_balance возвращает None."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get(f"{VK_API_BASE}/agency/clients.json").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s"),
        account_id=42,
    )
    assert await client.get_balance() is None


@pytest.mark.asyncio
@respx.mock
async def test_get_balance_extracts_from_agency_clients():
    """Агентский кабинет — /agency/clients.json возвращает items[].account_balance."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get(f"{VK_API_BASE}/agency/clients.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"id": 1, "name": "Client", "account_balance": 1500.0}
                ]
            },
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s"),
        account_id=42,
    )
    assert await client.get_balance() == 1500.0


@pytest.mark.asyncio
@respx.mock
async def test_get_campaigns_uses_ad_plans_endpoint():
    """Новый кабинет: get_campaigns должен ходить в /ad_plans.json, не в /campaigns.json."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 2,
                "items": [
                    {"id": 1, "name": "Спиридон", "status": "active"},
                    {"id": 2, "name": "Зеленецкий", "status": "blocked"},
                ],
            },
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    campaigns = await client.get_campaigns()
    assert len(campaigns) == 2
    assert campaigns[0]["name"] == "Спиридон"


@pytest.mark.asyncio
@respx.mock
async def test_get_campaigns_handles_empty_response():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(200, json={"count": 0, "items": []})
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    assert await client.get_campaigns() == []


@pytest.mark.asyncio
@respx.mock
async def test_get_ad_groups_uses_correct_endpoint():
    """get_ad_groups должен ходить в /ad_groups.json."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get(f"{VK_API_BASE}/ad_groups.json").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 100, "name": "g1", "ad_plan_id": 1}]}
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    groups = await client.get_ad_groups()
    assert len(groups) == 1
    assert groups[0]["id"] == 100


@pytest.mark.asyncio
@respx.mock
async def test_request_raises_on_4xx():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get(f"{VK_API_BASE}/users/current.json").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    with pytest.raises(VKAdsAPIError, match="403"):
        await client.get_account_info()


@pytest.mark.asyncio
@respx.mock
async def test_request_retries_on_401_with_oauth():
    """При 401 клиент должен сбросить токен и повторить запрос."""
    respx.post(OAUTH_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={"access_token": "old_token", "expires_in": 86400}
            ),
            httpx.Response(
                200, json={"access_token": "new_token", "expires_in": 86400}
            ),
        ]
    )
    user_route = respx.get(f"{VK_API_BASE}/users/current.json").mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json={"id": 1, "client_info": {"balance": 100}}),
        ]
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    info = await client.get_account_info()
    assert info["id"] == 1
    assert user_route.call_count == 2  # был ретрай


@pytest.mark.asyncio
@respx.mock
async def test_get_campaign_stats_with_empty_ids_returns_empty():
    client = VKAdsClient(static_token="x")
    result = await client.get_campaign_stats([], "2026-05-01", "2026-05-09")
    assert result == {"items": []}


@pytest.mark.asyncio
@respx.mock
async def test_get_campaign_stats_builds_correct_url():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    route = respx.get(f"{VK_API_BASE}/statistics/ad_plans/day.json").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    await client.get_campaign_stats([1, 2, 3], "2026-05-01", "2026-05-09")
    assert route.call_count == 1
    request = route.calls[0].request
    assert "id=1%2C2%2C3" in str(request.url) or "id=1,2,3" in str(request.url)
