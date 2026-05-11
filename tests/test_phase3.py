"""Тесты Phase 3: загрузка картинок, оркестратор AdCreator, копирайтер fallback."""

import httpx
import pytest
import respx

from src.claude_brain.copywriter import fallback_copy_from_caption
from src.services.ad_creator import (
    DEFAULT_AGE_SPLITS_ORTHODOX,
    DEFAULT_PATTERNS_PACKAGE_3122,
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


def test_default_age_splits_orthodox_has_1_window_for_test():
    """Дефолт временно — одна группа для теста с малым балансом."""
    assert len(DEFAULT_AGE_SPLITS_ORTHODOX) == 1


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
    """End-to-end: URL registration + image upload + single nested POST /ad_plans.json."""
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
    respx.post(VK_CONTENT_STATIC_URL).mock(
        return_value=httpx.Response(200, json={"id": 555, "variants": {}})
    )
    ad_plan_route = respx.post(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 7000,
                "campaigns": [
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
        image_bytes=b"img",
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

    # Проверяем тело POST'а
    body = ad_plan_route.calls[0].request.read().decode()
    # campaigns = array of ad_groups
    assert '"campaigns"' in body
    # ad_object на топ-уровне
    assert '"ad_object_type"' in body
    assert '"ad_object_id"' in body
    # name на топ-уровне
    assert '"name"' in body


@pytest.mark.asyncio
@respx.mock
async def test_patterns_are_on_ad_group_level_not_banner():
    """Regression: VK отвергает `patterns` внутри banner с ошибкой
    `unknown_resource_field: Unknown fields: patterns`.

    Patterns должны быть на уровне ad_group (campaigns[N] в payload ad_plan),
    рядом с package_id — потому что patterns зависят именно от package_id.
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
    respx.post(VK_CONTENT_STATIC_URL).mock(
        return_value=httpx.Response(200, json={"id": 555, "variants": {}})
    )
    ad_plan_route = respx.post(f"{VK_API_BASE}/ad_plans.json").mock(
        return_value=httpx.Response(
            200,
            json={"id": 7000, "campaigns": [{"id": 7100, "banners": [{"id": 7110}]}]},
        )
    )

    client = VKAdsClient(
        authenticator=VKAdsAuthenticator(client_id="c", client_secret="s")
    )
    creator = AdCreator(client)
    await creator.create_age_split_campaign(
        image_bytes=b"img",
        theme="t",
        copy=AdCopy(title="T", text="T", about="T"),
        community_url="https://vk.com/test",
        age_splits=[(41, 42)],
        daily_budget_rub_per_group=200,
    )

    body = json.loads(ad_plan_route.calls[0].request.read())
    ad_group = body["campaigns"][0]
    banner = ad_group["banners"][0]

    # patterns должно быть на уровне ad_group и непустой
    assert "patterns" in ad_group, "patterns должен быть на уровне ad_group"
    assert isinstance(ad_group["patterns"], list) and len(ad_group["patterns"]) > 0
    assert ad_group["patterns"] == DEFAULT_PATTERNS_PACKAGE_3122

    # patterns НЕ должно быть в banner — иначе VK вернёт unknown_resource_field
    assert "patterns" not in banner, (
        "patterns не должно быть в banner — VK отвергает с unknown_resource_field"
    )


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
