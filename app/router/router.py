import structlog

from app.models.domain import MaxIncomingMessage, TgIncomingMessage
from app.models.tasks import Max2TgTask, MediaItem, NotifyFallbackTask, Tg2MaxTask
from app.queue.protocols import QueuePort
from app.storage.protocols import StoragePort
from app.topic_locks import TopicLockRegistry

logger = structlog.get_logger(__name__)


class MessageRouter:
  """Маршрутизация сообщений между MAX и Telegram через очереди и хранилище."""

  def __init__(
    self,
    storage: StoragePort,
    queue: QueuePort,
    forum_channel_id: int,
    topic_locks: TopicLockRegistry,
  ) -> None:
    self._storage = storage
    self._queue = queue
    self._forum_channel_id = forum_channel_id
    self._topic_locks = topic_locks

  async def handle_max_message(self, message: MaxIncomingMessage) -> bool:
    logger.debug(
      "router_max_message_received",
      max_chat_id=message.max_chat_id,
      message_id=message.max_message_id,
      text_len=len(message.text),
      media_count=len(message.media),
    )
    if message.max_chat_id is None or message.max_message_id is None:
      logger.warning(
        "router_max_message_rejected",
        reason="missing_ids",
        max_chat_id=message.max_chat_id,
        message_id=message.max_message_id,
      )
      return False

    async with self._topic_locks.lock(message.max_chat_id):
      mapping = await self._storage.get_mapping_by_max_chat(message.max_chat_id)
    needs_new_topic = mapping is None
    if mapping is not None:
      logger.debug(
        "router_mapping_found",
        max_chat_id=message.max_chat_id,
        thread_id=mapping.tg_thread_id,
      )
    else:
      all_mappings = await self._storage.list_mappings()
      logger.info(
        "router_no_mapping",
        max_chat_id=message.max_chat_id,
        reason="chat_mapping_not_found_in_storage",
        will_create_topic=True,
        total_mappings_in_storage=len(all_mappings),
        known_max_chat_ids=[m.max_chat_id for m in all_mappings] if all_mappings else [],
        hint=(
          "empty_db_after_container_recreate"
          if not all_mappings
          else "mapping_missing_for_this_chat_only"
        ),
      )

    reply_to_tg: int | None = None
    if message.reply_to_max_message_id is not None:
      link = await self._storage.get_tg_message_by_max(
        message.max_chat_id, message.reply_to_max_message_id
      )
      if link:
        reply_to_tg = link[2]
        logger.debug(
          "router_reply_mapped",
          max_reply_to=message.reply_to_max_message_id,
          tg_reply_to=reply_to_tg,
        )
      else:
        logger.debug(
          "router_reply_not_found",
          max_chat_id=message.max_chat_id,
          max_reply_to=message.reply_to_max_message_id,
        )

    media = [MediaItem.model_validate(item) for item in message.media]

    task = Max2TgTask(
      max_chat_id=message.max_chat_id,
      max_message_id=message.max_message_id,
      text=message.text,
      sender_name=message.sender_name,
      is_dm=message.is_dm,
      chat_title=message.chat_title,
      chat_name=message.chat_name,
      chat_icon_url=message.chat_icon_url,
      chat_icon_local_path=message.chat_icon_local_path,
      participants_count=message.participants_count,
      max_chat_link=message.max_chat_link,
      needs_new_topic=needs_new_topic,
      reply_to_tg_message_id=reply_to_tg,
      media=media,
    )
    await self._queue.enqueue_max2tg(task)
    logger.info(
      "max_message_enqueued",
      max_chat_id=message.max_chat_id,
      message_id=message.max_message_id,
      needs_new_topic=needs_new_topic,
    )
    return True

  async def handle_tg_message(self, message: TgIncomingMessage) -> bool:
    logger.debug(
      "router_tg_message_received",
      tg_chat_id=message.tg_chat_id,
      tg_message_id=message.tg_message_id,
      thread_id=message.tg_thread_id,
      text_len=len(message.text),
    )
    if message.is_bot:
      logger.debug("router_tg_message_skipped", reason="is_bot")
      return False

    if message.tg_chat_id != self._forum_channel_id:
      logger.debug(
        "router_tg_message_skipped",
        reason="wrong_chat",
        tg_chat_id=message.tg_chat_id,
      )
      return False

    if message.tg_thread_id is None:
      logger.debug("router_tg_message_skipped", reason="no_thread_id")
      return False

    mapping = await self._storage.get_mapping_by_tg_thread(
      message.tg_chat_id, message.tg_thread_id
    )
    if mapping is None:
      logger.debug(
        "tg_message_no_mapping",
        tg_chat_id=message.tg_chat_id,
        thread_id=message.tg_thread_id,
      )
      return False

    reply_to_max: int | None = None
    if message.reply_to_tg_message_id is not None:
      link = await self._storage.get_max_message_by_tg(
        message.tg_chat_id, message.reply_to_tg_message_id
      )
      if link:
        reply_to_max = link[1]

    media = [MediaItem.model_validate(item) for item in message.media]

    task = Tg2MaxTask(
      max_chat_id=mapping.max_chat_id,
      text=message.text,
      tg_chat_id=message.tg_chat_id,
      tg_message_id=message.tg_message_id,
      tg_thread_id=message.tg_thread_id,
      reply_to_max_message_id=reply_to_max,
      media=media,
    )
    await self._queue.enqueue_tg2max(task)
    logger.info(
      "tg_message_enqueued",
      tg_message_id=message.tg_message_id,
      max_chat_id=mapping.max_chat_id,
    )
    return True

  async def notify_error(self, title: str, details: str) -> None:
    logger.error("router_notify_error", title=title, details=details)
    await self._queue.enqueue_max2tg(
      NotifyFallbackTask(title=title, details=details)
    )
