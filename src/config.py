"""Загрузка и валидация конфигурации из переменных окружения."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация приложения. Все значения берутся из env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(..., description="Токен бота от @BotFather")
    telegram_owner_id: int = Field(..., description="Telegram user_id владельца")

    # --- VK Реклама ---
    # Бот поддерживает два режима аутентификации:
    # 1. OAuth (предпочтительный): задаём VK_ADS_OAUTH_CLIENT_ID и VK_ADS_OAUTH_CLIENT_SECRET.
    #    Бот сам получает access_token через POST на target.my.com/api/v2/oauth2/token.json
    #    и обновляет его автоматически каждые ~24 часа.
    # 2. Прямой токен (legacy): задаём VK_ADS_TOKEN если уже есть готовый access_token.
    #    Если задан — используется напрямую без OAuth-обновления.
    vk_ads_token: str | None = Field(None, description="Access token (если уже есть)")
    vk_ads_oauth_client_id: str | None = Field(None, description="OAuth client_id")
    vk_ads_oauth_client_secret: str | None = Field(None, description="OAuth client_secret")
    vk_ads_account_id: int = Field(..., description="ID рекламного кабинета")
    vk_ads_agency_client_id: int | None = Field(None, description="ID клиента (для агентств)")
    vk_community_url_id: int | None = Field(
        None,
        description=(
            "VK ID сообщества — численный (например 216409501). "
            "Используется чтобы построить URL https://vk.com/club{id} "
            "если vk_community_url не задан напрямую."
        ),
    )
    vk_community_url: str | None = Field(
        None,
        description=(
            "Полный URL сообщества для рекламы (https://vk.com/pomolimsy). "
            "Используется приоритетно над vk_community_url_id. "
            "VK Ads сначала регистрирует этот URL и возвращает внутренний "
            "URL-ID, который потом ставится в ad_object_id."
        ),
    )

    # --- Шаблонная кампания (workaround) ---
    # Из-за особенности VK Ads API нельзя создавать кампанию с нуля через
    # POST /ad_plans.json (package_id 3122 имеет пустые settings.patterns).
    # Workaround: один раз вручную создаём «шаблонную» кампанию с группами
    # через UI кабинета, и бот добавляет в неё новые banner через
    # POST /banners.json (это работает без проблем).
    vk_template_ad_plan_id: int | None = Field(
        None,
        description="ID шаблонной кампании, в которую бот добавляет banner.",
    )
    vk_template_ad_group_ids: str | None = Field(
        None,
        description=(
            "ID групп шаблонной кампании через запятую "
            "(например '137881410,137893759,137893760'). Бот создаёт по одному "
            "banner в каждую группу."
        ),
    )

    @field_validator(
        "vk_ads_token",
        "vk_ads_oauth_client_id",
        "vk_ads_oauth_client_secret",
        "telegram_bot_token",
        "anthropic_api_key",
        mode="before",
    )
    @classmethod
    def _strip_whitespace(cls, v: object) -> object:
        """Убирает лишние пробелы и переносы строк из значений env vars.

        Когда пользователь копипастит ключи в Railway/iOS, иногда туда попадает
        невидимый '\\n' в конце. Это ломает HTTP-заголовки и сравнения строк.
        """
        if isinstance(v, str):
            return v.strip()
        return v

    # --- Claude ---
    anthropic_api_key: str = Field(..., description="API key Anthropic")
    anthropic_model: str = Field(
        "claude-sonnet-4-6",
        description="Модель Claude для анализа и копирайтинга",
    )

    # --- Database ---
    database_url: str = Field("sqlite:///./data/vk_marketer.db")

    # --- Safety / Budget ---
    max_daily_spend_rub: int = Field(2000)
    test_campaign_budget_rub: int = Field(100)
    hourly_no_click_threshold_rub: int = Field(300)
    auto_launch_limit_rub: int = Field(500)

    # --- Schedule ---
    morning_report_time: str = Field("09:00")
    tz: str = Field("Europe/Moscow")
    metrics_check_interval_min: int = Field(15)

    # --- Logging ---
    log_level: str = Field("INFO")

    @property
    def has_vk_oauth(self) -> bool:
        """OAuth настроен (можем сами генерить access_token)?"""
        return bool(self.vk_ads_oauth_client_id and self.vk_ads_oauth_client_secret)

    @property
    def has_vk_static_token(self) -> bool:
        """Статический токен задан (и не плейсхолдер)?"""
        return bool(self.vk_ads_token and self.vk_ads_token != "placeholder")

    @property
    def vk_configured(self) -> bool:
        """VK Ads клиент может работать (есть либо OAuth, либо прямой токен)."""
        return self.has_vk_oauth or self.has_vk_static_token

    @property
    def vk_template_group_ids_parsed(self) -> list[int]:
        """ID групп шаблонной кампании в виде списка int."""
        if not self.vk_template_ad_group_ids:
            return []
        return [
            int(x.strip())
            for x in self.vk_template_ad_group_ids.split(",")
            if x.strip().isdigit()
        ]

    @property
    def has_template_campaign(self) -> bool:
        """Template-кампания сконфигурирована.

        После уточнения от поддержки VK (May 2026) для workaround достаточно
        только VK_TEMPLATE_AD_PLAN_ID — бот создаёт новые ad_groups в эту
        кампанию через POST /ad_groups.json. Старая env var
        VK_TEMPLATE_AD_GROUP_IDS оставлена для обратной совместимости, но
        больше не нужна.
        """
        return self.vk_template_ad_plan_id is not None


# Singleton — импортируется модулями
settings = Settings()  # type: ignore
