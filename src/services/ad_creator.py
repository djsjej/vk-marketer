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

from src.vk_ads.client import VKAdsClient

logger = logging.getLogger(__name__)


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
    """Ошибка при создании кампании."""


class AdCreator:
    """Оркестратор создания рекламных кампаний с возрастным A/B сплитом."""

    def __init__(self, vk_client: VKAdsClient):
        self.vk = vk_client

    async def create_age_split_campaign(
        self,
        image_bytes: bytes,
        theme: str,
        copy: AdCopy,
        community_url_id: int,
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
            image_bytes: байты картинки (jpeg/png), из которой делаем баннер.
                         VK требует разные размеры (256x256, 600x600, 1080x607);
                         в MVP грузим один файл и используем как есть для image_600x600.
                         Полный мульти-размер будет в Phase 4.
            theme: краткое описание темы (для имени кампании в кабинете).
            copy: тексты объявления (title, text, about, cta).
            community_url_id: VK ID сообщества (число из URL после /club или
                              из настроек кабинета — uniq id "ad_object_id").
            age_splits: список окон возраста, например [(41,42), (43,44), (45,46)].
                        На каждое окно создастся своя ad_group со своим баннером.
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

        # 1. Грузим картинку → content_id
        logger.info(f"Загружаю картинку ({len(image_bytes)} байт) в VK")
        try:
            content_id = await self.vk.upload_image(
                image_bytes=image_bytes, filename=image_filename
            )
        except Exception as e:
            raise AdCreatorError(f"Не смог загрузить картинку: {e}") from e

        # 2. Собираем payload ad_plan с вложенными группами и баннерами
        today = date.today()
        end_date = today + timedelta(days=days_duration)
        date_start = today.isoformat()
        date_end = end_date.isoformat()

        # Имя кампании — короткое, чтобы было видно в кабинете
        safe_theme = theme[:50].strip()
        campaign_name = f"{campaign_name_prefix} | {safe_theme}"

        ad_groups_payload = []
        for age_from, age_to in age_splits:
            age_list = list(range(age_from, age_to + 1))
            group_name = f"{age_from}-{age_to}"
            ad_groups_payload.append(self._build_ad_group(
                name=group_name,
                age_list=age_list,
                sex=sex,
                geo_regions=geo_regions,
                budget_rub=daily_budget_rub_per_group,
                date_start=date_start,
                package_id=package_id,
                copy=copy,
                content_id=content_id,
                community_url_id=community_url_id,
            ))

        ad_plan_payload = {
            "name": campaign_name,
            "status": "active",
            "date_start": date_start,
            "date_end": date_end,
            "autobidding_mode": "max_goals",
            "budget_limit_day": daily_budget_rub_per_group * len(age_splits),
            "max_price": 0,
            "objective": "socialengagement",
            "ad_object_id": community_url_id,
            "ad_object_type": "url",
            "ad_groups": ad_groups_payload,
        }

        logger.info(
            f"POST /ad_plans.json: {campaign_name}, "
            f"{len(age_splits)} групп, "
            f"бюджет {ad_plan_payload['budget_limit_day']} ₽/день"
        )

        # 3. Один POST на всю иерархию
        try:
            result = await self.vk.create_ad_plan(ad_plan_payload)
        except Exception as e:
            raise AdCreatorError(f"VK не принял ad_plan: {e}") from e

        # 4. Парсим ответ
        return self._parse_create_response(result)

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
        community_url_id: int,
    ) -> dict:
        """Собрать payload одной ad_group с одним баннером внутри."""
        return {
            "name": name,
            "targetings": {
                "geo": {"regions": geo_regions},
                "sex": sex,
                "age": {"age_list": age_list},
            },
            "max_price": 0,
            "budget_limit_day": budget_rub,
            "date_start": date_start,
            "age_restrictions": "0+",
            "package_id": package_id,
            "banners": [
                {
                    "name": f"{name} | {copy.title[:30]}",
                    "urls": {"primary": {"id": community_url_id}},
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


# Стандартный возрастной сплит для православной аудитории Зеленецкого/Спиридона —
# по предыдущим тестам пользователя в нише. 5 окон по 2 года, что даёт
# гранулярность для определения «горячего» возраста.
DEFAULT_AGE_SPLITS_ORTHODOX: list[tuple[int, int]] = [
    (41, 42),
    (43, 44),
    (45, 46),
    (47, 48),
    (49, 50),
]
