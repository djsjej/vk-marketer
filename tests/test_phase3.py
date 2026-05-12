"""Тесты Phase 3: загрузка картинок, оркестратор AdCreator, копирайтер fallback."""

import httpx
import pytest
import respx

from src.claude_brain.copywriter import fallback_copy_from_caption
from src.services.ad_creator import (
    DEFAULT_AGE_SPLITS_ORTHODOX,
    AdCopy,
    AdCreator,
    AdCreatorError,
    CampaignSummary,
)
from src.vk_ads.auth import OAUTH_URL, VKAdsAuthenticator
from src.vk_ads.client import VK_API_BASE, VKAdsClient
from src.vk_ads.upload import (
    VK_CONTENT_STATIC_URL,
    VKContentUploadError,
    upload_image_bytes,
)


def _real_image_bytes(width: int = 600, height: int = 600) -> bytes:
    """Реальная PNG картинка в bytes — для тестов где AdCreator делает PIL.open.
    Используется вместо фейковых b"img" которые PIL не может разпарсить."""
    from io import BytesIO
    from PIL import Image

    img = Image.new("RGB", (width, height), (200, 100, 50))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# upload.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_upload_image_bytes_returns_full_response():
    respx.post(VK_CONTENT_STATIC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 21506470,
                "variants": {
                    "original": {"url": "https://r.mradx.net/img/x.jpg",
                                 "height": 600, "width": 600, "size": 14407}
                },
            },
        )
    )

    result = await upload_image_bytes(
        access_token="tk", image_bytes=b"fakeimage", filename="test.jpg"
    )
    assert result["id"] == 21506470
    assert "variants" in result


@pytest.mark.asyncio
async def test_upload_image_bytes_rejects_empty_bytes():
    with pytest.raises(VKContentUploadError, match="Пустые"):
        await upload_image_bytes(access_token="tk", image_bytes=b"")


@pytest.mark.asyncio
@respx.mock
async def test_upload_image_bytes_raises_on_4xx():
    respx.post(VK_CONTENT_STATIC_URL).mock(
        return_value=httpx.Response(400, text="bad request")
    )

    with pytest.raises(VKContentUploadError, match="400"):
        await upload_image_bytes(access_token="tk", image_bytes=b"x")


@pytest.mark.asyncio
@respx.mock
async def test_upload_image_bytes_raises_when_no_id_in_response():
    respx.post(VK_CONTENT_STATIC_URL).mock(
        return_value=httpx.Response(200, json={"variants": {}})
    )

    with pytest.raises(VKContentUploadError, match="нет id"):
        await upload_image_bytes(access_token="tk", image_bytes=b"x")


@pytest.mark.asyncio
@respx.mock
async def test_upload_image_sends_bearer_header():
    route = respx.post(VK_CONTENT_STATIC_URL).mock(
        return_value=httpx.Response(200, json={"id": 1, "variants": {}})
    )

    await upload_image_bytes(access_token="abc123", image_bytes=b"x")
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer abc123"


@pytest.mark.asyncio
@respx.mock
async def test_client_upload_image_uses_oauth_token():
    """VKAdsClient.upload_image должен дернуть OAuth и подставить токен."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "oauth_tk", "expires_in": 86400}
        )
    )
    upload_route = respx.post(VK_CONTENT_STATIC_URL).mock(
        return_value=httpx.Response(200, json={"id": 999, "variants": {}})
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    content_id = await client.upload_image(b"img", filename="x.jpg")
    assert content_id == 999

    request = upload_route.calls[0].request
    assert request.headers["authorization"] == "Bearer oauth_tk"


# ---------------------------------------------------------------------------
# copywriter.py — fallback
# ---------------------------------------------------------------------------


def test_fallback_copy_from_caption_uses_first_line_as_title():
    copy = fallback_copy_from_caption(
        "Молитвы за здравие\n\nВечное поминовение в монастыре..."
    )
    assert copy.title == "Молитвы за здравие"
    assert "Вечное" in copy.text


def test_fallback_copy_truncates_title_to_40_chars():
    long = "x" * 100
    copy = fallback_copy_from_caption(long)
    assert len(copy.title) == 40


def test_fallback_copy_handles_empty_caption():
    copy = fallback_copy_from_caption("")
    assert copy.title  # не пусто
    assert copy.text  # не пусто
    assert copy.cta == "signUp"


# ---------------------------------------------------------------------------
# ad_creator.py — payload и парсинг
# ---------------------------------------------------------------------------


def test_default_age_splits_orthodox_has_6_windows_by_3_years():
    """Дефолт — 6 возрастных окон по 3 года (41-43, 44-46, ..., 56-58).
    Соответствует template-кампании, которую Vizit настроил в UI кабинета.
    """
    assert len(DEFAULT_AGE_SPLITS_ORTHODOX) == 6
    for age_from, age_to in DEFAULT_AGE_SPLITS_ORTHODOX:
        assert age_to - age_from == 2, f"окно {age_from}-{age_to} не 3 года"


def test_build_ad_group_includes_age_list():
    grp = AdCreator._build_ad_group(
        name="41-42",
        age_list=[41, 42],
        sex=["female"],
        geo_regions=[188],
        budget_rub=200,
        date_start="2026-05-10",
        package_id=3122,
        copy=AdCopy(title="Т", text="Текст", about="О нас"),
        content_id=999,
        internal_url_id=12345,
    )
    assert grp["targetings"]["age"]["age_list"] == [41, 42]
    assert grp["targetings"]["sex"] == ["female"]
    assert grp["budget_limit_day"] == 200  # int рубли
    assert grp["banners"][0]["content"]["image_600x600"]["id"] == 999


def test_build_ad_group_truncates_long_text():
    long_title = "X" * 100
    long_text = "Y" * 3000
    long_about = "Z" * 200
    grp = AdCreator._build_ad_group(
        name="t",
        age_list=[40],
        sex=["male"],
        geo_regions=[188],
        budget_rub=100,
        date_start="2026-05-10",
        package_id=3122,
        copy=AdCopy(title=long_title, text=long_text, about=long_about),
        content_id=1,
        internal_url_id=1,
    )
    banner = grp["banners"][0]
    assert len(banner["textblocks"]["title_40_vkads"]["text"]) == 40
    assert len(banner["textblocks"]["text_2000"]["text"]) == 2000
    assert len(banner["textblocks"]["about_company_115"]["text"]) == 115


def test_parse_create_response_extracts_ids():
    summary = AdCreator._parse_create_response({
        "id": 100,
        "name": "test",
        "ad_groups": [
            {"id": 200, "banners": [{"id": 300}, {"id": 301}]},
            {"id": 201, "banners": [{"id": 302}]},
        ],
    })
    assert summary.ad_plan_id == 100
    assert summary.ad_group_ids == [200, 201]
    assert summary.banner_ids == [300, 301, 302]


def test_parse_create_response_raises_when_no_id():
    with pytest.raises(AdCreatorError, match="нет id"):
        AdCreator._parse_create_response({"ad_groups": []})


@pytest.mark.asyncio
@respx.mock
async def test_create_age_split_campaign_full_flow():
    """End-to-end: URL registration + image upload (2 размера: 600 + 256)
    + single nested POST /ad_plans.json с правильным banner payload."""
    import json as _json

    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get("https://ads.vk.com/api/v1/urls/").mock(
        return_value=httpx.Response(
            200,
            json={"id": 12345, "url": "https://vk.com/test", "url_object_id": 67890},
        )
    )
    # Картинка грузится 2 раза (image_600x600 + icon_256x256) — мок возвращает
    # последовательно 2 разных id чтобы можно было проверить что оба используются.
    upload_counter = {"i": 0}

    def _upload_resp(request):
        upload_counter["i"] += 1
        return httpx.Response(200, json={"id": 500 + upload_counter["i"]})

    respx.post(VK_CONTENT_STATIC_URL).mock(side_effect=_upload_resp)
    ad_plan_route = respx.post(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 7000,
                "ad_groups": [
                    {"id": 7100, "banners": [{"id": 7110}]},
                    {"id": 7200, "banners": [{"id": 7210}]},
                ],
            },
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    creator = AdCreator(client)

    summary = await creator.create_age_split_campaign(
        image_bytes=_real_image_bytes(),
        theme="test theme",
        copy=AdCopy(title="Т", text="Длинный текст", about="О"),
        community_url="https://vk.com/test",
        age_splits=[(41, 42), (43, 44)],
        daily_budget_rub_per_group=200,
    )

    assert summary.ad_plan_id == 7000
    assert summary.ad_group_ids == [7100, 7200]
    assert summary.banner_ids == [7110, 7210]
    assert ad_plan_route.call_count == 1
    # Картинка загружалась 2 раза (image_600x600 + icon_256x256)
    assert upload_counter["i"] == 2

    # Проверяем тело POST'а — структура по официальному гайду
    body = _json.loads(ad_plan_route.calls[0].request.read())
    # ad_groups = массив групп (актуальное имя поля)
    assert "ad_groups" in body
    assert body["ad_object_type"] == "url"
    assert body["ad_object_id"] == 12345
    # Banner должен иметь обе картинки и все 4 textblock
    banner = body["ad_groups"][0]["banners"][0]
    assert "icon_256x256" in banner["content"], "без icon — patterns ошибка"
    assert "image_600x600" in banner["content"]
    assert "title_40_vkads" in banner["textblocks"]
    assert "text_2000" in banner["textblocks"]
    assert "about_company_115" in banner["textblocks"]
    assert "cta_community_vk" in banner["textblocks"]
    # age_list начинается с 0 — для тех чей возраст не указан
    assert body["ad_groups"][0]["targetings"]["age"]["age_list"][0] == 0


@pytest.mark.asyncio
@respx.mock
async def test_payload_uses_auto_placement_mode():
    """Regression: payload должен оставлять VK в auto-режиме выбора площадок.

    Phase 3 разобрался: workaround = НЕ передавать pads и group_members.
    VK сам подбирает площадки под package_id 3122.

    Также проверяем что:
    - `patterns` не передаётся (поле в API не существует)
    - `blocked_patterns` не передаётся (тоже)
    - `pads` не передаётся (auto-mode)
    - icon_256x256 ОБЯЗАТЕЛЬНО присутствует
    """
    import json

    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get("https://ads.vk.com/api/v1/urls/").mock(
        return_value=httpx.Response(
            200,
            json={"id": 12345, "url": "https://vk.com/test", "url_object_id": 67890},
        )
    )
    upload_counter = {"i": 0}

    def _upload_resp(request):
        upload_counter["i"] += 1
        return httpx.Response(200, json={"id": 500 + upload_counter["i"]})

    respx.post(VK_CONTENT_STATIC_URL).mock(side_effect=_upload_resp)
    ad_plan_route = respx.post(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(
            200,
            json={"id": 7000, "ad_groups": [{"id": 7100, "banners": [{"id": 7110}]}]},
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    creator = AdCreator(client)
    await creator.create_age_split_campaign(
        image_bytes=_real_image_bytes(),
        theme="t",
        copy=AdCopy(title="T", text="T", about="T"),
        community_url="https://vk.com/test",
        age_splits=[(41, 42)],
        daily_budget_rub_per_group=200,
    )

    body = json.loads(ad_plan_route.calls[0].request.read())
    # Поле должно называться ad_groups (актуальное имя из инструкции VK)
    assert "ad_groups" in body, "поле должно называться ad_groups"
    assert "campaigns" not in body, "campaigns — легаси-имя"

    ad_group = body["ad_groups"][0]
    banner = ad_group["banners"][0]
    targetings = ad_group["targetings"]

    # 1) patterns/blocked_patterns не передаются — это устаревшие гипотезы
    assert "patterns" not in banner
    assert "patterns" not in ad_group
    assert "patterns" not in targetings
    assert "blocked_patterns" not in banner, (
        "blocked_patterns был неработающей гипотезой — убран"
    )

    # 2) pads не передаём — VK сам выбирает площадки в auto-режиме
    assert "pads" not in targetings

    # 3) group_members не передаётся — это поле для package_id 3194
    # (Повысить вовлечённость), не для нашего 3122 (Вступить в сообщество)
    assert "group_members" not in targetings, (
        "group_members не нужен для package 3122 — оставляем VK решать"
    )

    # 4) Banner должен иметь icon_256x256 — без него ошибка
    # 'patterns must be in package settings'
    assert "icon_256x256" in banner["content"]
    assert "image_600x600" in banner["content"]


@pytest.mark.asyncio
async def test_create_age_split_campaign_rejects_empty_splits():
    client = VKAdsClient(static_token="x")
    creator = AdCreator(client)
    with pytest.raises(AdCreatorError, match="age_splits"):
        await creator.create_age_split_campaign(
            image_bytes=b"x",
            theme="t",
            copy=AdCopy(title="T", text="T", about="T"),
            community_url="https://vk.com/test",
            age_splits=[],
        )


# ---------------------------------------------------------------------------
# Управление статусом (pause/resume/budget)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_pause_ad_plan_sends_blocked_status():
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    route = respx.post(f"{VK_API_BASE}/ad_plans/100.json").mock(
        return_value=httpx.Response(200, json={"id": 100, "status": "blocked"})
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    await client.pause_ad_plan(100)

    body = route.calls[0].request.read().decode()
    assert "blocked" in body


@pytest.mark.asyncio
async def test_update_budget_requires_at_least_one_param():
    client = VKAdsClient(static_token="x")
    with pytest.raises(ValueError, match="хотя бы один"):
        await client.update_ad_plan_budget(1)




@pytest.mark.asyncio
@respx.mock
async def test_template_flow_creates_groups_with_banners_in_existing_plan():
    """Workaround-флоу с правильным banner-payload по официальному гайду VK Ads:
    icon_256x256 + image_600x600 + 4 textblocks с правильными именами полей
    (title_40_vkads, text_2000, about_company_115, cta_community_vk=signUp).
    """
    import json
    from io import BytesIO
    from PIL import Image

    # Реальная картинка в bytes — без неё PIL.open падает
    test_img = Image.new("RGB", (600, 600), (200, 100, 50))
    buf = BytesIO()
    test_img.save(buf, format="JPEG")
    real_image_bytes = buf.getvalue()

    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get("https://ads.vk.com/api/v1/urls/").mock(
        return_value=httpx.Response(
            200,
            json={"id": 12345, "url": "https://vk.com/test", "url_object_id": 67890},
        )
    )
    # GET /ad_plans.json для подтягивания budget_limit_day у template-кампании
    respx.get(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": 20865519, "budget_limit_day": 420.0}], "count": 1},
        )
    )
    # Image upload: image_600x600 и icon_256x256 — 2 разных id
    upload_counter = {"i": 0}
    def _upload_response(request):
        upload_counter["i"] += 1
        return httpx.Response(200, json={"id": 500 + upload_counter["i"]})
    respx.post(VK_CONTENT_STATIC_URL).mock(side_effect=_upload_response)
    # ad_plans.json POST НЕ должен вызываться
    ad_plans_route = respx.post(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(500, text="не должно вызываться")
    )
    # banners.json НЕ должен вызываться (поддержка сказала: только GET/PATCH)
    banners_route = respx.post(f"{VK_API_BASE}/banners.json").mock(
        return_value=httpx.Response(500, text="не должно вызываться")
    )
    call_idx = {"i": 0}

    def _ad_group_response(request):
        call_idx["i"] += 1
        return httpx.Response(
            200,
            json={
                "id": 8000 + call_idx["i"],
                "banners": [{"id": 9000 + call_idx["i"]}],
            },
        )

    ad_groups_route = respx.post(f"{VK_API_BASE}/ad_groups.json").mock(
        side_effect=_ad_group_response
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    creator = AdCreator(client)
    summary = await creator.create_age_split_groups_in_template_plan(
        image_bytes=real_image_bytes,
        copy=AdCopy(title="Заголовок", text="Описание", about="О компании"),
        community_url="https://vk.com/test",
        template_ad_plan_id=20865519,
        age_splits=[(41, 43), (44, 46), (47, 49)],
        daily_budget_rub_per_group=200,
    )

    # 1) ad_plans.json POST не дёргался (используем шаблон, не создаём кампанию)
    assert ad_plans_route.call_count == 0
    # 2) banners.json не дёргался (создание banner только nested в ad_group)
    assert banners_route.call_count == 0
    # 3) ad_groups.json вызван по разу на каждое окно
    assert ad_groups_route.call_count == 3
    # 4) Загрузка картинки прошла 2 раза на ВСЁ выполнение метода:
    #    image_600x600 + icon_256x256 (грузятся один раз, не по разу на группу)
    assert upload_counter["i"] == 2

    # 5) Каждый payload — flat, с правильным набором полей banner
    for i in range(3):
        body = json.loads(ad_groups_route.calls[i].request.read())
        assert "ad_groups" not in body, "payload должен быть flat"
        assert body["ad_plan_id"] == 20865519
        # age_list начинается с 0 (по официальному гайду VK Ads)
        assert body["targetings"]["age"]["age_list"][0] == 0

        banner = body["banners"][0]
        # Имена textblock полей строго по официальному гайду
        assert "title_40_vkads" in banner["textblocks"]
        assert "text_2000" in banner["textblocks"]
        assert "about_company_115" in banner["textblocks"]
        assert banner["textblocks"]["cta_community_vk"]["text"] == "signUp"
        # 2 картинки в content
        assert "icon_256x256" in banner["content"]
        assert "image_600x600" in banner["content"]
        # url из /api/v1/urls/
        assert banner["urls"]["primary"]["id"] == 12345

    # 6) Summary
    assert summary.ad_plan_id == 20865519
    assert summary.ad_group_ids == [8001, 8002, 8003]
    assert summary.banner_ids == [9001, 9002, 9003]


# --- Phase 4: Statistics API v3 ---

@pytest.mark.asyncio
@respx.mock
async def test_get_statistics_v3_ad_plans_basic():
    """v3 statistics: правильный URL и параметры в запросе."""
    import json as _json

    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tk", "expires_in": 86400})
    )
    # Важно: v3 endpoint, не v2!
    route = respx.get("https://ads.vk.com/api/v3/statistics/ad_plans/day.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 20865519,
                        "total": {
                            "base": {
                                "shows": 1500, "clicks": 12,
                                "ctr": 0.8, "spent": "350.00",
                                "cpm": "233.33", "cpc": "29.17",
                            },
                            "events": {"joinings": 2, "likes": 5, "moving_into_group": 8},
                            "uniques": {"total": 1250, "frequency": 1.2},
                            "social_network": {"vk_join": 2, "result_join": 2},
                        },
                    }
                ],
                "total": {"base": {"shows": 1500, "clicks": 12, "ctr": 0.8, "spent": "350.00"}},
                "limit": 50, "offset": 0, "count": 1,
            },
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    result = await client.get_statistics_v3(
        object_type="ad_plans",
        ids=[20865519],
        date_from="2026-05-05",
        date_to="2026-05-12",
    )

    # Проверяем что попали в v3 endpoint
    assert route.called
    call = route.calls[0]
    qs = dict(call.request.url.params)
    assert qs["id"] == "20865519"
    assert qs["date_from"] == "2026-05-05"
    assert qs["date_to"] == "2026-05-12"
    assert "base" in qs["fields"]
    assert "events" in qs["fields"]
    # Парсинг
    assert result["items"][0]["total"]["base"]["ctr"] == 0.8
    assert result["items"][0]["total"]["events"]["joinings"] == 2


@pytest.mark.asyncio
async def test_get_statistics_v3_validates_object_type():
    """Невалидный object_type → ValueError."""
    client = VKAdsClient(static_token="x")
    with pytest.raises(ValueError, match="object_type"):
        await client.get_statistics_v3(
            object_type="invalid", ids=[1], date_from="2026-05-01"
        )


@pytest.mark.asyncio
async def test_get_statistics_v3_limits_ids_to_200():
    """>200 ids → ValueError (VK API limit)."""
    client = VKAdsClient(static_token="x")
    with pytest.raises(ValueError, match="200"):
        await client.get_statistics_v3(
            object_type="ad_groups", ids=list(range(201)), date_from="2026-05-01"
        )


@pytest.mark.asyncio
@respx.mock
async def test_budget_exceeds_balance_returns_friendly_error():
    """VK отвечает 400 с budget_limit_day:unallowed_value когда проектный
    бюджет превышает баланс. Бот должен перехватить это и выдать понятное
    сообщение с рекомендациями вместо сырой VK-ошибки."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get("https://ads.vk.com/api/v1/urls/").mock(
        return_value=httpx.Response(
            200, json={"id": 12345, "url_object_id": 67890}
        )
    )
    upload_counter = {"i": 0}

    def _upload_resp(request):
        upload_counter["i"] += 1
        return httpx.Response(200, json={"id": 500 + upload_counter["i"]})

    respx.post(VK_CONTENT_STATIC_URL).mock(side_effect=_upload_resp)

    # VK возвращает реальную ошибку про unallowed_value для budget_limit_day
    respx.post(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": "validation_failed",
                    "message": "Validation failed",
                    "fields": {
                        "campaigns": {
                            "code": "bad_items",
                            "message": "Bad items in list",
                            "items": [{
                                "fields": {
                                    "budget_limit_day": {
                                        "code": "unallowed_value",
                                        "message": "Unallowed value",
                                    },
                                },
                                "code": "object_validation",
                                "message": "Object is invalid",
                            }],
                        }
                    },
                }
            },
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    creator = AdCreator(client)

    with pytest.raises(AdCreatorError, match="бюджет превышает баланс"):
        await creator.create_age_split_campaign(
            image_bytes=_real_image_bytes(),
            theme="t",
            copy=AdCopy(title="T", text="T", about="T"),
            community_url="https://vk.com/test",
            age_splits=[(41, 43), (44, 46), (47, 49), (50, 52), (53, 55), (56, 58)],
            daily_budget_rub_per_group=420,
            days_duration=7,
        )


@pytest.mark.asyncio
@respx.mock
async def test_minimum_daily_budget_check():
    """Pre-flight: дневной бюджет кампании (daily × groups) должен быть
    >= 100₽ (минимум VK). Если меньше — ошибка ДО отправки в VK."""
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )
    respx.get("https://ads.vk.com/api/v1/urls/").mock(
        return_value=httpx.Response(
            200, json={"id": 12345, "url_object_id": 67890}
        )
    )
    upload_counter = {"i": 0}

    def _upload_resp(request):
        upload_counter["i"] += 1
        return httpx.Response(200, json={"id": 500 + upload_counter["i"]})

    respx.post(VK_CONTENT_STATIC_URL).mock(side_effect=_upload_resp)
    ad_plan_route = respx.post(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(500, text="не должно вызываться")
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    creator = AdCreator(client)

    # 10₽/день × 6 групп = 60₽/день кампании < 100₽ минимум
    with pytest.raises(AdCreatorError, match="меньше минимума VK"):
        await creator.create_age_split_campaign(
            image_bytes=_real_image_bytes(),
            theme="t",
            copy=AdCopy(title="T", text="T", about="T"),
            community_url="https://vk.com/test",
            age_splits=[(41, 43), (44, 46), (47, 49), (50, 52), (53, 55), (56, 58)],
            daily_budget_rub_per_group=10,
            days_duration=7,
        )

    # POST в VK НЕ дёрнули — pre-flight остановил
    assert ad_plan_route.call_count == 0
