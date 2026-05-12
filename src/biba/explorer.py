"""Биба-explorer: обходит справочные endpoints VK Ads и собирает inventory.

Принципы работы:
- Только GET-запросы (read-only, безопасно)
- НЕ трогает реальные кампании Vizit'а (не лезет в /ad_plans без id, /banners итд)
- Любой endpoint — best effort: если 404/500 — фиксирует и идёт дальше
- Сохраняет каждую находку как отдельный JSON в docs/biba_findings/

Список endpoints — гипотезы основанные на типичной структуре VK Ads API
(/api/v2/dictionary/*, /api/v2/info/*). Если что-то не отвечает — узнаем
по факту, тоже информация.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.biba.persona import BIBA_BANNER, BIBA_SIGNOFF
from src.vk_ads.client import VK_API_BASE, VKAdsClient

logger = logging.getLogger("biba")

# Куда складываем находки.
FINDINGS_DIR = Path("docs/biba_findings")


# Endpoints для исследования.
#
# Источник истины — оглавление VK Реклама API из docs/LINKS_VK_ADS.pdf и
# VK_Реклама___платформа_для_рекламы_на_проектах_VK.pdf (Project Knowledge).
# НЕ ДОГАДЫВАТЬСЯ — только то что подтверждено в документации Vizit'а.
#
# Структура: (имя, метод+путь, описание).
# Путь начинается со слэша и НЕ включает базовый URL.
#
# Что точно подтверждено документацией:
# - Dictionary: только regions и interests (см. LINKS_VK_ADS.pdf)
# - Resources с list view (plural form, GET без id):
#   Remarketing/segments, LeadForms, Subscriptions, Surveys,
#   ProductCatalogs, Products, AudienceFeeds, SharingKeys.
#
# Чего тут нет и быть НЕ ДОЛЖНО (типичные ошибки):
# - /objectives.json, /strategies.json, /formats.json, /packages.json
#   Это разделы info-документации, не endpoints. У них нет API.
# - /devices.json, /browsers.json и т.п. — таргетинг по device идёт
#   как поля внутри targetings, отдельных справочников VK Ads нет.
#
# Если нужно добавить новый endpoint — найди подтверждение в PDF Project
# Knowledge или попроси у Vizit'а страницу /resource/<Name>.
ENDPOINTS_TO_EXPLORE: list[tuple[str, str, str]] = [
    # --- Подтверждённые dictionary endpoints (LINKS_VK_ADS.pdf) ---
    ("dictionary_regions", "/dictionary/regions.json?limit=500",
     "Все регионы — страны, города, локации"),
    ("dictionary_interests", "/dictionary/interests.json",
     "Категории интересов (плоский список)"),

    # --- Remarketing (аудитории/сегменты — read-only list) ---
    ("remarketing_segments", "/remarketing/segments.json",
     "Существующие сегменты ремаркетинга кабинета"),
    ("remarketing_pixels", "/remarketing/pixels.json",
     "Установленные VK Pixel"),
    ("remarketing_users_lists", "/remarketing/users_lists.json",
     "Загруженные списки контактов"),

    # --- Лид-формы ---
    ("lead_forms", "/lead_forms.json",
     "Все лид-формы кабинета"),

    # --- Подписки и опросы ---
    ("subscriptions", "/subscriptions.json",
     "Подписки на рассылки"),
    ("surveys", "/surveys.json",
     "Опросы"),

    # --- Каталоги товаров / товарный фид ---
    ("product_catalogs", "/product_catalogs.json",
     "Каталоги товаров для динамической рекламы"),
    ("products", "/products.json",
     "Товары в каталогах"),
    ("audience_feeds", "/audience_feeds.json",
     "Фиды аудиторий"),

    # --- Ключи sharing / прочее ---
    ("sharing_keys", "/sharing_keys.json",
     "Ключи разделения доступа"),

    # --- Финансы (для агентских — может вернуть 403) ---
    ("transactions", "/transactions.json",
     "Транзакции (пополнения/списания)"),
    ("transaction_groups", "/transaction_groups.json",
     "Группы транзакций"),

    # --- URL объекты продвижения ---
    ("urls_v1", "/urls/", "URL объекты продвижения (v1 endpoint)"),

    # --- Top-level resources уже используемые ботом (sanity check) ---
    ("ad_plans", "/ad_plans.json?limit=5",
     "Все кампании кабинета (только sample 5 шт)"),
    ("ad_groups", "/ad_groups.json?limit=5",
     "Все группы кабинета (только sample)"),
    ("banner_fields", "/banner_fields.json",
     "Каталог всех полей banner (193 поля — упоминалось ранее)"),
]


@dataclass
class Finding:
    """Одна находка Бибы — результат опроса одного endpoint."""
    name: str
    path: str
    description: str
    status: str  # "ok" / "not_found" / "forbidden" / "server_error" / "no_data"
    http_code: int | None = None
    sample: Any = None  # первые элементы из ответа для понимания структуры
    keys_top_level: list[str] = field(default_factory=list)
    item_count: int | None = None
    note: str = ""

    def to_short_summary(self) -> str:
        """Короткая строчка для финального рапорта."""
        emoji = {
            "ok": "✅",
            "not_found": "⛔",
            "forbidden": "🔒",
            "server_error": "💥",
            "no_data": "⚠️",
        }.get(self.status, "❓")
        suffix = ""
        if self.item_count is not None:
            suffix = f" — {self.item_count} элементов"
        elif self.keys_top_level:
            suffix = f" — keys: {', '.join(self.keys_top_level[:5])}"
        return f"{emoji} {self.name} ({self.path}){suffix}"


@dataclass
class ExpeditionReport:
    """Полный отчёт Бибы об экспедиции."""
    started_at: str
    finished_at: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for f in self.findings if f.status == "ok")

    @property
    def not_found_count(self) -> int:
        return sum(1 for f in self.findings if f.status == "not_found")

    def to_markdown(self) -> str:
        """Markdown-рапорт в стиле Бибы — для отправки Claude'у/Vizit'у."""
        lines = [
            "# Рапорт Бибы — экспедиция в VK Ads",
            "",
            f"**Начато:** {self.started_at}",
            f"**Завершено:** {self.finished_at}",
            f"**Исследовано endpoints:** {len(self.findings)}",
            f"**Откликнулись:** {self.ok_count}",
            f"**Не найдены:** {self.not_found_count}",
            "",
            "## Сводка",
            "",
        ]
        for f in self.findings:
            lines.append(f"- {f.to_short_summary()}")
        lines.extend(["", "## Детали по каждому endpoint", ""])
        for f in self.findings:
            lines.append(f"### {f.name}")
            lines.append(f"- **Путь:** `{f.path}`")
            lines.append(f"- **Описание:** {f.description}")
            lines.append(f"- **Статус:** {f.status} (HTTP {f.http_code})")
            if f.item_count is not None:
                lines.append(f"- **Элементов:** {f.item_count}")
            if f.keys_top_level:
                lines.append(f"- **Top-level keys:** {', '.join(f.keys_top_level)}")
            if f.note:
                lines.append(f"- **Заметка:** {f.note}")
            if f.sample:
                sample_json = json.dumps(f.sample, ensure_ascii=False, indent=2)
                # Обрезаем гигантские sample для читаемости
                if len(sample_json) > 1500:
                    sample_json = sample_json[:1500] + "\n... [truncated]"
                lines.append(f"- **Sample:**\n```json\n{sample_json}\n```")
            lines.append("")
        lines.append(f"\n{BIBA_SIGNOFF}")
        return "\n".join(lines)


def _summarize_response(data: Any) -> tuple[list[str], Any, int | None]:
    """Извлекает: top-level keys, sample (первые 3 элемента/usefully cut), count.

    Возвращает (keys, sample, count). count = None если не список.
    """
    if data is None:
        return [], None, None
    if isinstance(data, list):
        return [], data[:3], len(data)
    if isinstance(data, dict):
        keys = list(data.keys())
        # Если есть items — это типичная VK-структура list-ответа
        if "items" in data and isinstance(data["items"], list):
            return keys, data["items"][:3], len(data["items"])
        # Иначе sample = весь dict (порежется при сериализации)
        return keys, data, None
    return [], data, None


async def explore(client: VKAdsClient) -> ExpeditionReport:
    """Прогоняет Бибу по всем endpoints. Возвращает полный отчёт."""
    print(BIBA_BANNER)
    started = datetime.now(timezone.utc).isoformat()
    report = ExpeditionReport(started_at=started, finished_at="")
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)

    for name, path, description in ENDPOINTS_TO_EXPLORE:
        print(f"  🔍 {name:30s} → GET {path}")
        try:
            # Используем _request чтобы получить raw данные напрямую
            data = await client._request("GET", path)
            keys, sample, count = _summarize_response(data)
            status = "ok" if data else "no_data"
            finding = Finding(
                name=name,
                path=path,
                description=description,
                status=status,
                http_code=200,
                sample=sample,
                keys_top_level=keys,
                item_count=count,
            )
            # Сохраняем raw ответ в отдельный JSON
            try:
                raw_file = FINDINGS_DIR / f"{name}.json"
                with open(raw_file, "w", encoding="utf-8") as fp:
                    json.dump(data, fp, ensure_ascii=False, indent=2)
            except Exception as e:
                finding.note = f"не смог сохранить raw: {e}"
        except Exception as e:
            err_str = str(e)
            # Парсим HTTP код из текста ошибки (VKAdsAPIError содержит "вернул XXX:")
            http_code = None
            status = "server_error"
            if "вернул 404" in err_str or "404" in err_str:
                status = "not_found"
                http_code = 404
            elif "вернул 403" in err_str or "403" in err_str:
                status = "forbidden"
                http_code = 403
            elif "вернул 401" in err_str:
                status = "forbidden"
                http_code = 401
            elif "вернул 500" in err_str or "вернул 502" in err_str:
                status = "server_error"
                http_code = 500
            finding = Finding(
                name=name,
                path=path,
                description=description,
                status=status,
                http_code=http_code,
                note=err_str[:300],
            )
        report.findings.append(finding)
        print(f"     → {finding.to_short_summary()}")

    report.finished_at = datetime.now(timezone.utc).isoformat()

    # Сохраняем сводный рапорт двумя файлами
    md_file = FINDINGS_DIR / "REPORT.md"
    json_file = FINDINGS_DIR / "report.json"
    md_file.write_text(report.to_markdown(), encoding="utf-8")
    with open(json_file, "w", encoding="utf-8") as fp:
        json.dump([asdict(f) for f in report.findings], fp, ensure_ascii=False, indent=2)

    # Биба думает: что ещё стоит проверить? Генерируем PROPOSALS.md.
    # Это его «второй мозг» — анализ найденного и выдвижение гипотез
    # (мнение полей, упоминания путей в ответах, вариации, синонимы).
    from src.biba.proposals import generate_proposals, proposals_to_markdown
    proposals = generate_proposals(report.findings)
    proposals_file = FINDINGS_DIR / "PROPOSALS.md"
    proposals_file.write_text(proposals_to_markdown(proposals), encoding="utf-8")
    print(f"\n💡 Биба предложил ещё {len(proposals)} endpoints для проверки.")
    print(f"   См. docs/biba_findings/PROPOSALS.md")

    return report
