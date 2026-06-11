from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    max_token: str
    max_device_id: str
    tg_bot_token: str
    tg_forum_channel_id: int
    fallback_user_id: int
    database_url: str = "sqlite+aiosqlite:///app/data/bridge.db"
    redis_url: str = "redis://redis:6379/0"
    tg_rate_limit_delay_sec: float = 3.5
    max_rate_limit_delay_sec: float = 1.0
    ls_topic_prefix: str = "👤 "
    max_reconnect_fetch_limit: int = 50
    log_level: str = "INFO"
    data_dir: str = ""
    max_session_name: str = "max_session.db"

    @property
    def sqlite_path(self) -> str:
        if self.database_url.startswith("sqlite+aiosqlite:///"):
            return self.database_url.removeprefix("sqlite+aiosqlite:///")
        return "app/data/bridge.db"

    @model_validator(mode="after")
    def _default_data_dir(self) -> "Settings":
        if not self.data_dir:
            object.__setattr__(self, "data_dir", str(Path(self.sqlite_path).parent))
        return self
