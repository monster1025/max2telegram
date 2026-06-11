import asyncio

import aiohttp
import structlog
from pymax import File, Photo, Video

from app.config import Settings
from app.media_transfer import download_media_item, tmp_dir
from app.max_layer.client_holder import MaxClientHolder
from app.models.tasks import SetReactionTask, Tg2MaxTask
from app.queue.protocols import QueuePort
from app.router.router import MessageRouter
from app.storage.protocols import StoragePort
from app.telegram_layer.bot_holder import BotHolder

logger = structlog.get_logger(__name__)


class MaxWorker:
  def __init__(
    self,
    settings: Settings,
    holder: MaxClientHolder,
    bot_holder: BotHolder,
    queue: QueuePort,
    router: MessageRouter,
    storage: StoragePort,
  ) -> None:
    self._settings = settings
    self._holder = holder
    self._bot_holder = bot_holder
    self._queue = queue
    self._router = router
    self._storage = storage
    self._running = True
    self._tmp_dir = tmp_dir(self._settings.data_dir)

  async def run(self) -> None:
    logger.info("max_worker_started")
    while self._running:
      task = await self._queue.dequeue_tg2max(timeout=5)
      if task is None:
        continue
      logger.info(
        "max_worker_task_received",
        tg_message_id=task.tg_message_id,
        max_chat_id=task.max_chat_id,
        media_count=len(task.media),
      )
      try:
        await self._process(task)
      except Exception as exc:
        logger.exception("max_worker_failed", tg_message_id=task.tg_message_id)
        await self._router.notify_error(
          "Ошибка отправки в MAX",
          f"tg_message_id={task.tg_message_id}: {exc}",
        )
      await asyncio.sleep(self._settings.max_rate_limit_delay_sec)

  async def _process(self, task: Tg2MaxTask) -> None:
    client = await self._holder.wait_client()
    attachments = await self._build_attachments(task.media)
    logger.debug(
      "max_worker_sending",
      max_chat_id=task.max_chat_id,
      attachment_count=len(attachments),
      text_len=len(task.text),
      reply_to=task.reply_to_max_message_id,
    )

    sent = await client.send_message(
      chat_id=task.max_chat_id,
      text=task.text,
      reply_to=task.reply_to_max_message_id,
      attachments=attachments or None,
    )
    if sent is None:
      raise RuntimeError("MAX API returned no message")

    await self._storage.save_message_link(
      task.max_chat_id,
      sent.id,
      task.tg_chat_id,
      task.tg_thread_id,
      task.tg_message_id,
    )
    await self._storage.update_sync_marker(task.max_chat_id, sent.id)

    await self._queue.enqueue_max2tg(
      SetReactionTask(
        tg_chat_id=task.tg_chat_id,
        tg_message_id=task.tg_message_id,
        tg_thread_id=task.tg_thread_id,
      )
    )
    logger.info(
      "max_message_sent",
      max_chat_id=task.max_chat_id,
      max_message_id=sent.id,
      tg_message_id=task.tg_message_id,
    )

  async def _build_attachments(self, media: list) -> list:
    if not media:
      return []
    bot = await self._bot_holder.wait_bot()
    result = []
    async with aiohttp.ClientSession() as session:
      for item in media:
        kind = item.kind if hasattr(item, "kind") else item.get("kind", "")
        if kind == "unsupported":
          continue
        path = await download_media_item(bot, session, item, self._tmp_dir)
        if path is None:
          logger.warning(
            "max_worker_media_skipped",
            kind=kind,
            file_id=getattr(item, "file_id", None) or item.get("file_id"),
            url=getattr(item, "url", None) or item.get("url"),
          )
          continue
        if "photo" in kind:
          result.append(Photo(path=str(path)))
        elif "video" in kind:
          result.append(Video(path=str(path)))
        else:
          result.append(File(path=str(path)))
    return result
