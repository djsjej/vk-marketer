"""Генератор текстов объявлений через Claude.

Учитывает специфику VK Реклама API v2 (поля textblocks):
- title_40_vkads: до 40 символов
- text_2000: до 2000 символов
- about_company_115: до 115 символов
- cta_community_vk: enum CTA (см. banner_fields.json от Бибы)

Формат под православную аудиторию: тон спокойный, без манипуляций, без эмодзи-флуда.
Когда тема нерелигиозная — Claude сам подстраивается под входящий контекст.

Бизнес-модель Vizit'а (vk.com/pomolimsy):
1. Объявление зовёт человека НАПИСАТЬ имена близких в сообщество
2. Человек пишет → принимается ботом/админом
3. Имя попадает в молитвенную рассылку
4. Опционально — добровольное пожертвование за поминовение

Поэтому основной CTA в православной теме — "write" (Написать),
а не "signUp" (Вступить).
"""

import json
import logging

from anthropic import AsyncAnthropic

from src.config import settings
from src.services.ad_creator import AdCopy

logger = logging.getLogger(__name__)


# Допустимые значения cta_community_vk из banner_fields.json
# (опросили через Бибу /banner_fields.json):
# signUp, buy, contactUs, subscribe, message, write, visitSite, learnMore,
# getoffer, book, enroll, askQuestion, startChat, getPrice.
# В промптах ограничиваем подмножеством релевантных для нашей ниши.
COPYWRITER_SYSTEM_PROMPT_SINGLE = """Ты — копирайтер VK Рекламы для ПРАВОСЛАВНОГО молитвенного сообщества (vk.com/pomolimsy).

Это всегда православная реклама. Тема, которую дал пользователь, — лишь повод и акцент \
(праздник, родительская суббота, конкретный святой, здравие или упокоение), но НИКОГДА \
не повод уходить в мирской маркетинг.

БИЗНЕС-МОДЕЛЬ (всегда одна и та же):
Человек по объявлению пишет имена близких (за здравие или за упокой), за них молятся в \
сообществе. ОСНОВНОЕ ДЕЙСТВИЕ объявления — попросить читателя НАПИСАТЬ имена дорогих сердцу. \
Не «вступить», не «подписаться», не «пройти курс» — именно написать имена.

ТОН (строго):
- Спокойный, благоговейный, тёплый, сострадательный.
- Без эмодзи, без мирских клише («акция», «срочно», «успей», «навык», «карьера», «доход»).
- Без кликбейта («ШОК», «брось читать», «ты устал притворяться»).
- Без манипуляций страхом и без обещаний чудес/исцелений.
- Можно мягко цитировать святых отцов или Писание с указанием источника.
- Образы: храм, свеча, икона, молитва, память о близких.

ЭТАЛОННЫЕ ПРИМЕРЫ (так писали тексты, которые реально запускали — держи ЭТОТ уровень и тон):
- «Помяните дорогих ушедших» — «Они ушли — но остались в сердце. И в молитве. Напишите имена ушедших близких — помянем.»
- «О здравии родителей» — «Родители стареют, а мы всё заняты. Простое дело — напишите их имена. Помолимся о здравии.»
- «Имя. Просьба. Молитва.» — «Имя. Просьба. Молитва. Напишите имя того, за кого болит сердце.»

cta: всегда "write" (Написать). Альтернатива только "message".

Формат VK-объявления (поля textblocks):
- title: до 40 символов (заголовок)
- text: до 2000 символов (основной текст)
- about: до 115 символов (о сообществе)

Возвращай ТОЛЬКО JSON следующей структуры, без преамбулы и Markdown:
{
  "title": "...",
  "text": "...",
  "about": "...",
  "cta": "write"
}

Каждое поле — строка. Длины строго в лимитах VK."""


COPYWRITER_SYSTEM_PROMPT_VARIANTS = """Ты — копирайтер VK Рекламы для ПРАВОСЛАВНОГО молитвенного сообщества (vk.com/pomolimsy). \
Генерируешь N разных вариантов объявления для A/B-теста.

Это ВСЕГДА православная реклама. Тема от пользователя — лишь повод и акцент \
(праздник, родительская суббота, святой, здравие или упокоение), но НИКОГДА не повод \
уходить в мирской маркетинг.

БИЗНЕС-МОДЕЛЬ (одна и та же во всех вариантах):
Человек по объявлению пишет имена близких (за здравие или за упокой), за них молятся в \
сообществе. ОСНОВНОЕ ДЕЙСТВИЕ каждого варианта — попросить читателя НАПИСАТЬ имена дорогих \
сердцу. Не «вступить», не «подписаться», не «пройти курс».

ВАРИАНТЫ отличаются УГЛОМ ВНУТРИ православной темы (не уходи из неё!):
1. Память об усопших — тёплая скорбь, «помяните дорогих, кого нет рядом»
2. Молитва о здравии родных — забота, «о здравии родителей, детей, мужа»
3. Соборная молитва / традиция — «вместе молимся», сила общей молитвы
4. Прямой тёплый призыв — коротко и сердечно написать имена

ЕСЛИ дан Контекст (аудитория / жизненный этап) — ЗАТАЧИВАЙ все варианты
под него: молодым матерям — о молитве за детей и младенцев; женщинам
средних лет — о здравии родителей, о детях, о силах в трудах; старшим —
о внуках, об упокоении ушедших, о памяти. Тема остаётся православной,
но боль и образы берёшь из жизни этой аудитории.

ТОН (строго):
- Спокойный, благоговейный, тёплый, сострадательный.
- БЕЗ эмодзи, БЕЗ мирских клише (акция/срочно/успей/навык/карьера/доход/курс).
- БЕЗ кликбейта («ШОК», «брось это читать», «ты устал притворяться»).
- БЕЗ манипуляций страхом, БЕЗ обещаний чудес и исцелений.
- Образы: храм, свеча, икона, молитва, память о близких.
- Можно мягко цитировать святых отцов / Писание с указанием источника.

cta: всегда "write" (Написать); альтернатива только "message".

ЭТАЛОННЫЕ ПРИМЕРЫ (так писали тексты, которые реально запускали — держи ЭТОТ уровень, тон и краткость):
- «Помяните дорогих ушедших» — «Они ушли — но остались в сердце. И в молитве. Напишите имена ушедших близких — помянем.»
- «О здравии родителей» — «Родители стареют, а мы всё заняты. Простое дело — напишите их имена. Помолимся о здравии.»
- «Молитва общины» — «В нашей общине пишут имена. Больных. Страдающих. Ушедших. Каждое имя становится молитвой.»
- «Имя. Просьба. Молитва.» — «Имя. Просьба. Молитва. Напишите имя того, за кого болит сердце.»
- «Когда тяжело» — «Бывают дни, когда сил нет, а слова не выходят. Просто напишите имя — мы помолимся за вас.»

Формат полей: title ≤40, text ≤2000, about ≤115 символов.

Возвращай ТОЛЬКО JSON следующей структуры, без преамбулы и Markdown:
{
  "variants": [
    {"title": "...", "text": "...", "about": "...", "cta": "write"},
    {"title": "...", "text": "...", "about": "...", "cta": "write"}
  ]
}

Длины строго в лимитах VK. Количество элементов в variants должно соответствовать запросу."""


class CopywriterError(Exception):
    """Ошибка генерации копирайта."""


class ClaudeCopywriter:
    """Генератор рекламных текстов через Claude."""

    def __init__(self, api_key: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def generate_copy(self, theme: str, extra_context: str = "") -> AdCopy:
        """Сгенерировать одно объявление по теме (legacy, для одного варианта)."""
        user_message = f"Тема: {theme}"
        if extra_context:
            user_message += f"\n\nКонтекст: {extra_context}"
        user_message += "\n\nСгенерируй одно объявление."

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=2500,
                system=COPYWRITER_SYSTEM_PROMPT_SINGLE,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            raise CopywriterError(f"Claude API недоступен: {e}") from e

        return self._parse_single(message.content[0].text)  # type: ignore

    async def generate_copy_variants(
        self, theme: str, n: int = 4, extra_context: str = ""
    ) -> list[AdCopy]:
        """Сгенерировать N разных вариантов объявления для A/B-выбора.

        Args:
            theme: краткое описание темы
            n: сколько вариантов (рекомендуется 3-5)
            extra_context: доп. контекст (что за проект, ссылка)

        Returns:
            Список AdCopy (длина = n при успехе, может быть меньше если Claude
            прислал меньше вариантов).
        """
        if n < 1 or n > 8:
            raise ValueError("n должно быть от 1 до 8")

        user_message = f"Тема: {theme}\n\nСгенерируй ровно {n} разных вариантов."
        if extra_context:
            user_message = f"Тема: {theme}\n\nКонтекст: {extra_context}\n\nСгенерируй ровно {n} разных вариантов."

        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=COPYWRITER_SYSTEM_PROMPT_VARIANTS,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            raise CopywriterError(f"Claude API недоступен: {e}") from e

        return self._parse_variants(message.content[0].text)  # type: ignore

    @staticmethod
    def _parse_single(raw: str) -> AdCopy:
        """Парсит JSON одного варианта в AdCopy."""
        cleaned = (
            raw.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Не распарсил JSON: {raw[:300]}")
            raise CopywriterError(f"Claude вернул не JSON: {e}") from e

        try:
            return AdCopy(
                title=str(data["title"])[:40],
                text=str(data["text"])[:2000],
                about=str(data["about"])[:115],
                cta=str(data.get("cta", "write")),
            )
        except KeyError as e:
            raise CopywriterError(f"В ответе нет поля {e}: {data}") from e

    @staticmethod
    def _parse_variants(raw: str) -> list[AdCopy]:
        """Парсит JSON со списком вариантов в [AdCopy]."""
        cleaned = (
            raw.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.error(f"Не распарсил JSON вариантов: {raw[:500]}")
            raise CopywriterError(f"Claude вернул не JSON: {e}") from e

        variants_raw = data.get("variants", [])
        if not isinstance(variants_raw, list) or not variants_raw:
            raise CopywriterError(f"В ответе нет вариантов: {data}")

        result = []
        for i, v in enumerate(variants_raw):
            if not isinstance(v, dict):
                logger.warning(f"Вариант {i} не словарь: {v}")
                continue
            try:
                result.append(AdCopy(
                    title=str(v["title"])[:40],
                    text=str(v["text"])[:2000],
                    about=str(v["about"])[:115],
                    cta=str(v.get("cta", "write")),
                ))
            except KeyError as e:
                logger.warning(f"В варианте {i} нет поля {e}: {v}")
                continue

        if not result:
            raise CopywriterError("Не удалось распарсить ни одного варианта")
        return result


def fallback_copy_from_caption(caption: str) -> AdCopy:
    """Если Claude недоступен — собираем AdCopy из caption напрямую."""
    caption = caption.strip()
    first_line = caption.split("\n", 1)[0].strip()
    title = first_line[:40] if first_line else "Напишите имена"
    text = caption[:2000] if caption else "Напишите имена близких — за них помолятся."
    about = first_line[:115] if first_line else "Православная община"
    return AdCopy(title=title, text=text, about=about, cta="write")
