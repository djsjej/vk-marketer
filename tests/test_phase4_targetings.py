"""Тесты Phase 4 — расширенный таргетинг и переход на package 3127.

Проверяем что в реальный POST на VK API улетают:
- package_id=3127 (Написать вместо Вступить)
- targetings.sex=["female"]
- targetings.interests_soc_dem=[27137] (Духовный рост)
- targetings.segments=[...] если задан VK_AUDIENCE_SEGMENT_IDS, иначе НЕ передаётся
- cta_community_vk берётся из copy.cta (не захардкожен)

Источник истины полей: docs/VK_API_Targetings.md (получено от Vizit'а 14.05.2026).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from src.services.ad_creator import AdCopy, AdCreator
from src.vk_ads.auth import OAUTH_URL, VKAdsAuthenticator
from src.vk_ads.client import VK_API_BASE, VKAdsClient


# Реалистичный AdCopy — то что Claude сгенерил бы для православной темы.
_TEST_COPY = AdCopy(
    title="Напишите имена близких",
    text="За них помолятся в монастыре.",
    about="Православная община",
    cta="write",  # Phase 4 default — write для package 3127
)


def _mock_common_endpoints(image_600_id: int = 111, icon_256_id: int = 222) -> None:
    """Минимальный набор моков для AdCreator: OAuth, URL register, upload, plan."""
    # OAuth
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tk", "expires_in": 86400})
    )
    # URL registration (v1)
    respx.get("https://ads.vk.com/api/v1/urls/").mock(
        return_value=httpx.Response(200, json={"id": 999})
    )
    # Image upload — два вызова (для image_600 и icon_256)
    respx.post(f"{VK_API_BASE}/content/static.json").mock(
        return_value=httpx.Response(
            200,
            json={"id": image_600_id, "url": "https://x/y.jpg"},
        )
    )
    # GET шаблонной кампании — bot вызывает /ad_plans.json?_id=...&fields=...
    # (см. get_ad_plan_raw в client.py). Используем regex чтобы поймать с query.
    respx.get(url__regex=r"https://ads\.vk\.com/api/v2/ad_plans\.json\?.*_id=").mock(
        return_value=httpx.Response(200, json={"id": 20865519, "budget_limit_day": 420})
    )


def _build_creator() -> AdCreator:
    auth = VKAdsAuthenticator(client_id="cid", client_secret="cs")
    client = VKAdsClient(authenticator=auth, account_id=30591625)
    return AdCreator(client)


def _make_valid_jpeg() -> bytes:
    """Минимальный валидный JPEG 4x4 через PIL — для тестов upload+resize."""
    from io import BytesIO
    from PIL import Image

    img = Image.new("RGB", (4, 4), color=(128, 128, 128))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


_FAKE_JPEG = _make_valid_jpeg()


@pytest.mark.asyncio
@respx.mock
async def test_template_flow_sends_package_3127_and_cta_from_copy(monkeypatch):
    """package_id=3127 default + cta_community_vk=copy.cta (раньше был захардкожен signUp)."""
    # В env сегментов нет — должны НЕ передавать поле segments
    monkeypatch.setattr(
        "src.config.settings.vk_audience_segment_ids", None
    )
    _mock_common_endpoints()
    route = respx.post(f"{VK_API_BASE}/ad_groups.json").mock(
        return_value=httpx.Response(200, json={"id": 1234567, "banners": [{"id": 222333}]})
    )

    creator = _build_creator()
    await creator.create_age_split_groups_in_template_plan(
        image_bytes=_FAKE_JPEG,
        copy=_TEST_COPY,
        community_url="https://vk.com/pomolimsy",
        template_ad_plan_id=20865519,
        age_splits=[(41, 42)],
        daily_budget_rub_per_group=420,
    )

    assert route.call_count == 1, "Должен быть один POST на /ad_groups.json"
    body = json.loads(route.calls[0].request.read())

    assert body["package_id"] == 3127, (
        f"package_id должен быть 3127 (Написать), получен {body['package_id']}"
    )
    cta_text = body["banners"][0]["textblocks"]["cta_community_vk"]["text"]
    assert cta_text == "write", (
        f"CTA должен браться из copy.cta='write', получен {cta_text!r} "
        f"(возможно остался захардкожен signUp)"
    )


@pytest.mark.asyncio
@respx.mock
async def test_template_flow_targetings_include_female_and_dukhovny_rost(monkeypatch):
    """targetings.sex=['female'] + interests_soc_dem=[27137] (Духовный рост)."""
    monkeypatch.setattr(
        "src.config.settings.vk_audience_segment_ids", None
    )
    _mock_common_endpoints()
    route = respx.post(f"{VK_API_BASE}/ad_groups.json").mock(
        return_value=httpx.Response(200, json={"id": 1, "banners": [{"id": 2}]})
    )

    creator = _build_creator()
    await creator.create_age_split_groups_in_template_plan(
        image_bytes=_FAKE_JPEG,
        copy=_TEST_COPY,
        community_url="https://vk.com/pomolimsy",
        template_ad_plan_id=20865519,
        age_splits=[(45, 46)],
        daily_budget_rub_per_group=420,
    )

    body = json.loads(route.calls[0].request.read())
    targetings = body["targetings"]
    assert targetings["sex"] == ["female"], (
        f"sex должен быть только female (бизнес-модель — ЦА женщины), получен {targetings['sex']}"
    )
    # 3 ID в OR: Духовный рост (27137) + Замужем (10186) + Есть дети в семье (12920).
    # Захват более широкой аудитории для oCPM-обучения VK.
    assert targetings["interests_soc_dem"] == [27137, 10186, 12920], (
        f"interests_soc_dem должно быть [27137, 10186, 12920] "
        f"(Духовный рост + Замужем + С детьми), получено "
        f"{targetings.get('interests_soc_dem')}. ID из targetings_tree.json."
    )


@pytest.mark.asyncio
@respx.mock
async def test_template_flow_segments_added_when_env_set(monkeypatch):
    """Когда VK_AUDIENCE_SEGMENT_IDS задан — targetings.segments присутствует со значениями."""
    monkeypatch.setattr(
        "src.config.settings.vk_audience_segment_ids", "5551,7772"
    )
    _mock_common_endpoints()
    route = respx.post(f"{VK_API_BASE}/ad_groups.json").mock(
        return_value=httpx.Response(200, json={"id": 1, "banners": [{"id": 2}]})
    )

    creator = _build_creator()
    await creator.create_age_split_groups_in_template_plan(
        image_bytes=_FAKE_JPEG,
        copy=_TEST_COPY,
        community_url="https://vk.com/pomolimsy",
        template_ad_plan_id=20865519,
        age_splits=[(47, 48)],
        daily_budget_rub_per_group=420,
    )

    body = json.loads(route.calls[0].request.read())
    assert body["targetings"]["segments"] == [5551, 7772], (
        f"segments должны быть [5551, 7772] из env VK_AUDIENCE_SEGMENT_IDS, "
        f"получено {body['targetings'].get('segments')}"
    )


@pytest.mark.asyncio
@respx.mock
async def test_template_flow_segments_absent_when_env_not_set(monkeypatch):
    """Когда VK_AUDIENCE_SEGMENT_IDS не задан — segments в payload отсутствует.

    Это важно: пустой массив `segments: []` VK расценил бы как «таргет ни на кого»
    (никакие сегменты не подходят), и кампания не покажется никому.
    """
    monkeypatch.setattr(
        "src.config.settings.vk_audience_segment_ids", None
    )
    _mock_common_endpoints()
    route = respx.post(f"{VK_API_BASE}/ad_groups.json").mock(
        return_value=httpx.Response(200, json={"id": 1, "banners": [{"id": 2}]})
    )

    creator = _build_creator()
    await creator.create_age_split_groups_in_template_plan(
        image_bytes=_FAKE_JPEG,
        copy=_TEST_COPY,
        community_url="https://vk.com/pomolimsy",
        template_ad_plan_id=20865519,
        age_splits=[(50, 52)],
        daily_budget_rub_per_group=420,
    )

    body = json.loads(route.calls[0].request.read())
    assert "segments" not in body["targetings"], (
        f"segments НЕ должно быть в targetings когда env пустой, "
        f"получено: {body['targetings'].get('segments')}"
    )


def test_audience_segment_ids_parsed_handles_csv():
    """Settings.vk_audience_segment_ids_parsed корректно парсит CSV."""
    from src.config import Settings

    # Через подкласс с прямой подменой поля — pydantic-friendly
    cases = [
        (None, []),
        ("", []),
        ("12345", [12345]),
        ("12345,67890", [12345, 67890]),
        (" 12345 , 67890 ", [12345, 67890]),  # пробелы
        ("12345,abc,67890", [12345, 67890]),  # мусор отфильтровывается
    ]
    for raw, expected in cases:
        s = Settings.model_construct(vk_audience_segment_ids=raw)
        # property работает без полной инициализации Settings
        assert s.vk_audience_segment_ids_parsed == expected, (
            f"для {raw!r} ожидали {expected}, получили {s.vk_audience_segment_ids_parsed}"
        )


def test_adcopy_default_cta_is_write():
    """AdCopy() без явного cta — должен быть 'write' (под package 3127)."""
    copy = AdCopy(title="t", text="x", about="a")
    assert copy.cta == "write", (
        f"AdCopy.cta default должен быть 'write', получен {copy.cta!r}"
    )
