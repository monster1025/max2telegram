import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.types import Message

from app.config import Settings
from app.router.router import MessageRouter
from app.storage.protocols import StoragePort
from app.telegram_layer.admin import register_admin_handlers
from app.telegram_layer.bot_holder import BotHolder
from app.telegram_layer.formatter import build_tg_incoming, format_tg_to_max_text

logger = structlog.get_logger(__name__)


class TelegramListener:
  def __init__(
    self,
    settings: Settings,
    bot_holder: BotHolder,
    router: MessageRouter,
    storage: StoragePort,
    max_holder,
  ) -> None:
    self._settings = settings
    self._bot_holder = bot_holder
    self._router = router
    self._storage = storage
    self._max_holder = max_holder
    self._bot = Bot(token=settings.tg_bot_token)
    self._dp = Dispatcher()

  async def run(self) -> None:
    self._bot_holder.set_bot(self._bot)
    register_admin_handlers(
      self._dp,
      self._settings,
      self._storage,
      self._max_holder,
    )
    self._dp.message.register(
      self._on_forum_message,
      F.chat.id == self._settings.tg_forum_channel_id,
      F.chat.type == ChatType.SUPERGROUP,
    )
    logger.info("telegram_listener_started")
    try:
      await self._dp.start_polling(self._bot)
    except Exception:
      logger.exception("telegram_polling_failed")
      raise

  async def _on_forum_message(self, message: Message) -> None:
    logger.debug(
      "tg_forum_message_received",
      message_id=message.message_id,
      thread_id=message.message_thread_id,
      has_text=bool(message.text or message.caption),
    )
    incoming = build_tg_incoming(message, self._settings.tg_forum_channel_id)
    if incoming is None:
      logger.debug("tg_forum_message_skipped", message_id=message.message_id)
      return
    incoming.text = format_tg_to_max_text(
      incoming.author_name,
      incoming.author_username,
      incoming.text,
    )
    try:
      await self._router.handle_tg_message(incoming)
    except Exception:
      logger.exception("tg_route_failed", message_id=message.message_id)
      await self._router.notify_error(
        "Ошибка маршрутизации TG → MAX",
        f"message_id={message.message_id}",
      )
