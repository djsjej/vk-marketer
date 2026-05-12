"""Биба-proposals: генератор гипотез на основе первого прохода разведки.

После первичного обхода Биба анализирует что нашёл и сам выдвигает
гипотезы что ещё стоит проверить. Гипотезы сохраняются в PROPOSALS.md
для одобрения через Vizit'а (он → Claude → код).

Стратегии генерации гипотез:

A) Mining имён в найденных ответах
   Если в ответе встречается ключ `category_id`, `audience_id` — попробовать
   `/category/<id>.json`, `/categories.json`, `/audiences.json` и т.п.

B) Variations
   Singular ↔ plural, разные базовые пути (/dictionary/X, /X, /info/X).

C) Linked resources
   Если в sample-ответе есть IDs или ссылки на другие сущности — попробовать
   их query endpoints.

D) Retry alternative names
   Если /strategies.json вернул 404 — попробовать /bidding_strategies.json,
   /autobid_strategies.json и т.п.

Все гипотезы получают rationale — почему Биба думает что стоит проверить.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.biba.explorer import Finding


@dataclass
class Proposal:
    """Гипотеза Бибы — endpoint который стоит попробовать."""
    path: str
    rationale: str  # почему Биба думает что это работает
    category: str  # "mining" / "variation" / "linked" / "alternative"
    confidence: str = "medium"  # "low" / "medium" / "high"

    def to_markdown_line(self, idx: int) -> str:
        emoji = {"high": "🎯", "medium": "🤔", "low": "🌀"}.get(self.confidence, "❓")
        return (
            f"{idx}. {emoji} **`GET {self.path}`** [{self.category}]\n"
            f"   _Гипотеза:_ {self.rationale}"
        )


# Известные «id-style» суффиксы которые часто указывают на отдельную сущность.
ID_SUFFIX_PATTERN = re.compile(r"^(.+?)_id$")

# Базовые пути которые точно работают в VK Ads API (для variations).
KNOWN_BASE_PATHS = ["/", "/dictionary/", "/info/"]


def _to_singular(word: str) -> str:
    """Грубая де-плюрализация: regions → region, categories → category."""
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("ses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _to_plural(word: str) -> str:
    """Грубая плюрализация: region → regions, category → categories."""
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith("s") or word.endswith("x"):
        return word + "es"
    return word + "s"


def _mine_id_keys(findings: list[Finding]) -> list[Proposal]:
    """Стратегия A — ищем поля _id в sample'ах ответов.

    Если встречается `package_id`, `category_id` — значит есть сущность
    `package`, `category` и стоит поискать справочник для неё.
    """
    proposals: list[Proposal] = []
    seen: set[str] = set()

    for f in findings:
        if not f.sample:
            continue
        # Собираем все ключи в sample (рекурсивно, но плоско)
        all_keys: set[str] = set()
        _collect_keys(f.sample, all_keys)

        for key in all_keys:
            m = ID_SUFFIX_PATTERN.match(key)
            if not m:
                continue
            entity_singular = m.group(1)
            entity_plural = _to_plural(entity_singular)
            for candidate in (f"/{entity_plural}.json", f"/dictionary/{entity_plural}.json"):
                if candidate in seen:
                    continue
                seen.add(candidate)
                proposals.append(Proposal(
                    path=candidate,
                    rationale=(
                        f"В ответе `{f.name}` встречалось поле `{key}` — значит "
                        f"есть отдельная сущность `{entity_singular}`. "
                        f"Скорее всего справочник лежит здесь."
                    ),
                    category="mining",
                    confidence="high",
                ))
    return proposals


def _collect_keys(obj: Any, accumulator: set[str], depth: int = 0) -> None:
    """Рекурсивно собирает все ключи dict'ов из вложенной структуры."""
    if depth > 5:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            accumulator.add(k)
            _collect_keys(v, accumulator, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:5]:  # ограничиваем чтобы не перебрать
            _collect_keys(item, accumulator, depth + 1)


def _retry_alternative_names(findings: list[Finding]) -> list[Proposal]:
    """Стратегия D — для каждого 404 пробуем альтернативные имена.

    Биба знает типовые синонимы из VK/myTarget API.
    """
    proposals: list[Proposal] = []

    # Карта: имя что не нашлось → альтернативы которые стоит попробовать
    alternatives_map = {
        "strategies": ["bidding_strategies", "autobid_strategies", "bid_modes"],
        "formats": ["ad_formats", "creative_formats", "banner_formats"],
        "objectives": ["campaign_objectives", "goals", "ad_objectives"],
        "packages": ["ad_packages", "campaign_packages", "package_types"],
        "cta_buttons": ["cta", "call_to_action", "buttons"],
        "interests": ["interest_categories", "user_interests"],
        "languages": ["locales", "user_languages"],
        "browsers": ["user_browsers", "web_browsers"],
        "pads": ["placements", "ad_pads", "ad_placements"],
        "audience_types": ["audience_kinds", "segment_types"],
        "limits": ["account_limits", "object_limits", "quotas"],
        "throttling": ["rate_limits", "api_limits"],
    }

    for f in findings:
        if f.status != "not_found":
            continue
        alts = alternatives_map.get(f.name, [])
        for alt in alts:
            proposals.append(Proposal(
                path=f"/{alt}.json",
                rationale=(
                    f"Endpoint `{f.path}` вернул 404. В VK/myTarget API такое "
                    f"часто называют `{alt}` — стоит попробовать."
                ),
                category="alternative",
                confidence="medium",
            ))
    return proposals


def _variations_for_successful(findings: list[Finding]) -> list[Proposal]:
    """Стратегия B — для каждого работающего endpoint пробуем variations.

    Если /regions.json работает — попробовать /dictionary/regions.json и
    /info/regions.json. Иногда там детальнее или иначе организовано.
    """
    proposals: list[Proposal] = []
    seen: set[str] = set()

    for f in findings:
        if f.status != "ok":
            continue
        # Извлекаем имя ресурса из пути: /regions.json → "regions"
        m = re.match(r"^/(?:dictionary/|info/)?([^.?]+)", f.path)
        if not m:
            continue
        name = m.group(1)

        for base in KNOWN_BASE_PATHS:
            candidate = f"{base}{name}.json"
            if candidate == f.path or candidate in seen:
                continue
            seen.add(candidate)
            proposals.append(Proposal(
                path=candidate,
                rationale=(
                    f"`{f.path}` работает. Возможно есть детальная версия "
                    f"по другому базовому пути."
                ),
                category="variation",
                confidence="low",
            ))
    return proposals


def _link_mining(findings: list[Finding]) -> list[Proposal]:
    """Стратегия C — ищем строки в ответах которые похожи на пути API."""
    proposals: list[Proposal] = []
    seen: set[str] = set()
    api_path_re = re.compile(r"/api/v\d+/([^\s\"',]+\.json)")

    for f in findings:
        if not f.sample:
            continue
        sample_str = str(f.sample)
        for match in api_path_re.findall(sample_str):
            candidate = f"/{match}"
            if candidate in seen:
                continue
            seen.add(candidate)
            proposals.append(Proposal(
                path=candidate,
                rationale=(
                    f"В ответе `{f.name}` встретилась ссылка на этот path — "
                    f"VK сам подсказал."
                ),
                category="linked",
                confidence="high",
            ))
    return proposals


def generate_proposals(findings: list[Finding]) -> list[Proposal]:
    """Главный entry — прогоняет все стратегии и возвращает уникальные гипотезы."""
    proposals: list[Proposal] = []
    proposals.extend(_mine_id_keys(findings))
    proposals.extend(_retry_alternative_names(findings))
    proposals.extend(_variations_for_successful(findings))
    proposals.extend(_link_mining(findings))

    # Дедуп по path — оставляем версию с более высокой confidence
    seen: dict[str, Proposal] = {}
    confidence_order = {"high": 3, "medium": 2, "low": 1}
    for p in proposals:
        existing = seen.get(p.path)
        if existing is None:
            seen[p.path] = p
        elif confidence_order[p.confidence] > confidence_order[existing.confidence]:
            seen[p.path] = p

    # Сортируем: сначала high confidence, потом по path
    result = sorted(
        seen.values(),
        key=lambda x: (-confidence_order[x.confidence], x.path),
    )
    return result


def proposals_to_markdown(proposals: list[Proposal]) -> str:
    """Рендер PROPOSALS.md в стиле Бибы."""
    if not proposals:
        return (
            "# Биба нечего предложить\n\n"
            "В первом проходе ничего интересного не зацепило. "
            "Возможно VK Ads API проще чем казалось, либо я (Биба) дурак "
            "и плохо смотрел. Дай мне свежий список endpoints — попробую.\n\n"
            "— Биба\n"
        )

    by_category: dict[str, list[Proposal]] = {}
    for p in proposals:
        by_category.setdefault(p.category, []).append(p)

    lines = [
        "# Предложения Бибы — что ещё посмотреть",
        "",
        "Биба проанализировал результаты первого обхода и предлагает "
        "проверить ещё эти endpoints. Каждая гипотеза имеет rationale "
        "(почему стоит попробовать) и confidence.",
        "",
        "**Workflow одобрения:**",
        "1. Ты (Vizit) смотришь список",
        "2. Говоришь Claude в чате: «одобряю 1, 3, 7-12»",
        "3. Claude добавляет одобренные в `ENDPOINTS_TO_EXPLORE` и пушит",
        "4. Ты запускаешь `python scripts/run_biba.py` снова",
        "",
        "🎯 = high confidence (VK сам подсказал или явная зацепка)",
        "🤔 = medium (типичный паттерн из VK API)",
        "🌀 = low (на удачу, может ничего)",
        "",
    ]

    category_titles = {
        "linked": "🔗 Linked — пути упомянутые в ответах VK",
        "mining": "⛏ Mining — выведено из имён полей _id",
        "alternative": "🔄 Alternative — синонимы для 404-endpoints",
        "variation": "🌱 Variations — другие базовые пути",
    }

    idx = 1
    for category in ("linked", "mining", "alternative", "variation"):
        items = by_category.get(category, [])
        if not items:
            continue
        lines.append(f"## {category_titles[category]}")
        lines.append("")
        for p in items:
            lines.append(p.to_markdown_line(idx))
            lines.append("")
            idx += 1

    lines.append("\n— Биба\n")
    return "\n".join(lines)
