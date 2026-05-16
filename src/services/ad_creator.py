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
    # cta_community_vk — enum из banner_fields.json:
    # signUp, buy, contactUs, subscribe, message, write, visitSite, learnMore,
    # getoffer, book, enroll, askQuestion, startChat, getPrice.
    # Для package 3127 (Написать в сообщество) семантически точнее 'write' —
    # буквально «Напишите имена близких» из бизнес-модели Vizit'а.
    cta: str = "write"


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
        package_id: int = 3127,
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
            sex: ['female'] / ['male'] / оба. По умолчанию ['female'] —
                бизнес-модель Vizit'а (молитвы → ЦА женщины 41-58).
            geo_regions: ID регионов VK, по умолчанию [188] (Россия).
            package_id: тип объявления. По умолчанию 3127 «Написать в сообщество»
                (бизнес-модель — собрать имена для поминовения).
                Историческое: 3122 «Вступить в сообщество».
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
            # Бизнес-модель: молитвенная аудитория преимущественно женская
            # 41-58 (см. DEFAULT_AGE_SPLITS_ORTHODOX). Раньше было оба пола,
            # переключили на female по запросу Vizit'а 14.05.2026.
            sex = ["female"]
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

        # 2. Готовим 2 версии картинки (image_600x600 + icon_256x256) и грузим.
        # Любой формат картинки от пользователя → smart-crop в квадрат → resize.
        # БЕЗ icon_256x256 patterns для package 3122 не подбираются →
        # ошибка 'patterns must be in package settings'. Это была корневая
        # причина 25+ итераций фиксов в Phase 3.
        # Тот же подход что в create_age_split_groups_in_template_plan.
        logger.info(f"Обрабатываю и загружаю картинку ({len(image_bytes)} байт)")
        try:
            from io import BytesIO
            from PIL import Image

            src = Image.open(BytesIO(image_bytes))
            if src.mode != "RGB":
                src = src.convert("RGB")
            w, h = src.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            src_square = src.crop((left, top, left + side, top + side))

            def _bytes_at(size: int) -> bytes:
                resized = src_square.resize((size, size), Image.Resampling.LANCZOS)
                buf = BytesIO()
                resized.save(buf, format="JPEG", quality=92)
                return buf.getvalue()

            image_600_id = await self.vk.upload_image(
                _bytes_at(600), "image_600x600.jpg"
            )
            icon_256_id = await self.vk.upload_image(
                _bytes_at(256), "icon_256x256.jpg"
            )
            logger.info(
                f"Загружено: image_600x600={image_600_id}, "
                f"icon_256x256={icon_256_id} (исходник {w}x{h})"
            )
        except Exception as e:
            raise AdCreatorError(f"Не смог обработать/загрузить картинку: {e}") from e

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
        # Опциональные сегменты аудитории (например, подписчики правосл.
        # сообществ из VK Ads → Аудитории, или CSV от TargetHunter).
        # По https://ads.vk.com/doc/api/object/Targetings поле `segments` —
        # list of integer. Если settings.vk_audience_segment_ids_parsed
        # пустой — поле в payload НЕ передаётся (иначе VK расценил бы пустой
        # массив как «таргет ни на кого»).
        from src.config import settings as _settings
        audience_segments = _settings.vk_audience_segment_ids_parsed
        for age_from, age_to in age_splits:
            # age_list начинается с 0 — показывать тем чей возраст не указан
            # (по официальному гайду VK Ads "Быстрый старт"). Без 0 теряем
            # часть аудитории.
            age_list = [0] + list(range(age_from, age_to + 1))
            group_name = f"{age_from}-{age_to}"
            # Базовые таргетинги для всех групп. interests_soc_dem — OR
            # между ID: пользователь подходит если у него ХОТЯ БЫ ОДИН
            # из сигналов. ID из targetings_tree.json от Бибы.
            # Между полями (sex × age × ...) — AND, сужение даёт там.
            ad_group_targetings: dict = {
                "geo": {"regions": geo_regions},
                "sex": sex,
                "age": {"age_list": age_list},
                "interests_soc_dem": [
                    27137,  # Духовный рост и самовыражение — главный сигнал
                    10186,  # Женаты, замужем — наша ЦА
                    12920,  # Есть дети в семье — молятся за детей
                ],
                # ВАЖНО: НЕ передаём `pads` — auto-mode сам подбирает
                # площадки под package_id. При явных pads VK переходит в
                # manual-mode и требует patterns в settings package.
                # group_members тоже не передаём (это для package 3194).
            }
            if audience_segments:
                ad_group_targetings["segments"] = audience_segments
            nested_campaigns.append({
                "name": group_name,
                "targetings": ad_group_targetings,
                "max_price": 0,
                "budget_limit_day": daily_budget_rub_per_group,
                "budget_limit": None,
                "date_start": date_start,
                "date_end": None,
                "age_restrictions": "0+",
                "package_id": package_id,
                "banners": [{
                    "name": f"{group_name} | {copy.title[:30]}",
                    "urls": {"primary": {"id": internal_url_id}},
                    "textblocks": {
                        "title_40_vkads": {"text": copy.title[:40]},
                        "text_2000": {"text": copy.text[:2000]},
                        "about_company_115": {"text": copy.about[:115]},
                        # Для package 3122 (Вступить) — "signUp"
                        # Для package 3127 (Написать) — "write" / "contactUs" / "message"
                        # Для package 3194 (Вовлеченность) — другой объект banner
                        "cta_community_vk": {"text": copy.cta},
                    },
                    "content": {
                        # ОБЕ картинки обязательны для подбора pattern в
                        # package 3122. Без icon_256x256 patterns ошибка.
                        "icon_256x256": {"id": icon_256_id},
                        "image_600x600": {"id": image_600_id},
                    },
                }],
            })

        ad_plan_payload = {
            "name": campaign_name,
            "status": "active",
            "date_start": date_start,
            "date_end": date_end,
            "autobidding_mode": "max_goals",
            # CBO (Campaign Budget Optimization) — режим оптимизации бюджета
            # кампании. budget_limit_day задаётся ОБЩИЙ на кампанию, VK сам
            # распределяет между группами автоматически. Минимум: 100₽/день
            # (из UI кабинета VK, скрин 13.05.2026).
            # Подтверждено сравнением с golden_inspect_20865519.json:
            # ad_plan.budget_limit_day = 420.0 + budget_optimization_enabled=true,
            # ad_group.budget_limit_day = 420.0 (одинаковое!).
            #
            # ВАЖНО: budget_optimization_enabled — READ ONLY поле в API
            # (подтверждено ошибкой VK 13.05.2026: read_only_field). VK сам
            # включает CBO когда видит одинаковый budget_limit_day на кампании
            # и группах. Не передаём это поле в payload.
            "budget_limit_day": daily_budget_rub_per_group,
            "budget_limit": None,
            "max_price": 0,
            "objective": "socialengagement",
            "ad_object_type": "url",
            "ad_object_id": internal_url_id,
            "ad_groups": nested_campaigns,  # ← актуальное имя поля (не "campaigns")
        }

        # PRE-FLIGHT: минимум 100₽/день на кампанию (подтверждено UI кабинета VK).
        # Бюджет в CBO режиме общий, не на группу. Слишком маленькое значение —
        # VK выдаст 'min_value' на budget_limit_day групп.
        VK_MIN_CAMPAIGN_DAILY = 100  # рубли, минимум дневного бюджета кампании
        if daily_budget_rub_per_group < VK_MIN_CAMPAIGN_DAILY:
            raise AdCreatorError(
                f"Дневной бюджет кампании ({daily_budget_rub_per_group}₽) "
                f"меньше минимума VK ({VK_MIN_CAMPAIGN_DAILY}₽/день). "
                f"Увеличь TEST_CAMPAIGN_BUDGET_RUB в Railway "
                f"минимум до {VK_MIN_CAMPAIGN_DAILY}."
            )

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
            # НЕ ПЕРЕХВАТЫВАЕМ С ДОГАДКАМИ.
            # Раньше тут была интерпретация unallowed_value/min_value как
            # 'бюджет превышает баланс', но это была гипотеза без подтверждения
            # документацией. У Vizit'а был баланс 999.99₽, проект 600₽, а бот
            # всё равно говорил 'превышает баланс' — это вводило в заблуждение.
            #
            # Правило (см. INSTRUCTIONS.md): не догадываемся про причины
            # ошибок VK. Пробрасываем полный ответ VK с request_id, чтобы
            # Vizit видел реальную причину и мы могли запросить нужную
            # страницу документации.
            raise AdCreatorError(
                f"VK не принял ad_plan: {e}", vk_error=e
            ) from e
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

    async def create_age_split_groups_in_template_plan(
        self,
        *,
        image_bytes: bytes,
        copy: AdCopy,
        community_url: str,
        template_ad_plan_id: int,
        age_splits: list[tuple[int, int]],
        daily_budget_rub_per_group: int,
        package_id: int = 3127,
        banner_name_prefix: str = "bot",
        image_filename: str = "image.jpg",
    ) -> CampaignSummary:
        """Создать N новых ad_groups с banner внутри в существующей кампании.

        Этот workaround используется когда нельзя создать кампанию через
        POST /ad_plans.json из-за пустых package settings. Vizit вручную
        создал шаблонную кампанию через UI (это инициализирует patterns
        settings в package), а бот добавляет в неё новые ad_groups через
        POST /ad_groups.json (поддержка VK подтвердила: banner создаётся
        ТОЛЬКО как nested внутри ad_group, отдельный POST /banners.json
        не поддерживается — только GET/PATCH).

        Шаги:
            1. Регистрируем URL → internal url_id (для banners[].urls.primary).
            2. Загружаем картинку → content_id.
            3. Для каждого возрастного окна создаём новую ad_group через
               POST /ad_groups.json с body содержащим banners[] (один banner).

        Args:
            template_ad_plan_id: ID кампании из VK_TEMPLATE_AD_PLAN_ID.
            age_splits: список (age_from, age_to) — окна возраста.
            daily_budget_rub_per_group: бюджет на каждую группу в день.

        Returns:
            CampaignSummary с template_ad_plan_id и списками id новых
            групп и баннеров.
        """
        if not age_splits:
            raise AdCreatorError("age_splits пустой")

        # 0. Узнаём budget_limit_day шаблонной кампании. VK требует чтобы
        # budget_limit_day у новой ad_group совпадал с бюджетом ad_plan
        # (или соответствовал каким-то правилам, которые мы не знаем).
        # На дефолтном значении 100 ₽ VK возвращал unallowed_value.
        # В рабочей кампании (inspect) у всех групп budget_limit_day = 420.0
        # (как у ad_plan). Подтягиваем динамически.
        ad_plan_budget: float | int = daily_budget_rub_per_group
        try:
            plan_info = await self.vk.get_ad_plan_raw(template_ad_plan_id)
            if plan_info and plan_info.get("budget_limit_day") is not None:
                ad_plan_budget = plan_info["budget_limit_day"]
                logger.info(
                    f"[groups-in-template] подтянул budget_limit_day={ad_plan_budget} "
                    f"из ad_plan {template_ad_plan_id}"
                )
        except Exception as e:
            # Не критично — продолжаем с переданным дефолтом
            logger.warning(
                f"Не смог получить budget_limit_day у ad_plan {template_ad_plan_id}: {e}. "
                f"Использую дефолт {daily_budget_rub_per_group}."
            )

        # 1. Регистрируем URL
        try:
            url_info = await self.vk.get_or_register_url(community_url)
            internal_url_id = int(url_info["id"])
        except Exception as e:
            raise AdCreatorError(
                f"Не смог зарегистрировать URL {community_url}: {e}"
            ) from e

        # 2. Готовим 2 версии картинки и грузим в VK Ads.
        # PIL делает smart-crop в квадрат + resize до нужного размера.
        # На входе бот получает любой формат от пользователя (iPhone 4032x3024,
        # горизонтальные, вертикальные, квадраты) — на выходе ровно 600x600
        # и 256x256, как требует VK (поля image_600x600 и icon_256x256 в
        # banner_fields имеют строгие размеры min == max).
        try:
            from io import BytesIO
            from PIL import Image

            src = Image.open(BytesIO(image_bytes))
            if src.mode != "RGB":
                src = src.convert("RGB")
            # Smart-crop по центру в квадрат
            w, h = src.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            src_square = src.crop((left, top, left + side, top + side))

            def _bytes_at(size: int) -> bytes:
                resized = src_square.resize((size, size), Image.Resampling.LANCZOS)
                buf = BytesIO()
                resized.save(buf, format="JPEG", quality=92)
                return buf.getvalue()

            image_600_id = await self.vk.upload_image(
                _bytes_at(600), "image_600x600.jpg"
            )
            icon_256_id = await self.vk.upload_image(
                _bytes_at(256), "icon_256x256.jpg"
            )
            logger.info(
                f"Загружено: image_600x600={image_600_id}, icon_256x256={icon_256_id} "
                f"(исходник {w}x{h} → smart-crop {side}x{side})"
            )
        except Exception as e:
            raise AdCreatorError(f"Не смог обработать/загрузить картинку: {e}") from e

        logger.info(
            f"[groups-in-template] template={template_ad_plan_id}, "
            f"url_id={internal_url_id}, image_600={image_600_id}, "
            f"icon_256={icon_256_id}, возрастных групп={len(age_splits)}"
        )

        # 3. Создаём по одной ad_group на каждое возрастное окно
        ad_group_ids: list[int] = []
        banner_ids: list[int] = []
        last_error: VKAdsAPIError | None = None

        # Опциональные сегменты аудитории — те же что и в create_age_split_campaign.
        # По https://ads.vk.com/doc/api/object/Targetings — `segments` это
        # list of integer, поле в Targetings объекте. Если в env пусто —
        # ключ не передаётся (пустой массив VK воспринял бы как «никаких
        # сегментов» = таргет ни на кого).
        from src.config import settings as _settings
        audience_segments = _settings.vk_audience_segment_ids_parsed

        for age_from, age_to in age_splits:
            group_name = f"{age_from}-{age_to}"
            age_list = list(range(age_from, age_to + 1))

            # Базовые таргетинги. interests_soc_dem — OR-логика между ID:
            # пользователь попадает в аудиторию если у него ХОТЯ БЫ ОДИН
            # из этих сигналов. Между разными полями (sex × age × ...) —
            # AND-логика. Поэтому расширяем сигналы здесь, а сужение даёт
            # пол/возраст/гео. ID взяты из targetings_tree.json от Бибы.
            ad_group_targetings: dict = {
                "geo": {"regions": [188]},  # 188 = Россия
                "sex": ["female"],
                # age_list начинается с 0 (по официальному гайду VK Ads):
                # 0 = показывать тем, чей возраст не определён
                "age": {"age_list": [0] + age_list},
                "interests_soc_dem": [
                    27137,  # Духовный рост и самовыражение — главный сигнал
                    10186,  # Женаты, замужем — наша ЦА (женщины 41-58)
                    12920,  # Есть дети в семье — молятся за детей
                ],
            }
            if audience_segments:
                ad_group_targetings["segments"] = audience_segments

            ad_group_payload = {
                "ad_plan_id": template_ad_plan_id,
                "name": group_name,
                "status": "active",
                "budget_limit_day": ad_plan_budget,
                "budget_limit": None,
                "max_price": 0,
                "package_id": package_id,
                "age_restrictions": "0+",
                "targetings": ad_group_targetings,
                "banners": [
                    # Структура banner по официальному гайду VK Ads.
                    # CTA берётся из copy.cta (раньше был захардкожен "signUp").
                    # Для package 3127 (Написать) Claude генерит cta="write" —
                    # семантически совпадает с бизнес-моделью «Напишите имена».
                    {
                        "name": f"{banner_name_prefix} | {group_name} | {copy.title[:25]}",
                        "urls": {"primary": {"id": internal_url_id}},
                        "content": {
                            "icon_256x256": {"id": icon_256_id},
                            "image_600x600": {"id": image_600_id},
                        },
                        "textblocks": {
                            "title_40_vkads": {"text": copy.title[:40]},
                            "text_2000": {"text": copy.text[:2000]},
                            "about_company_115": {"text": copy.about[:115]},
                            "cta_community_vk": {"text": copy.cta},
                        },
                    }
                ],
            }

            try:
                group_resp = await self.vk.create_ad_group(ad_group_payload)
            except VKAdsAPIError as e:
                last_error = e
                logger.error(
                    f"ad_group {group_name} не создана: {e} ({e.diag_summary()})"
                )
                continue

            gid = group_resp.get("id")
            if gid is not None:
                ad_group_ids.append(int(gid))
                # banners[] в ответе содержит созданные banner'ы
                resp_banners = group_resp.get("banners") or []
                for b in resp_banners:
                    bid = b.get("id")
                    if bid is not None:
                        banner_ids.append(int(bid))
                logger.info(
                    f"✅ ad_group {gid} ({group_name}) создана с "
                    f"{len(resp_banners)} banner'ом"
                )

        if not ad_group_ids:
            raise AdCreatorError(
                f"Ни одной группы не создалось в {len(age_splits)} окнах. "
                f"Последняя ошибка: {last_error}",
                vk_error=last_error,
            )

        logger.info(
            f"✅ Создано {len(ad_group_ids)}/{len(age_splits)} групп + "
            f"{len(banner_ids)} banner в шаблоне ad_plan={template_ad_plan_id}"
        )

        return CampaignSummary(
            ad_plan_id=template_ad_plan_id,
            ad_group_ids=ad_group_ids,
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

    async def create_smoke_test_campaign(
        self,
        *,
        image_bytes: bytes,
        copies: list[AdCopy],
        community_url: str,
        daily_budget_rub: int = 500,
        days_duration: int = 3,
        age_min: int = 41,
        age_max: int = 58,
        sex: list[str] | None = None,
        geo_regions: list[int] | None = None,
        package_id: int = 3127,
        campaign_name: str | None = None,
        image_filename: str = "smoke_test.jpg",
    ) -> CampaignSummary:
        """Smoke test — одна кампания, одна ad_group, 1-2 баннера для A/B.

        Phase 5.9 (16.05.2026 утром): отдельный метод для технической
        проверки цепочки на минимальном бюджете. Отличия от
        create_age_split_campaign:

        - **Одна ad_group** на весь возрастной диапазон 41-58
          (не 5 групп split). Причина: сегмент 80507749 уже сегментирован
          (TargetHunter горячие 619k), дробить узкую базу на 5 ещё более
          узких = размазать без статзначимости.
        - **N баннеров в одной ad_group** для A/B текстов (Боба:
          «один адсет, A/B на текстах внутри»).
        - **Один общий бюджет** на день (не на группу).
        - **Default package 3127** «Написать сообщение» — цель smoke test
          собрать сообщения с именами для поминовения.
        - Сегмент 80507749 подключается **автоматически через env**
          VK_AUDIENCE_SEGMENT_IDS (как в create_age_split_campaign).

        Args:
            image_bytes: байты изображения (обычно JPEG из Telegram).
            copies: 1-2 объекта AdCopy для A/B. Каждый = отдельный баннер.
            community_url: https://vk.com/pomolimsy.
            daily_budget_rub: дневной бюджет в рублях (минимум 100).
            days_duration: длительность кампании в днях (для smoke test = 3).
            age_min, age_max: возраст ЦА (по умолчанию 41-58).
            sex: ['female'] по умолчанию.
            geo_regions: [188] (Россия) по умолчанию.
            package_id: 3127 («Написать сообщение») по умолчанию.
            campaign_name: кастомное имя кампании в кабинете VK Ads.
            image_filename: имя файла для multipart upload.

        Returns:
            CampaignSummary с ad_plan_id, ad_group_ids, banner_ids.

        Raises:
            AdCreatorError: на любой провал.
        """
        # Валидация
        if not copies:
            raise AdCreatorError("copies пустой — нужен хотя бы один AdCopy")
        if len(copies) > 4:
            raise AdCreatorError(
                f"Слишком много креативов ({len(copies)}). Для smoke test "
                f"максимум 4 — иначе бюджет 500₽/день не наберёт статзначимости "
                f"на каждом."
            )
        if daily_budget_rub < 100:
            raise AdCreatorError(
                f"Дневной бюджет {daily_budget_rub}₽ меньше минимума VK (100₽/день)"
            )

        # Defaults
        if sex is None:
            sex = ["female"]
        if geo_regions is None:
            geo_regions = [188]  # Россия
        if campaign_name is None:
            from datetime import datetime
            campaign_name = (
                f"smoke_test {datetime.now().strftime('%Y-%m-%d')} "
                f"({len(copies)} креатива)"
            )

        # 1. Регистрируем URL в кабинете
        logger.info(f"[smoke 1/3] Регистрирую URL: {community_url}")
        try:
            url_info = await self.vk.get_or_register_url(community_url)
            internal_url_id = int(url_info["id"])
        except Exception as e:
            raise AdCreatorError(
                f"Не смог зарегистрировать URL {community_url}: {e}"
            ) from e

        # 2. Готовим картинку (icon_256 + image_600 — обе обязательны)
        logger.info(f"[smoke 2/3] Обрабатываю и загружаю картинку ({len(image_bytes)} байт)")
        try:
            from io import BytesIO
            from PIL import Image

            src = Image.open(BytesIO(image_bytes))
            if src.mode != "RGB":
                src = src.convert("RGB")
            w, h = src.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            src_square = src.crop((left, top, left + side, top + side))

            def _bytes_at(size: int) -> bytes:
                resized = src_square.resize((size, size), Image.Resampling.LANCZOS)
                buf = BytesIO()
                resized.save(buf, format="JPEG", quality=90)
                return buf.getvalue()

            image_600_bytes = _bytes_at(600)
            icon_256_bytes = _bytes_at(256)

            image_600_id = await self.vk.upload_image(
                image_600_bytes, "image_600x600.jpg"
            )
            icon_256_id = await self.vk.upload_image(
                icon_256_bytes, "icon_256x256.jpg"
            )
        except Exception as e:
            raise AdCreatorError(f"Не смог обработать/загрузить картинку: {e}") from e

        # 3. Targetings — единые для всех баннеров (один ad_group)
        from src.config import settings as _settings

        age_list = list(range(age_min, age_max + 1))
        audience_segments = _settings.vk_audience_segment_ids_parsed

        ad_group_targetings: dict = {
            "geo": {"regions": geo_regions},
            "sex": sex,
            "age": {"age_list": age_list},
            "interests_soc_dem": [
                27137,  # Духовный рост и самовыражение
                10186,  # Замужем
                12920,  # Есть дети
            ],
        }
        if audience_segments:
            ad_group_targetings["segments"] = audience_segments
            logger.info(
                f"[smoke] Подключаю remarketing-сегменты: {audience_segments}"
            )
        else:
            logger.warning(
                "[smoke] VK_AUDIENCE_SEGMENT_IDS не задан в env — кампания "
                "пойдёт на широкую ЦА без сегмента. Это ОК для теста UI."
            )

        # 4. Баннеры — по одному на AdCopy
        banners = []
        for idx, copy in enumerate(copies, start=1):
            banner_label = f"A" if idx == 1 else f"B" if idx == 2 else str(idx)
            banners.append({
                "name": f"smoke {banner_label} | {copy.title[:30]}",
                "urls": {"primary": {"id": internal_url_id}},
                "textblocks": {
                    "title_40_vkads": {"text": copy.title[:40]},
                    "text_2000": {"text": copy.text[:2000]},
                    "about_company_115": {"text": copy.about[:115]},
                    "cta_community_vk": {"text": copy.cta},
                },
                "content": {
                    "icon_256x256": {"id": icon_256_id},
                    "image_600x600": {"id": image_600_id},
                },
            })

        # 5. ad_group
        from datetime import datetime, timedelta
        date_start = datetime.now().strftime("%Y-%m-%d")
        date_end = (datetime.now() + timedelta(days=days_duration)).strftime("%Y-%m-%d")

        ad_group = {
            "name": f"smoke ж{age_min}-{age_max} сегмент",
            "targetings": ad_group_targetings,
            "max_price": 0,
            "budget_limit_day": daily_budget_rub,
            "budget_limit": None,
            "date_start": date_start,
            "date_end": None,
            "age_restrictions": "0+",
            "package_id": package_id,
            "banners": banners,
        }

        # 6. ad_plan
        ad_plan_payload = {
            "name": campaign_name,
            "status": "active",
            "date_start": date_start,
            "date_end": date_end,
            "autobidding_mode": "max_goals",
            "budget_limit_day": daily_budget_rub,
            "budget_limit": daily_budget_rub * days_duration,  # общий лимит
            "max_price": 0,
            "objective": "socialengagement",
            "ad_object_type": "url",
            "ad_object_id": internal_url_id,
            "ad_groups": [ad_group],  # одна группа
        }

        logger.info(
            f"[smoke 3/3] POST /ad_plans: {campaign_name}, "
            f"1 группа, {len(banners)} баннеров, бюджет {daily_budget_rub}₽/день × "
            f"{days_duration} дн = {daily_budget_rub * days_duration}₽ всего"
        )

        try:
            response = await self.vk.create_ad_plan(ad_plan_payload)
        except VKAdsAPIError as e:
            raise AdCreatorError(
                f"VK не принял smoke ad_plan: {e}", vk_error=e
            ) from e
        except Exception as e:
            raise AdCreatorError(f"VK не принял smoke ad_plan: {e}") from e

        # Парсим ответ
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
            f"✅ Smoke test кампания создана: ad_plan_id={ad_plan_id}, "
            f"группа={ad_group_ids[0] if ad_group_ids else '?'}, "
            f"баннеров={len(banner_ids)}"
        )

        return CampaignSummary(
            ad_plan_id=ad_plan_id,
            ad_group_ids=ad_group_ids,
            banner_ids=banner_ids,
            raw=response,
        )


# Сплит возрастов. По дефолту ОДНА группа (для тестирования с малым балансом) —
# когда баланс позволит, расширим до полного A/B сплита по 5 окон по 2 года.
# Полный сплит для будущего использования:
#   [(41, 42), (43, 44), (45, 46), (47, 48), (49, 50)]
DEFAULT_AGE_SPLITS_ORTHODOX: list[tuple[int, int]] = [
    (41, 43),
    (44, 46),
    (47, 49),
    (50, 52),
    (53, 55),
    (56, 58),
]
