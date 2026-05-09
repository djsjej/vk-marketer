"""Загрузка и валидация конфигурации из переменных окружения."""

from pydantic import Field
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
    vk_ads_token: str = Field(..., description="Access token VK Ads API")
    vk_ads_account_id: int = Field(..., description="ID рекламного аккаунта")
    vk_ads_client_id: int | None = Field(None, description="ID клиента (для агентств)")

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
    test_campaign_budget_rub: int = Field(200)
    hourly_no_click_threshold_rub: int = Field(300)
    auto_launch_limit_rub: int = Field(500)

    # --- Schedule ---
    morning_report_time: str = Field("09:00")
    tz: str = Field("Europe/Moscow")
    metrics_check_interval_min: int = Field(15)

    # --- Logging ---
    log_level: str = Field("INFO")


# Singleton — импортируется модулями
settings = Settings()  # type: ignore
