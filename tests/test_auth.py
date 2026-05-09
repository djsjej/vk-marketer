"""Тесты OAuth-аутентификатора VK Ads."""

import time

import httpx
import pytest
import respx

from src.vk_ads.auth import (
    OAUTH_URL,
    REFRESH_BEFORE_EXPIRY_SEC,
    TokenInfo,
    VKAdsAuthenticator,
    VKAdsAuthError,
)


def test_token_info_not_expired_when_fresh():
    token = TokenInfo(access_token="abc", expires_at=time.time() + 7200)
    assert not token.is_expired


def test_token_info_expired_when_past_expiry():
    token = TokenInfo(access_token="abc", expires_at=time.time() - 100)
    assert token.is_expired


def test_token_info_expired_within_refresh_window():
    """Токен считается истёкшим за 5 минут до истечения."""
    token = TokenInfo(
        access_token="abc",
        expires_at=time.time() + REFRESH_BEFORE_EXPIRY_SEC - 10,
    )
    assert token.is_expired


def test_authenticator_rejects_empty_credentials():
    with pytest.raises(ValueError):
        VKAdsAuthenticator(client_id="", client_secret="secret")
    with pytest.raises(ValueError):
        VKAdsAuthenticator(client_id="id", client_secret="")


@pytest.mark.asyncio
@respx.mock
async def test_authenticator_fetches_token_on_first_call():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh_token_xyz",
                "expires_in": 86400,
                "refresh_token": "refresh_xyz",
            },
        )
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    token = await auth.get_access_token()
    assert token == "fresh_token_xyz"


@pytest.mark.asyncio
@respx.mock
async def test_authenticator_caches_token_between_calls():
    """Второй вызов get_access_token не должен делать новый HTTP-запрос."""
    route = respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "cached_token", "expires_in": 86400}
        )
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    token1 = await auth.get_access_token()
    token2 = await auth.get_access_token()

    assert token1 == token2 == "cached_token"
    assert route.call_count == 1  # только один HTTP-запрос


@pytest.mark.asyncio
@respx.mock
async def test_authenticator_refreshes_when_expired():
    """Если токен истёк, должен сделать новый запрос."""
    respx.post(OAUTH_URL).mock(
        side_effect=[
            httpx.Response(
                200, json={"access_token": "first", "expires_in": 1}
            ),
            httpx.Response(
                200, json={"access_token": "second", "expires_in": 86400}
            ),
        ]
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    token1 = await auth.get_access_token()
    assert token1 == "first"

    # Принудительно делаем токен истёкшим
    auth._token.expires_at = time.time() - 1

    token2 = await auth.get_access_token()
    assert token2 == "second"


@pytest.mark.asyncio
@respx.mock
async def test_authenticator_raises_on_error_status():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="bad")
    with pytest.raises(VKAdsAuthError, match="401"):
        await auth.get_access_token()


@pytest.mark.asyncio
@respx.mock
async def test_authenticator_raises_when_no_token_in_response():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json={"some_other_field": "x"})
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    with pytest.raises(VKAdsAuthError, match="access_token"):
        await auth.get_access_token()


@pytest.mark.asyncio
@respx.mock
async def test_authenticator_invalidate_forces_refresh():
    respx.post(OAUTH_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "first", "expires_in": 86400}),
            httpx.Response(200, json={"access_token": "second", "expires_in": 86400}),
        ]
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    assert await auth.get_access_token() == "first"
    auth.invalidate()
    assert await auth.get_access_token() == "second"


@pytest.mark.asyncio
@respx.mock
async def test_authenticator_sends_correct_oauth_params():
    """Проверяем, что в POST уходят правильные form-data поля."""
    route = respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ok", "expires_in": 86400}
        )
    )

    auth = VKAdsAuthenticator(client_id="my_id", client_secret="my_secret")
    await auth.get_access_token()

    request = route.calls[0].request
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=my_id" in body
    assert "client_secret=my_secret" in body
