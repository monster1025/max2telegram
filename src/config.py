import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    max_phone: str
    max_work_dir: str
    telegram_bot_token: str
    telegram_fallback_user_id: str
    sqlite_path: str


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Environment variable {name} is required")
    return value


def load_settings() -> Settings:
    return Settings(
        max_phone=_require_env("MAX_PHONE"),
        max_work_dir=os.getenv("MAX_WORK_DIR", "cache").strip() or "cache",
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        telegram_fallback_user_id=_require_env("TELEGRAM_FALLBACK_USER_ID"),
        sqlite_path=os.getenv("SQLITE_PATH", "max2telegram.db").strip() or "max2telegram.db",
    )
