"""Высокоуровневый сервис создания рекламных кампаний.

Получает на вход картинку + текст темы + список возрастных окон,
собирает payload по доке VK Ads и одним POST на /ad_plans.json
создаёт всю иерархию: кампания → N групп → баннеры.

Ключевая фича — **возрастной A/B сплит**: одна тема, одно изображение,
но N групп объявлений, каждая со своим узким возрастным диапазоном.
Это даёт гранулярные данные на какой возраст хорошо заходит, и Phase 4
сможет автоматически отключать слабые группы.

Пример использования:
    creator = AdCreator(vk_client, settings)
    summary = await creator.create_age_split_campaign(
        image_bytes=photo_bytes,
        theme="Зеленецкий монастырь — молитвы за здравие",
        community_url_id=94164713,
        age_splits=[(41, 42), (43, 44), (45, 46), (47, 48), (49, 50)],
        daily_budget_rub_per_group=200,
    )
    print(summary.ad_plan_id, len(summary.ad_group_ids))

Структура VK Ads API v2 (по доке https://ads.vk.com/doc/api):
- ad_plan (кампания) содержит:
  - status, dates, budget_limit_day, objective, ad_object_id, ad_object_type
  - ad_groups[] — каждая со своим targeting (geo, sex, age_list)
    - banners[] — каждый с textblocks и content (id картинок)

Поля textblocks для package_id 3122 (Вступить в сообщество):
- title_40_vkads — заголовок до 40 символов
- text_2000 — основной текст до 2000 символов
- about_company_115 — О компании, до 115 символов
- cta_community_vk — CTA из enum (signUp, learnMore, ...)
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.vk_ads.client import VKAdsAPIError, VKAdsClient

logger = logging.getLogger(__name__)


# DEFAULT_PADS — справочный список ID площадок (placements) для package_id 3122.
# Эти ID взяты из реальной кампании 20865519, успешно созданной через UI кабинета
# (см. /inspect в боте — раздел ad_groups[].targetings.pads).
#
# В TEKУЩЕМ payload этот список НЕ ИСПОЛЬЗУЕТСЯ.
# Причина: в UI кабинета по умолчанию включён тоггл «Автоматический выбор мест
# размещения (рекомендуется)». Когда мы не передаём `pads` в targetings, VK
# работает в auto-режиме и сам подбирает площадки. Если же передать pads явно —
# VK переходит в manual-режим и требует чтобы patterns были pre-настроены в
# settings package, что приводит к ошибке валидации:
#   bad_value: "At least one pattern must be in package's settings"
#
# Список сохранён как справка — на случай если в будущем понадобится
# manual-режим с конкретными площадками и мы предварительно настроим
# patterns в кабинете VK или придумаем другой способ.
DEFAULT_PADS: list[int] = [
    1010345, 1265106, 1302973, 1361696, 1985149, 2243453, 2243456,
]


@dataclass
class AdCopy:
    """Текст одного объявления — что Claude или пользователь сгенерил."""

    title: str  # ≤40 символов (title_40_vkads)
    text: str  # ≤2000 символов (text_2000)
    about: str  # ≤115 символов (about_company_115)
    cta: str = "signUp"  # cta_community_vk: signUp/learnMore/openSite/etc


@dataclass
class CampaignSummary:
    """Что вернуло VK после создания."""

    ad_plan_id: int
    ad_group_ids: list[int] = field(default_factory=list)
    banner_ids: list[int] = field(default_factory=list)
    raw: dict | None = None


class AdCreatorError(Exception):
    """Ошибка при создании кампании.

    Если ошибка пришла из VK API — содержит диагностические данные:
    request_id, request_time, status_code (для пересылки в поддержку VK).
    """

    def __init__(
        self,
        message: str,
        *,
        vk_error: "VKAdsAPIError | None" = None,
    ) -> None:
        super().__init__(message)
        self.vk_error = vk_error


class AdCreator:
    """Оркестратор создания рекламных кампаний с возрастным A/B сплитом."""

    def __init__(self, vk_client: VKAdsClient):
        self.vk = vk_client

    async def create_age_split_campaign(
        self,
        image_bytes: bytes,
        theme: str,
        copy: AdCopy,
        community_url: str,
        age_splits: list[tuple[int, int]],
        daily_budget_rub_per_group: int = 200,
        sex: list[str] | None = None,
        geo_regions: list[int] | None = None,
        package_id: int = 3122,
        days_duration: int = 7,
        campaign_name_prefix: str = "auto",
        image_filename: str = "image.jpg",
    ) -> CampaignSummary:
        """Создать кампанию с возрастным сплитом за один POST.

        Args:
            image_bytes: байты картинки для баннера.
            theme: краткое описание темы (для имени кампании в кабинете).
            copy: тексты объявления (title, text, about, cta).
            community_url: полный URL сообщества (https://vk.com/pomolimsy или
                          https://vk.com/club216409501). VK сначала вернёт
                          внутренний URL-ID, который мы используем как ad_object_id.
            age_splits: список окон возраста, например [(41,42), (43,44)].
            daily_budget_rub_per_group: дневной бюджет в рублях на одну группу.
            sex: ['male', 'female'] или один из, по умолчанию оба.
            geo_regions: ID регионов VK, по умолчанию [188] (Россия).
            package_id: тип объявления (3122 = «Вступить в сообщество»).
            days_duration: сколько дней крутить кампанию.
            campaign_name_prefix: префикс имени для удобства идентификации.
            image_filename: имя файла (для multipart, влияет на MIME).

        Returns:
            CampaignSummary с id созданных сущностей.

        Raises:
            AdCreatorError: на любой провал сборки/POST.
        """
        if not age_splits:
            raise AdCreatorError("age_splits пустой — нет групп для создания")
        if sex is None:
            sex = ["female", "male"]
        if geo_regions is None:
            geo_regions = [188]  # Россия

        # 1. Регистрируем URL в кабинете → получаем внутренний url_id
        # ВАЖНО: VK не принимает численный VK community ID как ad_object_id —
        # он хочет именно внутренний идентификатор кабинета, выданный при
        # регистрации URL через /api/v1/urls/.
        logger.info(f"Регистрирую URL в кабинете: {community_url}")
        try:
            url_info = await self.vk.get_or_register_url(community_url)
            internal_url_id = int(url_info["id"])
        except Exception as e:
            raise AdCreatorError(
                f"Не смог зарегистрировать URL {community_url}: {e}"
            ) from e

        # 2. Грузим картинку → content_id
        logger.info(f"Загружаю картинку ({len(image_bytes)} байт) в VK")
        try:
            content_id = await self.vk.upload_image(
                image_bytes=image_bytes, filename=image_filename
            )
        except Exception as e:
            raise AdCreatorError(f"Не смог загрузить картинку: {e}") from e

        # 3. Собираем payload ad_plan с вложенными группами и баннерами
        today = date.today()
        end_date = today + timedelta(days=days_duration)
        date_start = today.isoformat()
        date_end = end_date.isoformat()

        safe_theme = theme[:50].strip()
        campaign_name = f"{campaign_name_prefix} | {safe_theme}"

        # 3-5. Один POST на всю иерархию: ad_plan + вложенные ad_groups + banners.
        #
        # Поле массива групп — `ad_groups: [...]` (актуальное имя из инструкции
        # поддержки VK, chat 92fe567f). Раньше использовали легаси-имя
        # `campaigns: [...]`, но через него VK триггерил старую логику валидации,
        # которая искала несуществующее поле patterns в banner и отвечала
        # 'unknown_resource_field' / 'At least one pattern must be in package settings'.
        # Переменная всё ещё называется nested_campaigns для читаемости diff'а.
        nested_campaigns = []
        for age_from, age_to in age_splits:
            age_list = list(range(age_from, age_to + 1))
            group_name = f"{age_from}-{age_to}"
            nested_campaigns.append({
                "name": group_name,
                "targetings": {
                    "geo": {"regions": geo_regions},
                    "sex": sex,
                    "age": {"age_list": age_list},
                    # group_members — таргет на тех кто НЕ состоит в сообществе.
                    # Логично для objective=socialengagement: нет смысла показывать
                    # объявление о вступлении тем, кто уже вступил.
                    "group_members": "not_group_member",
                    # ВАЖНО: НЕ передаём `pads` (площадки/placements). В UI кабинета
                    # есть тоггл «Автоматический выбор мест размещения
                    # (рекомендуется)» — включён по умолчанию. Когда pads не задан
                    # в payload, VK включает auto-mode и сам подбирает площадки
                    # под наш package_id. Если же передать pads явно — VK перейдёт
                    # в manual-mode и потребует чтобы patterns были pre-настроены
                    # в settings package, что вызывало ошибку
                    # 'At least one pattern must be in package's settings'.
                },
                "max_price": 0,
                "budget_limit_day": daily_budget_rub_per_group,
                "budget_limit": None,
                "date_start": date_start,
                "date_end": None,
                "age_restrictions": "0+",
                "package_id": package_id,
                "banners": [{
                    "name": f"{group_name} | {copy.title[:30]}",
                    # blocked_patterns: [] — явно говорим VK что не блокируем
                    # ни один pattern. Без этого поля VK при валидации не
                    # может определить активные patterns и ругается
                    # 'At least one pattern must be in package's settings'.
                    # В реальной UI-созданной кампании blocked_patterns: []
                    # присутствует — взяли из /inspect 20865519.
                    "blocked_patterns": [],
                    "urls": {"primary": {"id": internal_url_id}},
                    "textblocks": {
                        "title_40_vkads": {"text": copy.title[:40]},
                        "text_2000": {"text": copy.text[:2000]},
                        "about_company_115": {"text": copy.about[:115]},
                        "cta_community_vk": {"text": copy.cta},
                    },
                    "content": {
                        "image_600x600": {"id": content_id},
                    },
                }],
            })

        ad_plan_payload = {
            "name": campaign_name,
            "status": "active",
            "date_start": date_start,
            "date_end": date_end,
            "autobidding_mode": "max_goals",
            "budget_limit_day": daily_budget_rub_per_group * len(age_splits),
            "budget_limit": None,
            "max_price": 0,
            "objective": "socialengagement",
            "ad_object_type": "url",
            "ad_object_id": internal_url_id,
            "ad_groups": nested_campaigns,  # ← актуальное имя поля (не "campaigns")
        }

        logger.info(
            f"[Step 3/3] POST /ad_plans.json: {campaign_name}, "
            f"{len(age_splits)} групп в campaigns[], "
            f"бюджет {ad_plan_payload['budget_limit_day']} ₽/день"
        )
        # Полный payload — на DEBUG-уровне, чтобы при странных ошибках валидации
        # можно было сравнить, что именно мы отправляли. На Railway включается
        # переменной LOG_LEVEL=DEBUG.
        import json as _json
        logger.debug(
            "[ad_plan payload] %s",
            _json.dumps(ad_plan_payload, ensure_ascii=False),
        )
        try:
            response = await self.vk.create_ad_plan(ad_plan_payload)
        except VKAdsAPIError as e:
            # Пробрасываем VKAdsAPIError целиком — у него внутри request_id и
            # время, которые нужны для тикетов в поддержку VK.
            raise AdCreatorError(
                f"VK не принял ad_plan: {e}", vk_error=e
            ) from e
        except Exception as e:
            raise AdCreatorError(f"VK не принял ad_plan: {e}") from e

        # Парсим ответ: ad_plan_id из корня, ids групп/баннеров из nested
        ad_plan_id = int(response.get("id", 0))
        ad_group_ids: list[int] = []
        banner_ids: list[int] = []
        nested_resp = response.get("campaigns") or response.get("ad_groups") or []
        for group in nested_resp:
            if isinstance(group, dict) and "id" in group:
                ad_group_ids.append(int(group["id"]))
                for banner in group.get("banners", []):
                    if isinstance(banner, dict) and "id" in banner:
                        banner_ids.append(int(banner["id"]))

        if not ad_plan_id:
            raise AdCreatorError(f"В ответе VK нет id ad_plan: {response}")

        logger.info(
            f"✅ Кампания создана: ad_plan_id={ad_plan_id}, "
            f"групп={len(ad_group_ids)}, баннеров={len(banner_ids)}"
        )

        return CampaignSummary(
            ad_plan_id=ad_plan_id,
            ad_group_ids=ad_group_ids,
            banner_ids=banner_ids,
            raw=response,
        )

    async def create_banners_in_template_groups(
        self,
        *,
        image_bytes: bytes,
        copy: AdCopy,
        community_url: str,
        template_ad_plan_id: int,
        template_ad_group_ids: list[int],
        banner_name_prefix: str = "bot",
        image_filename: str = "image.jpg",
    ) -> CampaignSummary:
        """Создать N банеров в готовых template-группах (workaround-флоу).

        Используется когда package_id у аккаунта не имеет настроенных
        patterns и нельзя создать кампанию через POST /ad_plans.json.
        Vizit вручную создал шаблонную кампанию с группами через UI,
        а бот добавляет в неё banner через POST /banners.json (это
        работает без проблем — package_id уже инициализирован UI-флоу).

        Шаги:
            1. Регистрируем URL сообщества → получаем internal url_id.
            2. Загружаем картинку → получаем content_id.
            3. Для каждой template-группы создаём один banner через
               POST /banners.json.

        Returns:
            CampaignSummary с ad_plan_id шаблона и списком ID созданных
            банеров (по одному на группу).
        """
        if not template_ad_group_ids:
            raise AdCreatorError("template_ad_group_ids пустой")

        # 1. Регистрируем URL — получаем internal url_id для banners[].urls.primary
        try:
            url_info = await self.vk.get_or_register_url(community_url)
            internal_url_id = int(url_info["id"])
        except Exception as e:
            raise AdCreatorError(
                f"Не смог зарегистрировать URL {community_url}: {e}"
            ) from e

        # 2. Загружаем картинку → content_id (upload_image возвращает int)
        try:
            content_id = await self.vk.upload_image(image_bytes, image_filename)
        except Exception as e:
            raise AdCreatorError(f"Не смог загрузить картинку: {e}") from e

        logger.info(
            f"[banners-in-template] url_id={internal_url_id}, content_id={content_id}, "
            f"шаблон ad_plan={template_ad_plan_id}, групп={len(template_ad_group_ids)}"
        )

        # 3. Создаём по banner на каждую template-группу
        banner_ids: list[int] = []
        last_error: VKAdsAPIError | None = None
        for group_id in template_ad_group_ids:
            banner_payload = {
                "ad_group_id": group_id,
                "name": f"{banner_name_prefix} | {copy.title[:30]}",
                "urls": {"primary": {"id": internal_url_id}},
                "textblocks": {
                    "title_40_vkads": {"text": copy.title[:40]},
                    "text_2000": {"text": copy.text[:2000]},
                    "about_company_115": {"text": copy.about[:115]},
                    "cta_community_vk": {"text": "signUp"},
                },
                "content": {
                    "image_600x600": {"id": content_id},
                },
            }
            try:
                banner_resp = await self.vk.create_banner(banner_payload)
            except VKAdsAPIError as e:
                last_error = e
                logger.error(
                    f"Banner для группы {group_id} не создан: {e} "
                    f"({e.diag_summary()})"
                )
                continue
            banner_id = banner_resp.get("id")
            if banner_id is not None:
                banner_ids.append(int(banner_id))
                logger.info(f"Banner {banner_id} создан в группе {group_id}")

        if not banner_ids:
            # Все банеры провалились — пробрасываем последнюю ошибку с диагностикой
            raise AdCreatorError(
                f"Ни одного banner не создалось в {len(template_ad_group_ids)} группах. "
                f"Последняя ошибка: {last_error}",
                vk_error=last_error,
            )

        logger.info(
            f"✅ Создано {len(banner_ids)}/{len(template_ad_group_ids)} банеров "
            f"в шаблоне ad_plan={template_ad_plan_id}"
        )

        return CampaignSummary(
            ad_plan_id=template_ad_plan_id,
            ad_group_ids=list(template_ad_group_ids),
            banner_ids=banner_ids,
            raw=None,
        )

    @staticmethod
    def _build_ad_group(
        *,
        name: str,
        age_list: list[int],
        sex: list[str],
        geo_regions: list[int],
        budget_rub: int,
        date_start: str,
        package_id: int,
        copy: AdCopy,
        content_id: int,
        internal_url_id: int,
    ) -> dict:
        """Собрать payload одной ad_group с одним баннером внутри.

        Формат полей подтверждён рабочими n8n-кампаниями 2024-2025:
        - budget_limit_day: int рубли (не копейки, не строка)
        - budget_limit: null (обязательно)
        - date_end: null (обязательно для групп без явной даты конца)
        """
        return {
            "name": name,
            "targetings": {
                "geo": {"regions": geo_regions},
                "sex": sex,
                "age": {"age_list": age_list},
            },
            "max_price": 0,
            "budget_limit_day": budget_rub,  # int рубли
            "budget_limit": None,  # обязательный явный null
            "date_start": date_start,
            "date_end": None,  # обязательный явный null на уровне группы
            "age_restrictions": "0+",
            "package_id": package_id,
            "banners": [
                {
                    "name": f"{name} | {copy.title[:30]}",
                    "urls": {"primary": {"id": internal_url_id}},
                    "textblocks": {
                        "title_40_vkads": {"text": copy.title[:40]},
                        "text_2000": {"text": copy.text[:2000]},
                        "about_company_115": {"text": copy.about[:115]},
                        "cta_community_vk": {"text": copy.cta},
                    },
                    "content": {
                        "image_600x600": {"id": content_id},
                    },
                }
            ],
        }

    @staticmethod
    def _parse_create_response(result: dict) -> CampaignSummary:
        """Извлечь id из вложенного ответа VK."""
        ad_plan_id = result.get("id")
        if not ad_plan_id:
            raise AdCreatorError(f"В ответе VK нет id ad_plan: {result}")

        ad_group_ids = []
        banner_ids = []
        for grp in result.get("ad_groups", []):
            if grp_id := grp.get("id"):
                ad_group_ids.append(int(grp_id))
            for banner in grp.get("banners", []):
                if banner_id := banner.get("id"):
                    banner_ids.append(int(banner_id))

        return CampaignSummary(
            ad_plan_id=int(ad_plan_id),
            ad_group_ids=ad_group_ids,
            banner_ids=banner_ids,
            raw=result,
        )


# Сплит возрастов. По дефолту ОДНА группа (для тестирования с малым балансом) —
# когда баланс позволит, расширим до полного A/B сплита по 5 окон по 2 года.
# Полный сплит для будущего использования:
#   [(41, 42), (43, 44), (45, 46), (47, 48), (49, 50)]
DEFAULT_AGE_SPLITS_ORTHODOX: list[tuple[int, int]] = [(41, 50)]
