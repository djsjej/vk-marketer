"""РЕПРО-ТЕСТ: воспроизводим падение Бибы из прода Railway.

Цель — увидеть точный traceback. Мокаем OAuth + ВСЕ endpoints через respx
catch-all, как будто VK успешно отвечает на каждый запрос.

Если Биба падает даже здесь — значит баг в самом коде (не в окружении).
Если не падает — баг специфичен для Railway (filesystem, permissions, env).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from src.biba.explorer import explore
from src.vk_ads.auth import OAUTH_URL, VKAdsAuthenticator
from src.vk_ads.client import VK_API_BASE, VKAdsClient


@pytest.mark.asyncio
@respx.mock
async def test_biba_full_run_all_endpoints_mocked(tmp_path, monkeypatch):
    """Полный прогон Бибы: OAuth + 36 endpoints отвечают валидным JSON."""
    # Биба пишет в docs/biba_findings — переадресуем в tmp_path
    from src.biba import explorer as biba_explorer
    monkeypatch.setattr(biba_explorer, "FINDINGS_DIR", tmp_path / "findings")

    # 1. OAuth — отдаём permanent токен
    respx.post(OAUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tk", "expires_in": 86400}
        )
    )

    # 2. Catch-all для всех GET на ads.vk.com — отвечаем {"items": [{"id": 1}]}
    # Это покрывает И /api/v2/* И /api/v1/urls/ — в Бибе оба есть.
    respx.get(url__regex=r"https://ads\.vk\.com/.*").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1, "name": "test"}], "count": 1}
        )
    )

    auth = VKAdsAuthenticator(client_id="cid", client_secret="csecret")
    client = VKAdsClient(authenticator=auth)

    report = await explore(client)

    # Если до сюда дошли — Биба не падает на коде, только проверим количество.
    assert len(report.findings) == 36, (
        f"Ожидали 36 endpoints, прошли {len(report.findings)}"
    )
