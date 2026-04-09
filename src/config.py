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
    max_work_dir = os.getenv("MAX_WORK_DIR", "cache").strip() or "cache"
    sqlite_env = os.getenv("SQLITE_PATH", "").strip()
    sqlite_default = os.path.join(max_work_dir, "max2telegram.db")
    sqlite_path = sqlite_env or sqlite_default

    return Settings(
        max_phone=_require_env("MAX_PHONE"),
        max_work_dir=max_work_dir,
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        telegram_fallback_user_id=_require_env("TELEGRAM_FALLBACK_USER_ID"),
        sqlite_path=sqlite_path,
    )
