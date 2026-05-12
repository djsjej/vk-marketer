#!/usr/bin/env python3
"""Запуск Бибы — разведчика VK Ads.

Использование:
    python scripts/run_biba.py

Биба обойдёт ~30 справочных endpoints VK Ads и сохранит результат в:
    docs/biba_findings/REPORT.md    — Markdown-сводка
    docs/biba_findings/report.json  — структурированный отчёт
    docs/biba_findings/<name>.json  — raw ответы каждого endpoint

После прогона — закоммить эти файлы в git, чтобы Claude в следующей сессии
сразу видел inventory кабинета.

Требуется в env:
    VK_ADS_OAUTH_CLIENT_ID
    VK_ADS_OAUTH_CLIENT_SECRET
"""

import asyncio
import logging
import sys
from pathlib import Path

# Чтобы запустить из корня репо без установки пакета
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.biba.explorer import explore
from src.config import settings
from src.vk_ads.auth import VKAdsAuthenticator
from src.vk_ads.client import VKAdsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)


async def main() -> int:
    if not settings.has_vk_oauth:
        print("❌ Нет VK_ADS_OAUTH_CLIENT_ID/SECRET в env. Биба никуда не пойдёт.")
        return 1

    print("⚙️  Готовлю клиента VK Ads...")
    auth = VKAdsAuthenticator(
        client_id=settings.vk_ads_oauth_client_id,
        client_secret=settings.vk_ads_oauth_client_secret,
    )
    client = VKAdsClient(authenticator=auth)

    print("🚀 Биба отправляется в экспедицию...\n")
    report = await explore(client)

    print(f"\n✅ Экспедиция завершена.")
    print(f"   Endpoints: {len(report.findings)}")
    print(f"   Откликнулись: {report.ok_count}")
    print(f"   Не найдены:   {report.not_found_count}")
    print(f"\n📄 Рапорт: docs/biba_findings/REPORT.md")
    print(f"📦 Raw: docs/biba_findings/<endpoint>.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
