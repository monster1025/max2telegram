import asyncio
import logging

from pymax import MaxClient, Message
from dotenv import load_dotenv

from bridge import MaxToTelegramBridge
from config import load_settings
from reverse_bridge import TelegramToMaxBridge
from storage import BridgeStorage
from telegram_api import TelegramClient


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_client() -> tuple[MaxClient, MaxToTelegramBridge]:
    settings = load_settings()
    max_client = MaxClient(
        phone=settings.max_phone,
        work_dir=settings.max_work_dir,
    )
    telegram_client = TelegramClient(
        bot_token=settings.telegram_bot_token,
        fallback_user_id=settings.telegram_fallback_user_id,
    )
    storage = BridgeStorage(settings.sqlite_path)
    bridge = MaxToTelegramBridge(max_client=max_client, telegram=telegram_client, storage=storage)
    return max_client, bridge


def main() -> None:
    load_dotenv()
    _setup_logging()
    settings = load_settings()
    max_client = MaxClient(phone=settings.max_phone, work_dir=settings.max_work_dir)
    telegram_client = TelegramClient(
        bot_token=settings.telegram_bot_token,
        fallback_user_id=settings.telegram_fallback_user_id,
    )
    storage = BridgeStorage(settings.sqlite_path)
    bridge = MaxToTelegramBridge(max_client=max_client, telegram=telegram_client, storage=storage)
    reverse_bridge = TelegramToMaxBridge(max_client=max_client, telegram=telegram_client, storage=storage)
    logger = logging.getLogger("max2telegram")

    @max_client.on_start
    async def on_start() -> None:
        logger.info("Max client started as %s", max_client.me.id)
        asyncio.create_task(reverse_bridge.start())

    @max_client.on_message()
    async def on_message(message: Message) -> None:
        try:
            await bridge.forward_message(message)
        except Exception:
            logger.exception("Failed to forward Max message")

    asyncio.run(max_client.start())


if __name__ == "__main__":
    main()
