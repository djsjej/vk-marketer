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
ORTHODOX_COMMUNITIES_SEED: list[OrthodoxCommunity] = [
    OrthodoxCommunity(
        screen_name="pravoslavnie_hristiane",
        expected_name="Верую † Православие",
        approx_members=700_000,
        category="информационный",
        notes="Крупнейшее православное VK-сообщество, общая тематика.",
    ),
    OrthodoxCommunity(
        screen_name="veruyu",
        expected_name="Верю!",
        approx_members=109_000,
        category="информационный",
        notes="Подтверждено через /vk_parse 15.05 — подписчики открыты.",
    ),
    OrthodoxCommunity(
        screen_name="p_trapeza",
        expected_name="Постная трапеза",
        approx_members=90_000,
        category="женский",  # женская аудитория, готовка
        notes="Высокая концентрация ЦА pomolimsy — замужние женщины 35-60.",
    ),
    OrthodoxCommunity(
        screen_name="ortodoxia",
        expected_name="Православие † Ορθόδοξη",
        approx_members=85_000,
        category="информационный",
        notes="Православие в широком смысле, история и традиции.",
    ),
    OrthodoxCommunity(
        screen_name="orthodox_woman",
        expected_name="Православная женщина",
        approx_members=80_000,
        category="женский",
        notes="Идеальное попадание в ЦА — женщины 40-60, замужние.",
    ),
    OrthodoxCommunity(
        screen_name="pravoclavie",
        expected_name="Православие",
        approx_members=80_000,
        category="информационный",
        notes="Общая правосл. аудитория.",
    ),
    OrthodoxCommunity(
        screen_name="orthodoxpsiholog",
        expected_name="Православная психология",
        approx_members=55_000,
        category="женский",
        notes="Женская аудитория с запросом на духовно-психологическую помощь.",
    ),
    OrthodoxCommunity(
        screen_name="pravoslavie_fm",
        expected_name="Православие FM",
        approx_members=50_000,
        category="информационный",
        notes="Радио + молитвы, может содержать молящихся за близких.",
    ),
]


def get_seed_screen_names() -> list[str]:
    """Только список screen_names для парсинга — удобный shortcut."""
    return [c.screen_name for c in ORTHODOX_COMMUNITIES_SEED]
