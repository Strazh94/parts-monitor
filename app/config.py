"""Настройки приложения."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://monitor:monitor@localhost:5432/parts_monitor"
    debug: bool = False
    # Часовой пояс для ежедневного запуска (МСК)
    parse_hour_msk: int = 21
    parse_minute_msk: int = 0

    model_config = {"env_file": ".env", "env_prefix": "PM_"}


settings = Settings()
