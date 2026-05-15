"""Захардкоженный список крупных открытых православных сообществ VK.

Используется агентом-таргетологом для парсинга подписчиков и поиска
пересечений (Phase 5.2). Без `groups.search` (тот требует user-token,
ошибка 1051 с нашим service-token).

Список составлен Claude+Vizit 15.05.2026 после того как выяснилось что
поиск через VK API недоступен. Источники: топ-10 православных сообществ
на pravoslavie.fm (2016) + проверка через `/vk_check` на проде.

Критерии включения:
- Открытое сообщество (is_closed=0) — иначе VK вернёт error 15
- Подписчиков 50k+ — для статистической значимости пересечений
- Чисто православная (не другие конфессии)
- Не сатира/мемы

Перед использованием в проде Vizit должен пройти glазами и одобрить.
Сообщества со скрытыми подписчиками (как pomolimsy) сюда не включаем.
"""

from typing import NamedTuple


class OrthodoxCommunity(NamedTuple):
    """Карточка известного православного сообщества."""

    screen_name: str
    expected_name: str           # для проверки что VK вернул то же
    approx_members: int          # ожидаемое число подписчиков (для UI)
    category: str                # 'информационный' / 'женский' / 'постный' / 'монастырь'
    notes: str                   # пояснение почему в списке


# Топ открытых православных сообществ для парсинга подписчиков и поиска
# пересечений. Список упорядочен по приблизительному размеру убывание.
#
# Проверено на проде 15.05.2026 через /vk_audience — 4 сообщества из
# предыдущей версии оказались со скрытыми подписчиками (error 15) и убраны:
# veruyu, orthodox_woman, pravoclavie, pravoslavie_fm. Оставлены только
# реально открытые. На будущее — кто захочет добавить новые группы,
# должен сначала проверить через /vk_parse <screen_name> 10 что VK
# не возвращает error 15.
ORTHODOX_COMMUNITIES_SEED: list[OrthodoxCommunity] = [
    OrthodoxCommunity(
        screen_name="orthodoxpsiholog",
        expected_name="Православная психология",
        approx_members=406_000,
        category="женский",
        notes="ПРОВЕРЕНО 15.05 на проде — открытые подписчики. 406k! Сильно больше чем я думал. Женская аудитория с запросом на духовно-психологическую помощь — попадание в ЦА pomolimsy.",
    ),
    OrthodoxCommunity(
        screen_name="p_trapeza",
        expected_name="Постная трапеза. Великий пост. Рецепты",
        approx_members=224_000,
        category="женский",
        notes="ПРОВЕРЕНО 15.05 — открытые. 224k, тоже сильно больше ожидаемого 90k. Женская аудитория, замужние, готовка.",
    ),
    OrthodoxCommunity(
        screen_name="pravoslavnie_hristiane",
        expected_name="Верю!",
        approx_members=109_000,
        category="информационный",
        notes="ПРОВЕРЕНО 15.05 — открытые. Реально это сообщество называется 'Верю!' (а не 'Верую Православие' как я думал). Общая правосл. аудитория.",
    ),
    OrthodoxCommunity(
        screen_name="ortodoxia",
        expected_name="Православие. Ортодоксия.",
        approx_members=69_000,
        category="информационный",
        notes="ПРОВЕРЕНО 15.05 — открытые. Православие в широком смысле, история и традиции.",
    ),
    # --- НЕПРОВЕРЕННЫЕ кандидаты на 15.05.2026 (добавлены при расширении seed) ---
    # Vizit должен проверить каждое через /vk_parse <screen_name> 10.
    # Если возвращает error 15 (access denied / group hide members) — убрать
    # из seed. Если работает — пометить как ПРОВЕРЕНО.
    OrthodoxCommunity(
        screen_name="iconpainting",
        expected_name="Иконопись (~)",
        approx_members=80_000,
        category="информационный",
        notes="НЕПРОВЕРЕНО 15.05. Иконы и иконопись — близкая тематика, может иметь молящихся.",
    ),
    OrthodoxCommunity(
        screen_name="pravoslavnie_molitvi",
        expected_name="Православные молитвы (~)",
        approx_members=60_000,
        category="женский",
        notes="НЕПРОВЕРЕНО 15.05. Прямой запрос ЦА — молитвы.",
    ),
    OrthodoxCommunity(
        screen_name="pravoslavie_ru",
        expected_name="Православие.Ру (~)",
        approx_members=70_000,
        category="информационный",
        notes="НЕПРОВЕРЕНО 15.05. Один из крупнейших правосл. порталов.",
    ),
    OrthodoxCommunity(
        screen_name="diveevo_serafim",
        expected_name="Дивеево / Серафим Саровский (~)",
        approx_members=50_000,
        category="монастырь",
        notes="НЕПРОВЕРЕНО 15.05. Если открыто — сильное попадание (паломники).",
    ),
    OrthodoxCommunity(
        screen_name="azbyka_ru",
        expected_name="Азбука Веры (~)",
        approx_members=100_000,
        category="информационный",
        notes="НЕПРОВЕРЕНО 15.05. Крупный православный портал с молитвословом.",
    ),
    OrthodoxCommunity(
        screen_name="optina",
        expected_name="Оптина пустынь (~)",
        approx_members=60_000,
        category="монастырь",
        notes="НЕПРОВЕРЕНО 15.05. Известный монастырь, паломническая аудитория.",
    ),
    OrthodoxCommunity(
        screen_name="pravkalendar",
        expected_name="Православный календарь (~)",
        approx_members=40_000,
        category="информационный",
        notes="НЕПРОВЕРЕНО 15.05. Календарь святых — релевантно нашей нише.",
    ),
    OrthodoxCommunity(
        screen_name="pravlife",
        expected_name="Православная жизнь (~)",
        approx_members=80_000,
        category="женский",
        notes="НЕПРОВЕРЕНО 15.05. Жизнь православной семьи.",
    ),
]


def get_seed_screen_names() -> list[str]:
    """Только список screen_names для парсинга — удобный shortcut."""
    return [c.screen_name for c in ORTHODOX_COMMUNITIES_SEED]
