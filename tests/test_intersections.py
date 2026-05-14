"""Тесты модуля пересечений (Phase 5.2)."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.targetolog.intersections import (
    GroupParseResult,
    find_intersections,
    parse_groups_parallel,
)
from src.targetolog.vk_api_client import VK_API_BASE, VKAPIClient


def test_find_intersections_min_2():
    """Базовая логика: user в 2+ группах попадает в hot_users."""
    groups = [
        GroupParseResult(
            screen_name="group_a",
            group_name="A",
            total_members=3,
            parsed_members={1, 2, 3},
        ),
        GroupParseResult(
            screen_name="group_b",
            group_name="B",
            total_members=3,
            parsed_members={2, 3, 4},
        ),
        GroupParseResult(
            screen_name="group_c",
            group_name="C",
            total_members=2,
            parsed_members={3, 5},
        ),
    ]

    result = find_intersections(groups, min_intersections=2)

    # user 1: только в A → не горячий
    # user 2: в A+B → горячий
    # user 3: в A+B+C → горячий
    # user 4: только в B → не горячий
    # user 5: только в C → не горячий
    assert result.hot_users == {2, 3}
    assert result.total_unique_users == 5
    assert result.hot_users_count == 2


def test_find_intersections_min_3():
    """min_intersections=3 — только подписчики 3+ групп."""
    groups = [
        GroupParseResult("a", "A", 3, {1, 2, 3}),
        GroupParseResult("b", "B", 3, {2, 3, 4}),
        GroupParseResult("c", "C", 2, {3, 5}),
    ]

    result = find_intersections(groups, min_intersections=3)

    # Только user 3 в 3 группах
    assert result.hot_users == {3}


def test_find_intersections_empty_groups():
    """Если все группы пустые или с ошибками — никаких горячих."""
    groups = [
        GroupParseResult("a", "A", 0, set(), error="API failed"),
        GroupParseResult("b", "B", 0, set()),
    ]

    result = find_intersections(groups, min_intersections=2)

    assert result.hot_users == set()
    assert result.total_unique_users == 0


def test_find_intersections_distribution():
    """get_distribution() возвращает корректную статистику."""
    groups = [
        GroupParseResult("a", "A", 4, {1, 2, 3, 4}),
        GroupParseResult("b", "B", 3, {2, 3, 5}),
        GroupParseResult("c", "C", 2, {3, 6}),
    ]

    result = find_intersections(groups, min_intersections=2)
    dist = result.get_distribution()

    # user 1: в 1 группе (A)
    # user 2: в 2 (A+B)
    # user 3: в 3 (A+B+C)
    # user 4: в 1 (A)
    # user 5: в 1 (B)
    # user 6: в 1 (C)
    assert dist[1] == 4  # 1, 4, 5, 6 — каждый в 1 группе
    assert dist[2] == 1  # user 2
    assert dist[3] == 1  # user 3


@pytest.mark.asyncio
@respx.mock
async def test_parse_groups_parallel_collects_members():
    """parse_groups_parallel парсит инфу и подписчиков для каждой группы."""
    # Мокаем groups.getById для двух групп
    def get_by_id_respond(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "group_ids=group_a" in body:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "groups": [
                            {"id": 1, "name": "Group A", "members_count": 100}
                        ],
                        "profiles": [],
                    }
                },
            )
        if "group_ids=group_b" in body:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "groups": [
                            {"id": 2, "name": "Group B", "members_count": 50}
                        ],
                        "profiles": [],
                    }
                },
            )
        return httpx.Response(500)

    respx.post(f"{VK_API_BASE}/groups.getById").mock(side_effect=get_by_id_respond)

    # Мокаем groups.getMembers — возвращаем уникальные ID для каждой
    def get_members_respond(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        if "group_id=group_a" in body:
            return httpx.Response(
                200,
                json={"response": {"count": 100, "items": list(range(1, 101))}},
            )
        if "group_id=group_b" in body:
            return httpx.Response(
                200,
                json={"response": {"count": 50, "items": list(range(50, 100))}},
            )
        return httpx.Response(500)

    respx.post(f"{VK_API_BASE}/groups.getMembers").mock(side_effect=get_members_respond)

    async with VKAPIClient(service_token="x", rate_limit_per_sec=100) as client:
        results = await parse_groups_parallel(client, ["group_a", "group_b"])

    assert len(results) == 2
    assert results[0].screen_name == "group_a"
    assert results[0].group_name == "Group A"
    assert len(results[0].parsed_members) == 100
    assert results[1].screen_name == "group_b"
    assert len(results[1].parsed_members) == 50


@pytest.mark.asyncio
@respx.mock
async def test_parse_groups_parallel_handles_closed_group():
    """Если у одной из групп подписчики скрыты (error 15) — записываем error, но продолжаем."""
    respx.post(f"{VK_API_BASE}/groups.getById").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "groups": [{"id": 1, "name": "Hidden", "members_count": 1000}],
                    "profiles": [],
                }
            },
        )
    )
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

    async with VKAPIClient(service_token="x", rate_limit_per_sec=100) as client:
        results = await parse_groups_parallel(client, ["closed_group"])

    assert len(results) == 1
    assert results[0].error is not None
    assert "15" in results[0].error
    assert results[0].parsed_members == set()
