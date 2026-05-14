"""Поиск пересечений подписчиков нескольких VK-сообществ.

Phase 5.2 — основная ценность таргетолога. Подписчик одной правосл.
группы — это просто верующий. Подписчик 3-5 правосл. групп — это
«горячий» представитель ЦА, гораздо вероятнее напишет имя для
поминовения. Это и есть «горячая аудитория» по ТЗ Vizit'а.

Логика проста, выполняется на стороне Python через set-операции:
1. Парсим подписчиков каждой группы → set[int] ID
2. Для каждого пользователя считаем в скольких группах он состоит
3. Фильтруем тех у кого counter >= min_intersections
4. Возвращаем список ID + статистику по группам

Скорость: упирается в VK API rate limit (4 req/sec). На 8 групп по
50k-700k человек — примерно 5-15 минут. На длинной выборке ничего
не закэшируем (лимит памяти не настолько критичен, ~1M int IDs ~30MB).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable

from src.targetolog.vk_api_client import VKAPIClient, VKAPIError

logger = logging.getLogger(__name__)


@dataclass
class GroupParseResult:
    """Результат парсинга одного сообщества."""

    screen_name: str
    group_name: str
    total_members: int      # сколько у группы всего по версии VK
    parsed_members: set[int]  # сколько мы реально собрали (может быть меньше)
    error: str | None = None  # если парсинг упал — сообщение об ошибке


@dataclass
class IntersectionResult:
    """Результат пересечения нескольких групп."""

    groups: list[GroupParseResult]
    user_to_groups: dict[int, set[str]]  # user_id -> {screen_names}
    hot_users: set[int]  # ID пользователей с counter >= min_intersections
    min_intersections: int

    @property
    def total_unique_users(self) -> int:
        """Общее число уникальных пользователей по всем группам (объединение)."""
        return len(self.user_to_groups)

    @property
    def hot_users_count(self) -> int:
        return len(self.hot_users)

    def get_distribution(self) -> dict[int, int]:
        """Распределение: сколько пользователей подписано на 1, 2, 3, ... групп."""
        dist: dict[int, int] = {}
        for groups_set in self.user_to_groups.values():
            count = len(groups_set)
            dist[count] = dist.get(count, 0) + 1
        return dist


async def parse_groups_parallel(
    client: VKAPIClient,
    screen_names: Iterable[str],
    max_per_group: int | None = None,
) -> list[GroupParseResult]:
    """Парсит подписчиков нескольких сообществ.

    Запросы идут последовательно (не параллельно), потому что VK API
    rate limit на токен ОДИН — все наши запросы упираются в один лимит
    независимо от параллельности. asyncio.gather не ускорит.

    Args:
        client: открытый VKAPIClient
        screen_names: список идентификаторов сообществ
        max_per_group: ограничить число подписчиков с каждой группы
            (полезно для теста — например по 1000 с каждой вместо всех)

    Returns:
        Список GroupParseResult по одному на сообщество. Если парсинг
        упал для какого-то — error заполнен, parsed_members пустой.
    """
    results: list[GroupParseResult] = []

    for screen_name in screen_names:
        # Сначала получаем инфо о группе (имя, число подписчиков)
        try:
            info = await client.groups_get_by_id(screen_name)
            group_name = info.get("name", screen_name)
            total = info.get("members_count", 0)
        except VKAPIError as e:
            results.append(
                GroupParseResult(
                    screen_name=screen_name,
                    group_name=screen_name,
                    total_members=0,
                    parsed_members=set(),
                    error=f"groups.getById failed: {e.code} {e.message}",
                )
            )
            continue

        # Парсим подписчиков
        try:
            members = await client.groups_get_members(
                screen_name, max_count=max_per_group
            )
            results.append(
                GroupParseResult(
                    screen_name=screen_name,
                    group_name=group_name,
                    total_members=total,
                    parsed_members=set(members),
                )
            )
            logger.info(
                f"Parsed {screen_name}: {len(members)}/{total} подписчиков"
            )
        except VKAPIError as e:
            results.append(
                GroupParseResult(
                    screen_name=screen_name,
                    group_name=group_name,
                    total_members=total,
                    parsed_members=set(),
                    error=f"groups.getMembers failed: {e.code} {e.message}",
                )
            )
            logger.warning(f"Failed to parse {screen_name}: {e}")

    return results


def find_intersections(
    groups: list[GroupParseResult],
    min_intersections: int = 2,
) -> IntersectionResult:
    """Находит пользователей подписанных на N+ сообществ одновременно.

    Args:
        groups: список результатов парсинга
        min_intersections: минимум групп в которых должен состоять
            пользователь. По умолчанию 2 — это и есть пересечение.
            3-4 даст более узкую «горячую» аудиторию.

    Returns:
        IntersectionResult с распределением и списком ID hot_users.
    """
    # Строим reverse-index: user_id -> {screen_names в которых он есть}
    user_to_groups: dict[int, set[str]] = {}
    for result in groups:
        if result.error or not result.parsed_members:
            continue
        for user_id in result.parsed_members:
            user_to_groups.setdefault(user_id, set()).add(result.screen_name)

    # Отсеиваем «горячих» — состоящих в min_intersections+ группах
    hot_users = {
        user_id
        for user_id, groups_set in user_to_groups.items()
        if len(groups_set) >= min_intersections
    }

    return IntersectionResult(
        groups=groups,
        user_to_groups=user_to_groups,
        hot_users=hot_users,
        min_intersections=min_intersections,
    )
