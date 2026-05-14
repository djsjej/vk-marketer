"""Тесты VK API клиента (Phase 5 — таргетолог).

Проверяют:
- В payload каждого запроса есть access_token и v
- Парсинг новой формы ответа (v5.131+) с groups/profiles
- Парсинг старой формы ответа (массив сразу)
- VKAPIError поднимается на error-объект от VK
- Throttle делает паузу между быстрыми запросами

Источник истины полей: https://dev.vk.com/ru/method/groups.getById
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from src.targetolog.vk_api_client import (
    VK_API_BASE,
    VK_API_VERSION,
    VKAPIClient,
    VKAPIError,
)


@pytest.mark.asyncio
@respx.mock
async def test_groups_get_by_id_returns_group_info():
    """groups.getById возвращает группу из новой формы ответа (groups/profiles)."""
    respx.post(f"{VK_API_BASE}/groups.getById").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "groups": [
                        {
                            "id": 216409501,
                            "name": "Pomolimsy",
                            "screen_name": "pomolimsy",
                            "members_count": 1234,
                            "description": "Православное сообщество",
                            "is_closed": 0,
                        }
                    ],
                    "profiles": [],
                }
            },
        )
    )

    async with VKAPIClient(service_token="dummy") as client:
        group = await client.groups_get_by_id("pomolimsy")

    assert group["id"] == 216409501
    assert group["name"] == "Pomolimsy"
    assert group["members_count"] == 1234
    assert group["screen_name"] == "pomolimsy"


@pytest.mark.asyncio
@respx.mock
async def test_groups_get_by_id_handles_old_response_format():
    """Старая форма ответа (response как массив, без groups/profiles)."""
    respx.post(f"{VK_API_BASE}/groups.getById").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": [
                    {
                        "id": 1,
                        "name": "Test",
                        "screen_name": "test",
                        "members_count": 100,
                    }
                ]
            },
        )
    )

    async with VKAPIClient(service_token="dummy") as client:
        group = await client.groups_get_by_id("test")

    assert group["name"] == "Test"
    assert group["members_count"] == 100


@pytest.mark.asyncio
@respx.mock
async def test_vk_api_error_raised_on_error_response():
    """Когда VK возвращает объект error — поднимаем VKAPIError с кодом и сообщением."""
    respx.post(f"{VK_API_BASE}/groups.getById").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 100,
                    "error_msg": "One of the parameters specified was missing or invalid",
                }
            },
        )
    )

    async with VKAPIClient(service_token="dummy") as client:
        with pytest.raises(VKAPIError) as exc_info:
            await client.groups_get_by_id("nonexistent_group_xyz")

    assert exc_info.value.code == 100
    assert "missing or invalid" in exc_info.value.message


@pytest.mark.asyncio
@respx.mock
async def test_groups_get_by_id_raises_when_empty_response():
    """Если VK вернул пустой список групп — поднимаем VKAPIError(100)."""
    respx.post(f"{VK_API_BASE}/groups.getById").mock(
        return_value=httpx.Response(
            200,
            json={"response": {"groups": [], "profiles": []}},
        )
    )

    async with VKAPIClient(service_token="dummy") as client:
        with pytest.raises(VKAPIError) as exc_info:
            await client.groups_get_by_id("ghost")

    assert exc_info.value.code == 100


@pytest.mark.asyncio
@respx.mock
async def test_call_includes_token_and_version_in_request():
    """В POST-body должны быть access_token и v — VK без них откажет."""
    route = respx.post(f"{VK_API_BASE}/groups.getById").mock(
        return_value=httpx.Response(
            200,
            json={"response": {"groups": [{"id": 1, "name": "x"}], "profiles": []}},
        )
    )

    async with VKAPIClient(service_token="my_secret_token") as client:
        await client.groups_get_by_id("x")

    # respx даёт сырое body как bytes — декодируем
    body = bytes(route.calls[0].request.read()).decode()
    assert "access_token=my_secret_token" in body
    assert f"v={VK_API_VERSION}" in body
    # Также проверим что наш метод-специфичный параметр есть
    assert "group_ids=x" in body


@pytest.mark.asyncio
@respx.mock
async def test_throttle_makes_pause_between_fast_calls():
    """Два быстрых вызова подряд должны быть разнесены по времени.

    Проверяем что общее время выполнения >= 1/rate_limit_per_sec.
    """
    respx.post(f"{VK_API_BASE}/groups.getById").mock(
        return_value=httpx.Response(
            200,
            json={"response": {"groups": [{"id": 1, "name": "x"}], "profiles": []}},
        )
    )

    async with VKAPIClient(service_token="dummy", rate_limit_per_sec=10) as client:
        # rate_limit_per_sec=10 → min_interval=0.1сек.
        start = time.monotonic()
        await client.groups_get_by_id("a")
        await client.groups_get_by_id("b")
        elapsed = time.monotonic() - start

    # Между двумя вызовами должна быть пауза ≥0.1 сек.
    # С запасом проверяем ≥0.08 (на случай погрешностей CI runner).
    assert elapsed >= 0.08, f"Throttle не сработал, elapsed={elapsed}"


@pytest.mark.asyncio
async def test_call_without_context_manager_raises():
    """Использование без `async with` должно явно ругаться."""
    client = VKAPIClient(service_token="dummy")
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.call("groups.getById", group_ids="x")
