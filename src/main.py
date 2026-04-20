import asyncio
import logging

from pymax import MaxClient, Message
from dotenv import load_dotenv

from bridge import MaxToTelegramBridge
from config import load_settings
from health import HealthState
from health_web import start_health_server
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
    health = HealthState(unhealthy_after_sec=15 * 60)
    start_health_server(host="0.0.0.0", port=5000, health=health)

    reverse_bridge = TelegramToMaxBridge(max_client=max_client, telegram=telegram_client, storage=storage, health=health)
    logger = logging.getLogger("max2telegram")
    reverse_bridge_task: asyncio.Task[None] | None = None
    max_probe_task: asyncio.Task[None] | None = None

    @max_client.on_start
    async def on_start() -> None:
        nonlocal reverse_bridge_task, max_probe_task
        logger.info("Max client started as %s", max_client.me.id)
        health.mark_max_ok()
        if reverse_bridge_task is None or reverse_bridge_task.done():
            reverse_bridge_task = asyncio.create_task(reverse_bridge.start(), name="reverse-bridge")
        if max_probe_task is None or max_probe_task.done():
            max_probe_task = asyncio.create_task(
                _max_probe_loop(max_client=max_client, health=health),
                name="max-probe-loop",
            )

    @max_client.on_message()
    async def on_message(message: Message) -> None:
        health.mark_max_event()
        try:
            await bridge.forward_message(message)
        except Exception as exc:
            logger.exception("Failed to forward Max message")
            await bridge.notify_delivery_failure(message, exc)

    asyncio.run(max_client.start())


async def _max_probe_loop(*, max_client: MaxClient, health: HealthState) -> None:
    # Best-effort контроль соединения: периодически дергаем API.
    # Если PyMax разорвет соединение/сломается сессия, это обычно проявится как исключение.
    await asyncio.sleep(2)
    while True:
        try:
            me = getattr(max_client, "me", None)
            my_id = getattr(me, "id", None)
            if my_id is not None:
                await max_client.get_user(user_id=my_id)
            health.mark_max_ok()
        except Exception:
            health.mark_max_error()
            logger = logging.getLogger("max2telegram")
            logger.exception("Max probe failed")
        await asyncio.sleep(60)


if __name__ == "__main__":
    main()
