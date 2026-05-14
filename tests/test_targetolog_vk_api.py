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


# --- groups.search ---


@pytest.mark.asyncio
@respx.mock
async def test_groups_search_returns_items():
    """groups.search возвращает items с группами."""
    respx.post(f"{VK_API_BASE}/groups.search").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "count": 3,
                    "items": [
                        {"id": 1, "name": "Верую Православие", "members_count": 700000},
                        {"id": 2, "name": "Православие", "members_count": 80000},
                        {"id": 3, "name": "Постная трапеза", "members_count": 90000},
                    ],
                }
            },
        )
    )

    async with VKAPIClient(service_token="dummy") as client:
        groups = await client.groups_search("православие", count=3)

    assert len(groups) == 3
    assert groups[0]["name"] == "Верую Православие"
    assert groups[0]["members_count"] == 700_000


@pytest.mark.asyncio
@respx.mock
async def test_groups_search_includes_required_params():
    """В запросе должны быть q, count, type=group, fields, country."""
    route = respx.post(f"{VK_API_BASE}/groups.search").mock(
        return_value=httpx.Response(
            200, json={"response": {"count": 0, "items": []}}
        )
    )

    async with VKAPIClient(service_token="dummy") as client:
        await client.groups_search("монастырь", count=20, country=1)

    body = bytes(route.calls[0].request.read()).decode()
    assert "q=" in body and "%D0%BC%D0%BE%D0%BD%D0%B0%D1%81%D1%82%D1%8B%D1%80%D1%8C" in body
    assert "count=20" in body
    assert "type=group" in body
    assert "country=1" in body
    # fields содержит members_count для сортировки на нашей стороне
    assert "members_count" in body


# --- groups.getMembers с пагинацией ---


@pytest.mark.asyncio
@respx.mock
async def test_groups_get_members_paginates_when_more_than_1000():
    """Если в группе >1000 человек — клиент делает несколько запросов."""
    # Имитируем сообщество с 2500 подписчиками — 3 запроса (1000+1000+500)
    call_count = [0]

    def respond(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        body = request.read().decode()
        # Парсим offset из form-data
        offset = 0
        for part in body.split("&"):
            if part.startswith("offset="):
                offset = int(part.split("=")[1])

        # Генерим уникальные ID для каждой страницы
        if offset == 0:
            items = list(range(1, 1001))  # 1..1000
        elif offset == 1000:
            items = list(range(1001, 2001))  # 1001..2000
        elif offset == 2000:
            items = list(range(2001, 2501))  # 2001..2500 (500 — последняя страница)
        else:
            items = []

        return httpx.Response(
            200, json={"response": {"count": 2500, "items": items}}
        )

    respx.post(f"{VK_API_BASE}/groups.getMembers").mock(side_effect=respond)

    async with VKAPIClient(service_token="dummy", rate_limit_per_sec=100) as client:
        members = await client.groups_get_members("pomolimsy")

    assert call_count[0] == 3, f"Должно быть 3 запроса с пагинацией, было {call_count[0]}"
    assert len(members) == 2500
    assert members[0] == 1
    assert members[-1] == 2500


@pytest.mark.asyncio
@respx.mock
async def test_groups_get_members_respects_max_count():
    """max_count=100 → возвращает ровно 100 (хоть в группе и больше)."""
    respx.post(f"{VK_API_BASE}/groups.getMembers").mock(
        return_value=httpx.Response(
            200,
            json={"response": {"count": 5000, "items": list(range(1, 1001))}},
        )
    )

    async with VKAPIClient(service_token="dummy", rate_limit_per_sec=100) as client:
        members = await client.groups_get_members("pomolimsy", max_count=100)

    assert len(members) == 100
    assert members[0] == 1
    assert members[-1] == 100


@pytest.mark.asyncio
@respx.mock
async def test_groups_get_members_raises_on_closed_group():
    """Закрытое сообщество — VK возвращает error 15 (access denied)."""
    respx.post(f"{VK_API_BASE}/groups.getMembers").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": {
                    "error_code": 15,
                    "error_msg": "Access denied: group members are hidden",
                }
            },
        )
    )

    async with VKAPIClient(service_token="dummy") as client:
        with pytest.raises(VKAPIError) as exc_info:
            await client.groups_get_members("closed_group")

    assert exc_info.value.code == 15


# --- find_orthodox_communities (расширенный поиск с фильтрацией) ---


@pytest.mark.asyncio
@respx.mock
async def test_find_orthodox_filters_blacklist():
    """Сообщества с «сатан», «прикол», «католич» в названии/описании отсеиваются."""
    # Все запросы groups.search мокаем одним списком
    respx.post(f"{VK_API_BASE}/groups.search").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "count": 5,
                    "items": [
                        # Должны пройти
                        {"id": 1, "name": "Православие.ру", "description": "Молитвы за здравие", "members_count": 50000},
                        {"id": 2, "name": "Дивеевский монастырь", "description": "Святыни Серафима", "members_count": 30000},
                        # Должны отфильтроваться
                        {"id": 3, "name": "Церковь Сатаны", "description": "Чернокнижие", "members_count": 5000},
                        {"id": 4, "name": "Православный прикол", "description": "Юмор о церкви", "members_count": 8000},
                        {"id": 5, "name": "Католическая церковь", "description": "Папа Римский", "members_count": 20000},
                    ],
                }
            },
        )
    )

    async with VKAPIClient(service_token="dummy", rate_limit_per_sec=100) as client:
        groups = await client.find_orthodox_communities(
            queries=["православие"], min_members=500
        )

    ids = [g["id"] for g in groups]
    assert 1 in ids, "Православие.ру должна остаться"
    assert 2 in ids, "Дивеевский монастырь должен остаться"
    assert 3 not in ids, "Сатанизм должен быть отфильтрован"
    assert 4 not in ids, "Приколы должны быть отфильтрованы"
    assert 5 not in ids, "Католическая церковь должна быть отфильтрована"


@pytest.mark.asyncio
@respx.mock
async def test_find_orthodox_dedups_across_queries():
    """Одна и та же группа из разных поисков — один раз в результате."""
    call_count = [0]

    def respond(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        # Все 6 дефолтных запросов возвращают одну и ту же группу
        return httpx.Response(
            200,
            json={
                "response": {
                    "count": 1,
                    "items": [
                        {"id": 100, "name": "Господу Помолимся", "description": "Поминовение", "members_count": 62000}
                    ],
                }
            },
        )

    respx.post(f"{VK_API_BASE}/groups.search").mock(side_effect=respond)

    async with VKAPIClient(service_token="dummy", rate_limit_per_sec=100) as client:
        groups = await client.find_orthodox_communities()

    assert call_count[0] == 6, "По умолчанию 6 параллельных запросов"
    assert len(groups) == 1, "Дедуп — одна группа в результате"
    assert groups[0]["id"] == 100


@pytest.mark.asyncio
@respx.mock
async def test_find_orthodox_filters_small_groups():
    """Группы с < min_members подписчиков отсеиваются."""
    respx.post(f"{VK_API_BASE}/groups.search").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "count": 3,
                    "items": [
                        {"id": 1, "name": "Большой монастырь", "members_count": 50000, "description": ""},
                        {"id": 2, "name": "Средний приход", "members_count": 5000, "description": ""},
                        {"id": 3, "name": "Малая часовня", "members_count": 100, "description": ""},
                    ],
                }
            },
        )
    )

    async with VKAPIClient(service_token="dummy", rate_limit_per_sec=100) as client:
        groups = await client.find_orthodox_communities(
            queries=["православие"], min_members=1000
        )

    ids = [g["id"] for g in groups]
    assert 1 in ids
    assert 2 in ids
    assert 3 not in ids, "Группа с 100 подписчиков должна быть отфильтрована"


@pytest.mark.asyncio
@respx.mock
async def test_find_orthodox_sorts_by_members_desc():
    """Результат отсортирован по числу подписчиков убывание."""
    respx.post(f"{VK_API_BASE}/groups.search").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "count": 3,
                    "items": [
                        {"id": 1, "name": "Малый", "members_count": 5000, "description": ""},
                        {"id": 2, "name": "Огромный", "members_count": 700000, "description": ""},
                        {"id": 3, "name": "Средний", "members_count": 80000, "description": ""},
                    ],
                }
            },
        )
    )

    async with VKAPIClient(service_token="dummy", rate_limit_per_sec=100) as client:
        groups = await client.find_orthodox_communities(
            queries=["x"], min_members=500
        )

    assert groups[0]["id"] == 2  # Огромный — первый
    assert groups[1]["id"] == 3  # Средний
    assert groups[2]["id"] == 1  # Малый
