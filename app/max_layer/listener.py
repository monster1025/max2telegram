import asyncio
from pathlib import Path

import structlog
from pymax import ExtraConfig, Message, WebClient
from pymax.types.domain.enums import ChatType

from app.config import Settings
from app.media_transfer import download_max_media, tmp_dir
from app.max_layer.client_holder import MaxClientHolder
from app.max_layer.formatter import (
  build_chat_title,
  extract_forwarded_content,
  format_forwarded_text,
  resolve_media,
  resolve_raw_attaches,
  resolve_sender_name,
)
from app.models.domain import MaxIncomingMessage
from app.router.router import MessageRouter
from app.storage.protocols import StoragePort

logger = structlog.get_logger(__name__)


class MaxListener:
  def __init__(
    self,
    settings: Settings,
    holder: MaxClientHolder,
    router: MessageRouter,
    storage: StoragePort,
  ) -> None:
    self._settings = settings
    self._holder = holder
    self._router = router
    self._storage = storage
    self._client: WebClient | None = None
    self._tmp_dir = tmp_dir(self._settings.data_dir)

  def build_client(self) -> WebClient:
    work_dir = str(Path(self._settings.data_dir))
    extra = ExtraConfig(
      token=self._settings.max_token,
      device_id=self._settings.max_device_id,
      log_level=self._settings.log_level,
      reconnect=True,
      reconnect_delay=3.0,
      telemetry=False,
    )
    client = WebClient(
      session_name=self._settings.max_session_name,
      work_dir=work_dir,
      extra_config=extra,
    )
    self._register_handlers(client)
    return client

  def _register_handlers(self, client: WebClient) -> None:
    @client.on_start()
    async def on_start(c: WebClient) -> None:
      my_id = c.me.contact.id if c.me else None
      self._holder.set_client(c, my_id)
      logger.info("max_client_started", user_id=my_id)
      await self._catch_up_history(c)

    @client.on_message()
    async def on_message(message: Message, c: WebClient) -> None:
      logger.debug(
        "max_message_event",
        chat_id=message.chat_id,
        message_id=message.id,
        sender=message.sender,
        msg_type=message.type,
        has_text=bool(message.text),
        attach_count=len(message.attaches),
        is_forward=bool(getattr(message, "link", None)),
      )
      try:
        await self._process_message(c, message)
      except Exception:
        logger.exception(
          "max_message_handler_failed",
          chat_id=message.chat_id,
          message_id=message.id,
        )

  async def run(self) -> None:
    self._client = self.build_client()
    await self._client.start()

  async def _catch_up_history(self, client: WebClient) -> None:
    if not client.chats:
      logger.info("max_history_catch_up_skipped", reason="no_chats")
      return
    limit = self._settings.max_reconnect_fetch_limit
    logger.info("max_history_catch_up_started", chat_count=len(client.chats), limit=limit)
    for chat in client.chats:
      try:
        messages = await client.fetch_history(chat_id=chat.id, backward=limit)
        if not messages:
          logger.debug("max_history_empty", chat_id=chat.id)
          continue
        logger.info("max_history_fetched", chat_id=chat.id, count=len(messages))
        for msg in sorted(messages, key=lambda m: m.id):
          await self._process_message(client, msg)
      except Exception:
        logger.exception("max_history_fetch_failed", chat_id=chat.id)

  async def _process_message(self, client: WebClient, message: Message) -> None:
    if message.chat_id is None:
      logger.debug("max_message_skipped", reason="no_chat_id", message_id=message.id)
      return
    if self._holder.my_user_id and message.sender == self._holder.my_user_id:
      link = await self._storage.get_tg_message_by_max(
        message.chat_id, message.id
      )
      if link is not None:
        logger.debug(
          "max_message_skipped",
          reason="own_echo",
          chat_id=message.chat_id,
          message_id=message.id,
        )
        return

    forwarded = extract_forwarded_content(message)
    effective_text = message.text or ""
    forwarded_attaches: list | None = None
    if forwarded is not None:
      nested_text, nested_attaches = forwarded
      original_sender_name: str | None = None
      nested = getattr(message, "link", {}).get("message", {})
      nested_sender = nested.get("sender") if isinstance(nested, dict) else None
      if nested_sender:
        try:
          user = await client.get_user(nested_sender)
          original_sender_name = resolve_sender_name(user)
        except Exception:
          original_sender_name = f"User {nested_sender}"
          logger.warning(
            "max_forward_sender_lookup_failed",
            chat_id=message.chat_id,
            message_id=message.id,
            nested_sender=nested_sender,
          )
      effective_text = format_forwarded_text(nested_text, original_sender_name)
      forwarded_attaches = nested_attaches

    has_content = bool(
      effective_text.strip()
      or message.attaches
      or forwarded_attaches
    )
    if not has_content:
      logger.debug(
        "max_message_skipped",
        reason="empty",
        chat_id=message.chat_id,
        message_id=message.id,
        is_forward=forwarded is not None,
      )
      return

    try:
      chat = await client.get_chat(message.chat_id)
    except Exception:
      logger.exception("max_chat_fetch_failed", chat_id=message.chat_id)
      return

    is_dm = chat.type == ChatType.DIALOG or getattr(chat, "is_dialog", False)
    chat_title, _ = build_chat_title(chat, self._settings.ls_topic_prefix)

    sender_name: str | None = None
    if not is_dm and message.sender:
      try:
        user = await client.get_user(message.sender)
        sender_name = resolve_sender_name(user)
      except Exception:
        sender_name = f"User {message.sender}"

    reply_to: int | None = None
    if message.options and isinstance(message.options, dict):
      reply_to = message.options.get("replyTo")
    if reply_to is None and message.prev_message_id:
      try:
        reply_to = int(message.prev_message_id)
      except (TypeError, ValueError):
        reply_to = None

    try:
      if forwarded_attaches is not None:
        # File API requires the forward wrapper message id in this chat,
        # not the nested original message id (error.user.file.access).
        media = await resolve_raw_attaches(
          client, message.chat_id, message.id, forwarded_attaches
        )
      else:
        media = await resolve_media(client, message)
      media = await download_max_media(client, media, self._tmp_dir)
    except Exception:
      logger.exception(
        "max_media_resolve_failed",
        chat_id=message.chat_id,
        message_id=message.id,
      )
      media = []

    logger.info(
      "max_message_processing",
      chat_id=message.chat_id,
      message_id=message.id,
      is_forward=forwarded is not None,
      text_len=len(effective_text),
      media_count=len(media),
      is_dm=is_dm,
    )

    incoming = MaxIncomingMessage(
      max_chat_id=message.chat_id,
      max_message_id=message.id,
      text=effective_text,
      sender_id=message.sender,
      sender_name=sender_name,
      is_dm=is_dm,
      chat_title=chat_title,
      reply_to_max_message_id=reply_to,
      media=media,
    )
    try:
      await self._router.handle_max_message(incoming)
    except Exception:
      logger.exception(
        "max_route_failed",
        chat_id=message.chat_id,
        message_id=message.id,
      )
      await self._router.notify_error(
        "Ошибка маршрутизации MAX → TG",
        f"chat_id={message.chat_id} message_id={message.id}",
      )
