"""HTTP-клиент к VK API на httpx.

Использует service token из settings.vk_api_service_token (Railway env
VK_API_SERVICE_TOKEN). Получен от Vizit'а 14.05.2026, привязан к
зарегистрированному VK ID приложению 54593625 «Парсер».

Ограничения service token (по докам VK):
Доступные публичные методы:
- groups.getById (информация о сообществе)
- groups.getMembers (подписчики ПУБЛИЧНОГО сообщества)
- wall.get (посты публичной стены)
- users.get (базовая инфа о пользователе)
- newsfeed.search (поиск по новостям)

НЕ доступны (потребуют user token через PKCE OAuth позже, если понадобятся):
- likes.getList (кто лайкнул пост)
- friends.get (друзья пользователя)
- users.getSubscriptions (на что подписан человек)
- messages.* (личные сообщения)

Rate limit: 5 запросов/сек на токен. Мы делаем 4/сек с запасом
(в _throttle), чтобы не упереться в 429.

Документация VK API:
- https://dev.vk.com/ru/api/api-requests
- https://dev.vk.com/ru/method/groups.getById
- https://dev.vk.com/ru/method/groups.getMembers
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VK_API_BASE = "https://api.vk.com/method"
# Версия API. 5.199 — стабильная на 2026.
# Старые версии (5.131, 5.81) тоже работают, но новая форма ответа
# у groups.getById только в 5.199+ (с полями groups/profiles).
VK_API_VERSION = "5.199"


class VKAPIError(Exception):
    """Ошибка от VK API (response.error).

    Коды ошибок VK см. https://dev.vk.com/ru/reference/errors. Самые
    частые для нашего случая:
    - 5: пользовательская авторизация не удалась (плохой токен)
    - 6: слишком много запросов в секунду
    - 15: доступ запрещён (закрытое сообщество)
    - 100: один из параметров неверный
    - 113: invalid user id (несуществующий пользователь)
    - 203: доступ к группе запрещён
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"VK API error {code}: {message}")


class VKAPIClient:
    """Минимальный async-клиент к VK API.

    Использовать как async context manager (для управления httpx.AsyncClient):

        async with VKAPIClient(service_token="...") as client:
            group = await client.groups_get_by_id("pomolimsy")

    Все методы возвращают распакованное поле 'response' из VK API ответа.
    """

    def __init__(
        self,
        service_token: str,
        api_version: str = VK_API_VERSION,
        rate_limit_per_sec: int = 4,
        timeout_seconds: float = 30.0,
    ):
        self.service_token = service_token
        self.api_version = api_version
        self.rate_limit_per_sec = rate_limit_per_sec
        self.timeout_seconds = timeout_seconds
        # time.monotonic() безопаснее event_loop.time() — не зависит от loop
        self._last_call_time: float = 0.0
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "VKAPIClient":
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        """Простейший rate limiter — пауза перед запросом если нужно.

        Не идеальный (single-instance, без честной shared-очереди), но
        для нашего объёма (один процесс, один токен) хватает.
        """
        min_interval = 1.0 / self.rate_limit_per_sec
        now = time.monotonic()
        elapsed = now - self._last_call_time
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_call_time = time.monotonic()

    async def call(self, method: str, **params: Any) -> Any:
        """Универсальный вызов любого VK API метода.

        Автоматически:
        - Добавляет access_token и v (API version)
        - Throttle перед каждым запросом
        - Парсит ответ и поднимает VKAPIError на ошибки

        Returns:
            Распакованное поле 'response' из VK API JSON ответа.
            Может быть dict, list, число, строка — зависит от метода.

        Raises:
            VKAPIError: если VK вернул объект error
            httpx.HTTPStatusError: если HTTP-статус не 2xx
            RuntimeError: если клиент не открыт как context manager
        """
        if not self._client:
            raise RuntimeError(
                "VKAPIClient должен использоваться как async context manager: "
                "`async with VKAPIClient(...) as client:`"
            )

        await self._throttle()

        # access_token и v добавляем последними чтобы не перетереть
        # явно переданные params если кто-то их укажет (но обычно никто
        # не должен — это служебные параметры).
        request_params = {**params, "access_token": self.service_token, "v": self.api_version}

        url = f"{VK_API_BASE}/{method}"
        logger.debug(
            "VK API call: %s (params: %s)",
            method,
            [k for k in params.keys()],  # не логируем значения — могут быть PII
        )

        # VK принимает и GET и POST. POST безопаснее (токен не в URL → не
        # в логах reverse-proxy).
        response = await self._client.post(url, data=request_params)
        response.raise_for_status()

        data = response.json()
        if "error" in data:
            err = data["error"]
            raise VKAPIError(
                code=err.get("error_code", -1),
                message=err.get("error_msg", "Unknown VK API error"),
            )

        return data.get("response")

    async def groups_get_by_id(self, group_id: str | int) -> dict:
        """Получить информацию о сообществе по ID или screen_name.

        Args:
            group_id: числовой ID (216409501) или screen_name ('pomolimsy',
                'pravoslavnie_hristiane'). VK сам распарсит и то и другое.
                URL `vk.com/pomolimsy` нужно сначала почистить — оставить
                только последний сегмент (`pomolimsy`).

        Returns:
            dict с полями группы:
            - id (int)
            - name (str) — отображаемое название
            - screen_name (str) — slug в URL
            - members_count (int) — число подписчиков
            - description (str) — описание сообщества
            - is_closed (int): 0=открытое, 1=закрытое, 2=частное

        Raises:
            VKAPIError(100): сообщество не найдено
            VKAPIError(203): доступ запрещён (закрытое)
        """
        # VK API метод groups.getById принимает CSV в group_ids.
        # Запрашиваем одну группу → ожидаем список с одним элементом.
        response = await self.call(
            "groups.getById",
            group_ids=str(group_id),
            fields="members_count,description",
        )

        # Форма ответа меняется между версиями API:
        # - v5.131+: {"groups": [...], "profiles": []}  (новая)
        # - v5.81 и старее: прямой list of group objects (старая)
        # Поддерживаем обе на всякий случай.
        if isinstance(response, dict) and "groups" in response:
            groups = response["groups"]
        elif isinstance(response, list):
            groups = response
        else:
            raise VKAPIError(
                -1,
                f"Неожиданный формат ответа groups.getById: {type(response).__name__}",
            )

        if not groups:
            raise VKAPIError(100, f"Сообщество не найдено: {group_id}")

        return groups[0]

    async def groups_search(
        self,
        query: str,
        count: int = 30,
        country: int = 1,
    ) -> list[dict]:
        """Поиск сообществ по ключевой фразе.

        VK API возвращает результаты по умолчанию отсортированными по
        релевантности, что обычно ставит крупные сообщества первыми.

        Args:
            query: ключевое слово ('православие', 'монастырь', 'молитва').
            count: число результатов, max 1000 за запрос (VK сам обрежет).
            country: ID страны фильтра (1 = Россия). Передать 0 для всех.

        Returns:
            Список словарей групп, каждый с полями id, name, screen_name,
            members_count, description, photo_200, is_closed.

        Raises:
            VKAPIError: при ошибке от VK.
        """
        params: dict[str, Any] = {
            "q": query,
            "count": min(count, 1000),
            "type": "group",  # только группы, без событий и пабликов
            "fields": "members_count,description,photo_200,activity",
        }
        if country:
            params["country"] = country

        response = await self.call("groups.search", **params)
        # response — это {"count": N, "items": [...]}
        if not isinstance(response, dict) or "items" not in response:
            raise VKAPIError(-1, f"Неожиданный формат ответа groups.search: {response}")
        return response["items"]

    async def groups_get_members(
        self,
        group_id: str | int,
        max_count: int | None = None,
        fields: str = "",
    ) -> list[int | dict]:
        """Парсинг ID подписчиков сообщества с автоматической пагинацией.

        VK отдаёт по 1000 пользователей за запрос. Если в сообществе 60000
        подписчиков — будет 60 запросов, при rate limit 4/sec это ~15 секунд.

        Args:
            group_id: numeric ID или screen_name сообщества.
            max_count: ограничение общего числа (None = все доступные).
                Полезно для тестового парсинга — взять первые 100.
            fields: CSV дополнительных полей пользователей (например
                'sex,bdate,city'). Если пусто — возвращаются только ID.

        Returns:
            Если fields пусто — список int (user IDs).
            Если fields указан — список dict с user объектами.

        Raises:
            VKAPIError(15): сообщество закрытое, доступ запрещён.
            VKAPIError(100): сообщество не найдено.
        """
        all_members: list[Any] = []
        offset = 0
        per_request = 1000  # VK максимум за запрос

        while True:
            params: dict[str, Any] = {
                "group_id": str(group_id),
                "count": per_request,
                "offset": offset,
            }
            if fields:
                params["fields"] = fields

            response = await self.call("groups.getMembers", **params)
            if not isinstance(response, dict) or "items" not in response:
                raise VKAPIError(
                    -1, f"Неожиданный формат ответа groups.getMembers: {response}"
                )

            items = response["items"]
            total = response.get("count", 0)
            all_members.extend(items)

            # Прекращаем если: достигли max_count, или больше нет
            # элементов, или собрали всех.
            if max_count is not None and len(all_members) >= max_count:
                all_members = all_members[:max_count]
                break
            if len(items) < per_request:
                break  # последняя страница
            if len(all_members) >= total:
                break  # собрали всех по версии VK

            offset += per_request

        return all_members

    async def users_get_batch(
        self,
        user_ids: list[int],
        fields: str = "sex,bdate,city",
    ) -> list[dict]:
        """Получает профили пользователей пачкой.

        Используется для фильтрации горячей аудитории — узнаём пол,
        возраст и город каждого человека, оставляем только тех кто
        попадает в ЦА.

        VK ограничивает 1000 user_id за запрос (через CSV-параметр).
        Делаем автопагинацию.

        Args:
            user_ids: список ID пользователей
            fields: CSV полей которые нужны. Стандартные:
                - sex (1=жен, 2=муж, 0=не указан)
                - bdate (формат 'D.M.YYYY' или 'D.M' если год скрыт)
                - city (объект {id, title})
                - country, online, has_photo, и другие

        Returns:
            Список dict с профилями. Для пользователей которые скрыли
            свои данные — соответствующие поля будут отсутствовать.

        Ограничения service token:
        - Возвращает только публичные данные
        - Удалённые/заблокированные пользователи возвращают объект с
          полями {id, deactivated: 'deleted'|'banned'} — без bdate/city
        """
        if not user_ids:
            return []

        all_profiles: list[dict] = []
        per_request = 1000  # VK максимум

        for start in range(0, len(user_ids), per_request):
            batch = user_ids[start : start + per_request]
            response = await self.call(
                "users.get",
                user_ids=",".join(str(uid) for uid in batch),
                fields=fields,
            )
            # response — это просто список профилей
            if isinstance(response, list):
                all_profiles.extend(response)
            else:
                # неожиданный формат — логируем но не падаем
                logger.warning(
                    f"users.get вернул не список: {type(response).__name__}"
                )

        return all_profiles

    async def find_orthodox_communities(
        self,
        queries: list[str] | None = None,
        min_members: int = 500,
        max_per_query: int = 30,
    ) -> list[dict]:
        """Расширенный поиск православных сообществ с фильтрацией мусора.

        Vizit (14.05.2026): «бывают издевательские сообщества типо
        церковь сатаны, или приколы». Сырой поиск по 'монастырь'
        возвращает всякое — нужна предфильтрация.

        Стратегия:
        1. Параллельные запросы с разными комбинациями ключей —
           «православная церковь», «православный монастырь», и т.д.
           Префикс «православн*» сильно отсекает другие конфессии.
        2. Дедуп по group ID (один и тот же монастырь может быть найден
           под разными ключами).
        3. Блек-лист стоп-слов в названии/описании: «сатан», «прикол»,
           «угар», «шутк», «юмор», «троллинг», «ересь», и т.д.
        4. Минимум подписчиков — отсекает мелкие шутливые группы.
        5. Сортировка по числу подписчиков убывание.

        Args:
            queries: список ключей. По умолчанию — стандартный набор для
                православных мест (церкви, монастыри, соборы, часовни).
            min_members: минимум подписчиков для включения (500 = норма).
            max_per_query: сколько результатов брать с каждого ключа.

        Returns:
            Отсортированный по убыванию подписчиков список dict групп,
            прошедших фильтрацию.
        """
        if queries is None:
            # Стандартный набор: префикс 'православн*' + типы мест
            # отсекает другие конфессии (католические, протестантские,
            # буддистские «храмы»). А ещё ключ 'икона' для иконных сообществ
            # и просто 'православие' для информационных каналов.
            queries = [
                "православная церковь",
                "православный монастырь",
                "православный собор",
                "православная часовня",
                "православная икона",
                "православные молитвы",
            ]

        # Чёрный список — если в name или description встречается одно из
        # этих ВХОЖДЕНИЙ (case-insensitive), отбрасываем. Использовать
        # подстроки, не точные слова — чтобы ловить «приколов», «шутки»
        # и т.д. с разными окончаниями.
        BLACKLIST_SUBSTRINGS = [
            "сатан",       # сатанизм, церковь сатаны
            "ересь",       # ересь, еретик
            "прикол",      # приколы, прикольный
            "угар",        # угарно, угар
            "шутк",        # шутки, шутковать
            "юмор",        # юмор, юморист
            "троллинг",
            "стеб",        # стёб над верой
            "пародия",
            "анекдот",
            "мем ",        # сообщества мемов про церковь
            "мемы",
            # Иные конфессии (если попадут через общие ключи)
            "католич",
            "протестант",
            "евангел",     # евангельская церковь = протестанты
            "адвентист",
            "свидетел",    # свидетели иеговы
            "иегова",
            "мормон",
            "буддист",     # буддистская часовня
            "буддийск",
            "мусульман",
            "ислам",
            "синагог",     # иудейские
        ]

        # 1. Параллельные запросы
        all_groups: dict[int, dict] = {}  # id -> group dict (дедуп)
        for query in queries:
            try:
                groups = await self.groups_search(
                    query, count=max_per_query, country=1
                )
            except VKAPIError as e:
                logger.warning(f"groups_search failed for '{query}': {e}")
                continue
            for g in groups:
                gid = g.get("id")
                if gid:
                    # При дубликате оставляем версию с более полным описанием
                    existing = all_groups.get(gid)
                    if not existing or (
                        len(g.get("description", "")) > len(existing.get("description", ""))
                    ):
                        all_groups[gid] = g

        # 2. Фильтрация по чёрному списку и min_members
        filtered = []
        for group in all_groups.values():
            name = (group.get("name", "") or "").lower()
            description = (group.get("description", "") or "").lower()
            text = f"{name} {description}"
            members = group.get("members_count", 0)

            # Проверка чёрного списка
            if any(bad in text for bad in BLACKLIST_SUBSTRINGS):
                continue

            # Проверка минимума подписчиков
            if members < min_members:
                continue

            filtered.append(group)

        # 3. Сортировка по подписчикам убывание
        filtered.sort(key=lambda g: g.get("members_count", 0), reverse=True)

        return filtered
