import asyncio
from pathlib import Path
from uuid import uuid4

import aiohttp
import structlog
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
  FSInputFile,
  InputMediaDocument,
  InputMediaPhoto,
  InputMediaVideo,
  ReactionTypeEmoji,
)

from app.config import Settings
from app.media_transfer import (
  cleanup_paths,
  download_max_image_url,
  download_media_item,
  resolve_file_name,
  tmp_dir,
)
from app.max_layer.client_holder import MaxClientHolder
from app.max_layer.formatter import format_max_text
from app.models.domain import ChatMapping, MaxIncomingMessage
from app.models.tasks import (
  Max2TgTask,
  MediaItem,
  NotifyFallbackTask,
  SetReactionTask,
)
from app.queue.protocols import QueuePort
from app.storage.protocols import StoragePort
from app.telegram_layer.bot_holder import BotHolder
from app.telegram_layer.formatter import format_topic_pin_text
from app.topic_locks import TopicLockRegistry

logger = structlog.get_logger(__name__)


def _is_thread_not_found(exc: TelegramBadRequest) -> bool:
  return "message thread not found" in exc.message.lower()


class TelegramWorker:
  def __init__(
    self,
    settings: Settings,
    bot_holder: BotHolder,
    max_holder: MaxClientHolder,
    queue: QueuePort,
    storage: StoragePort,
    topic_locks: TopicLockRegistry,
  ) -> None:
    self._settings = settings
    self._bot_holder = bot_holder
    self._max_holder = max_holder
    self._queue = queue
    self._storage = storage
    self._topic_locks = topic_locks
    self._running = True
    self._tmp_dir = tmp_dir(self._settings.data_dir)

  async def run(self) -> None:
    logger.info("tg_worker_started")
    while self._running:
      task = await self._queue.dequeue_max2tg(timeout=5)
      if task is None:
        continue
      logger.info("tg_worker_task_received", task_kind=task.kind)
      try:
        if isinstance(task, Max2TgTask):
          await self._send_to_tg(task)
        elif isinstance(task, SetReactionTask):
          await self._set_reaction(task)
        elif isinstance(task, NotifyFallbackTask):
          await self._notify_fallback(task)
      except Exception as exc:
        logger.exception("tg_worker_failed", task_kind=task.kind)
        if isinstance(task, Max2TgTask):
          await self._queue.enqueue_max2tg(
            NotifyFallbackTask(
              title="Ошибка отправки в Telegram",
              details=f"max_message_id={task.max_message_id}: {exc}",
            )
          )
      await asyncio.sleep(self._settings.tg_rate_limit_delay_sec)

  async def _create_and_save_topic(
    self,
    bot,
    task: Max2TgTask,
    *,
    reason: str,
    old_thread_id: int | None = None,
  ) -> ChatMapping:
    topic = await bot.create_forum_topic(
      chat_id=self._settings.tg_forum_channel_id,
      name=task.chat_title[:128],
    )
    mapping = ChatMapping(
      max_chat_id=task.max_chat_id,
      tg_chat_id=self._settings.tg_forum_channel_id,
      tg_thread_id=topic.message_thread_id,
      display_name=task.chat_title,
      is_dm=task.is_dm,
    )
    await self._storage.save_mapping(mapping)
    await self._send_and_pin_topic_info(bot, topic.message_thread_id, task)
    logger.info(
      "tg_topic_created",
      max_chat_id=task.max_chat_id,
      thread_id=topic.message_thread_id,
      title=task.chat_title,
      reason=reason,
      old_thread_id=old_thread_id,
    )
    return mapping

  async def _send_and_pin_topic_info(
    self,
    bot,
    thread_id: int,
    task: Max2TgTask,
  ) -> None:
    chat_name = task.chat_name or task.chat_title
    caption = format_topic_pin_text(
      chat_name=chat_name,
      is_dm=task.is_dm,
      max_chat_id=task.max_chat_id,
      participants_count=task.participants_count,
      max_chat_link=task.max_chat_link,
    )
    kwargs: dict = {
      "chat_id": self._settings.tg_forum_channel_id,
      "message_thread_id": thread_id,
    }

    downloaded: Path | None = None
    icon_path: Path | None = None
    try:
      icon_path = await self._resolve_topic_icon_path(task)
      if icon_path is not None:
        sent = await bot.send_photo(
          photo=FSInputFile(icon_path),
          caption=caption,
          **kwargs,
        )
        if (
          not task.chat_icon_local_path
          or icon_path.resolve() != Path(task.chat_icon_local_path).resolve()
        ):
          downloaded = icon_path
      else:
        sent = await bot.send_message(text=caption, **kwargs)

      await bot.pin_chat_message(
        chat_id=self._settings.tg_forum_channel_id,
        message_id=sent.message_id,
        disable_notification=True,
      )
      logger.info(
        "tg_topic_info_pinned",
        max_chat_id=task.max_chat_id,
        thread_id=thread_id,
        message_id=sent.message_id,
        has_icon=icon_path is not None,
      )
    except Exception:
      logger.exception(
        "tg_topic_info_pin_failed",
        max_chat_id=task.max_chat_id,
        thread_id=thread_id,
      )
    finally:
      if downloaded is not None:
        cleanup_paths([downloaded])

  async def _resolve_topic_icon_path(self, task: Max2TgTask) -> Path | None:
    if task.chat_icon_local_path:
      local = Path(task.chat_icon_local_path)
      if local.is_file() and local.stat().st_size > 0:
        return local

    if not task.chat_icon_url:
      return None

    dest = self._tmp_dir / f"topic_icon_{task.max_chat_id}_{uuid4().hex}.jpg"
    async with aiohttp.ClientSession() as session:
      if await download_max_image_url(session, task.chat_icon_url, dest):
        return dest
    logger.warning(
      "tg_topic_icon_download_failed",
      max_chat_id=task.max_chat_id,
      url=task.chat_icon_url,
    )
    dest.unlink(missing_ok=True)
    return None

  async def _send_to_tg(self, task: Max2TgTask) -> None:
    bot = await self._bot_holder.wait_bot()
    logger.debug(
      "tg_worker_send_start",
      max_chat_id=task.max_chat_id,
      max_message_id=task.max_message_id,
      needs_new_topic=task.needs_new_topic,
      media_count=len(task.media),
    )

    async with self._topic_locks.lock(task.max_chat_id):
      mapping = await self._storage.get_mapping_by_max_chat(task.max_chat_id)
      if mapping is None:
        logger.info(
          "tg_topic_creating",
          max_chat_id=task.max_chat_id,
          title=task.chat_title,
          reason="no_mapping_in_storage_at_send_time",
          needs_new_topic_from_router=task.needs_new_topic,
          sqlite_path=self._settings.sqlite_path,
          hint="mapping_missing_in_sqlite_will_call_create_forum_topic",
        )
        mapping = await self._create_and_save_topic(
          bot, task, reason="no_mapping_in_storage"
        )
      elif task.needs_new_topic:
        logger.info(
          "tg_topic_reused",
          max_chat_id=task.max_chat_id,
          thread_id=mapping.tg_thread_id,
          reason="mapping_found_despite_needs_new_topic_flag",
          hint="mapping_appeared_between_router_enqueue_and_worker_send",
        )
      else:
        logger.debug(
          "tg_topic_reused",
          max_chat_id=task.max_chat_id,
          thread_id=mapping.tg_thread_id,
        )

    assert mapping is not None

    formatted = format_max_text(
      MaxIncomingMessage(
        max_chat_id=task.max_chat_id,
        max_message_id=task.max_message_id,
        text=task.text,
        sender_id=None,
        sender_name=task.sender_name,
        is_dm=task.is_dm,
        chat_title=task.chat_title,
        chat_name=task.chat_name,
        chat_icon_url=task.chat_icon_url,
        chat_icon_local_path=task.chat_icon_local_path,
        participants_count=task.participants_count,
        max_chat_link=task.max_chat_link,
        reply_to_max_message_id=None,
        media=[],
      )
    )

    reply_to = task.reply_to_tg_message_id
    try:
      sent = await self._dispatch_content(
        bot, mapping.tg_thread_id, formatted, task.media, reply_to
      )
    except TelegramBadRequest as exc:
      if not _is_thread_not_found(exc):
        raise
      old_thread_id = mapping.tg_thread_id
      logger.info(
        "tg_topic_stale",
        max_chat_id=task.max_chat_id,
        old_thread_id=old_thread_id,
        reason="telegram_message_thread_not_found",
        action="recreate_topic_and_update_binding",
      )
      async with self._topic_locks.lock(task.max_chat_id):
        mapping = await self._create_and_save_topic(
          bot,
          task,
          reason="telegram_thread_recreated",
          old_thread_id=old_thread_id,
        )
      sent = await self._dispatch_content(
        bot, mapping.tg_thread_id, formatted, task.media, None
      )
    if sent is None:
      raise RuntimeError("Telegram API returned no message")

    await self._storage.save_message_link(
      task.max_chat_id,
      task.max_message_id,
      mapping.tg_chat_id,
      mapping.tg_thread_id,
      sent.message_id,
    )
    logger.info(
      "tg_message_sent",
      max_message_id=task.max_message_id,
      tg_message_id=sent.message_id,
      thread_id=mapping.tg_thread_id,
    )

  async def _dispatch_content(
    self,
    bot,
    thread_id: int,
    text: str,
    media: list[MediaItem],
    reply_to: int | None,
  ):
    chat_id = self._settings.tg_forum_channel_id
    kwargs: dict = {
      "chat_id": chat_id,
      "message_thread_id": thread_id,
    }
    if reply_to is not None:
      kwargs["reply_to_message_id"] = reply_to

    if not media:
      return await bot.send_message(text=text or " ", **kwargs)

    downloaded: list = []
    try:
      max_client = self._max_holder.client
      async with aiohttp.ClientSession() as session:
        uploads: list[tuple[MediaItem, FSInputFile]] = []
        for item in media:
          path = await download_media_item(
            bot,
            session,
            item,
            self._tmp_dir,
            max_client=max_client,
          )
          if path is None:
            logger.warning(
              "tg_worker_media_skipped",
              kind=item.kind,
              file_id=item.file_id,
              url=item.url,
            )
            continue
          downloaded.append(path)
          uploads.append(
            (item, FSInputFile(path, filename=resolve_file_name(item)))
          )

        if not uploads:
          fallback = text or "[Telegram files]"
          return await bot.send_message(text=fallback, **kwargs)

        if len(uploads) == 1:
          item, upload = uploads[0]
          if item.kind == "photo":
            return await bot.send_photo(
              photo=upload, caption=text or None, **kwargs
            )
          if item.kind == "video":
            return await bot.send_video(
              video=upload, caption=text or None, **kwargs
            )
          return await bot.send_document(
            document=upload, caption=text or None, **kwargs
          )

        group = []
        for idx, (item, upload) in enumerate(uploads):
          caption = text if idx == 0 else None
          if item.kind == "photo":
            group.append(InputMediaPhoto(media=upload, caption=caption))
          elif item.kind == "video":
            group.append(InputMediaVideo(media=upload, caption=caption))
          else:
            group.append(InputMediaDocument(media=upload, caption=caption))

        messages = await bot.send_media_group(
          chat_id=chat_id,
          message_thread_id=thread_id,
          media=group,
        )
        return messages[0]
    finally:
      cleanup_paths(downloaded)

  async def _set_reaction(self, task: SetReactionTask) -> None:
    bot = await self._bot_holder.wait_bot()
    await bot.set_message_reaction(
      chat_id=task.tg_chat_id,
      message_id=task.tg_message_id,
      reaction=[ReactionTypeEmoji(emoji=task.emoji)],
    )
    logger.info("tg_reaction_set", message_id=task.tg_message_id)

  async def _notify_fallback(self, task: NotifyFallbackTask) -> None:
    bot = await self._bot_holder.wait_bot()
    text = f"⚠️ {task.title}\n\n{task.details}"
    await bot.send_message(chat_id=self._settings.fallback_user_id, text=text)
    logger.warning("fallback_notified", title=task.title)
