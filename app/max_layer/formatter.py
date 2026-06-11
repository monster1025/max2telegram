from typing import Any

import structlog
from pymax.types.domain.message import Message as MaxMessage

from app.models.domain import MaxChatMeta, MaxIncomingMessage

logger = structlog.get_logger(__name__)

def get_forward_link(message: MaxMessage) -> dict[str, Any] | None:
  link = getattr(message, "link", None)
  if not isinstance(link, dict):
    return None
  if str(link.get("type", "")).upper() != "FORWARD":
    return None
  return link


def get_reply_target(message: MaxMessage) -> int | None:
  link = getattr(message, "link", None)
  if not isinstance(link, dict):
    return None
  if str(link.get("type", "")).upper() != "REPLY":
    return None
  target = link.get("messageId") or link.get("message_id")
  if target is None:
    return None
  try:
    return int(target)
  except (TypeError, ValueError):
    return None


def extract_forwarded_content(
  message: MaxMessage,
) -> tuple[str, list[dict[str, Any]]] | None:
  link = get_forward_link(message)
  if link is None:
    return None

  nested = link.get("message")
  if not isinstance(nested, dict):
    logger.warning(
      "max_forward_missing_nested_message",
      chat_id=message.chat_id,
      message_id=message.id,
    )
    return None

  text = nested.get("text") or ""
  attaches = nested.get("attaches") or []
  if not isinstance(attaches, list):
    attaches = []

  logger.info(
    "max_forward_extracted",
    chat_id=message.chat_id,
    message_id=message.id,
    nested_message_id=nested.get("id"),
    nested_sender=nested.get("sender"),
    text_len=len(text),
    attach_count=len(attaches),
  )
  return text, attaches


def format_forwarded_text(
  original_text: str,
  original_sender_name: str | None = None,
) -> str:
  header = "↪️ Переслано"
  if original_sender_name:
    header += f" от {original_sender_name}"
  if original_text:
    return f"{header}:\n{original_text}"
  return header


def _attach_type(attach: dict[str, Any]) -> str:
  return str(attach.get("_type") or attach.get("type") or "").upper()


async def resolve_raw_attaches(
  client,
  chat_id: int,
  message_id: int,
  attaches: list[dict[str, Any]],
) -> list[dict]:
  items: list[dict] = []
  for attach in attaches:
    if not isinstance(attach, dict):
      continue

    attach_type = _attach_type(attach)
    if attach_type == "PHOTO":
      url = attach.get("baseUrl") or attach.get("base_url")
      if url:
        items.append(
          {
            "kind": "photo",
            "url": url,
            "max_chat_id": chat_id,
            "max_message_id": message_id,
          }
        )
      continue

    if attach_type == "VIDEO":
      video_id = attach.get("videoId") or attach.get("video_id")
      if video_id is not None:
        items.append(
          {
            "kind": "video",
            "max_chat_id": chat_id,
            "max_message_id": message_id,
            "max_video_id": video_id,
          }
        )
      continue

    if attach_type == "FILE":
      file_id = attach.get("fileId") or attach.get("file_id")
      if file_id is not None:
        items.append(
          {
            "kind": "document",
            "file_name": attach.get("name"),
            "max_chat_id": chat_id,
            "max_message_id": message_id,
            "max_file_id": file_id,
          }
        )
      continue

    url = attach.get("baseUrl") or attach.get("base_url") or attach.get("url")
    if url:
      items.append(
        {
          "kind": "document",
          "url": url,
          "max_chat_id": chat_id,
          "max_message_id": message_id,
        }
      )

  logger.debug(
    "max_raw_attaches_resolved",
    chat_id=chat_id,
    message_id=message_id,
    input_count=len(attaches),
    resolved_count=len(items),
  )
  return items


async def resolve_media(client, message: MaxMessage) -> list[dict]:
  from pymax.types.domain.attachments.file import FileAttachment
  from pymax.types.domain.attachments.photo import PhotoAttachment
  from pymax.types.domain.attachments.video import VideoAttachment

  items: list[dict] = []
  chat_id = message.chat_id
  if chat_id is None:
    return items

  for attach in message.attaches:
    if isinstance(attach, dict):
      items.extend(
        await resolve_raw_attaches(client, chat_id, message.id, [attach])
      )
      continue
    if isinstance(attach, PhotoAttachment):
      items.append(
        {
          "kind": "photo",
          "url": attach.base_url,
          "max_chat_id": chat_id,
          "max_message_id": message.id,
        }
      )
    elif isinstance(attach, VideoAttachment):
      items.append(
        {
          "kind": "video",
          "max_chat_id": chat_id,
          "max_message_id": message.id,
          "max_video_id": attach.video_id,
        }
      )
    elif isinstance(attach, FileAttachment):
      items.append(
        {
          "kind": "document",
          "file_name": attach.name,
          "max_chat_id": chat_id,
          "max_message_id": message.id,
          "max_file_id": attach.file_id,
        }
      )
    else:
      url = getattr(attach, "base_url", None) or getattr(attach, "url", None)
      if url:
        items.append(
          {
            "kind": "document",
            "url": url,
            "max_chat_id": chat_id,
            "max_message_id": message.id,
          }
        )
  return items


def _pick_image_url(*values: str | None) -> str | None:
  for value in values:
    if value and value.strip():
      return value.strip()
  return None


def _chat_icon_url(chat) -> str | None:
  return _pick_image_url(
    getattr(chat, "base_icon_url", None),
    getattr(chat, "base_raw_icon_url", None),
  )


def _user_icon_url(user) -> str | None:
  return _pick_image_url(
    getattr(user, "base_url", None),
    getattr(user, "base_raw_url", None),
  )


async def resolve_chat_icon_url(
  client,
  chat,
  my_user_id: int | None,
) -> str | None:
  is_dm = bool(getattr(chat, "is_dialog", False) or chat.type == "DIALOG")
  if is_dm:
    for user_id in chat.participants or {}:
      if my_user_id and user_id == my_user_id:
        continue
      try:
        user = await client.get_user(user_id)
        icon_url = _user_icon_url(user)
        if icon_url:
          return icon_url
      except Exception:
        logger.debug(
          "max_dm_icon_lookup_failed",
          chat_id=chat.id,
          user_id=user_id,
        )
    return None

  return _chat_icon_url(chat)


def extract_chat_meta(chat, ls_prefix: str) -> MaxChatMeta:
  is_dm = bool(getattr(chat, "is_dialog", False) or chat.type == "DIALOG")
  chat_name = chat.title or ("Контакт" if is_dm else f"Чат {chat.id}")
  topic_title = chat_name
  if is_dm and not topic_title.startswith(ls_prefix.strip()):
    topic_title = f"{ls_prefix}{topic_title}"
  return MaxChatMeta(
    topic_title=topic_title,
    chat_name=chat_name,
    is_dm=is_dm,
    icon_url=_chat_icon_url(chat),
    participants_count=getattr(chat, "participants_count", 0) or 0,
    link=getattr(chat, "link", None),
  )


def build_chat_title(chat, ls_prefix: str) -> tuple[str, bool]:
  meta = extract_chat_meta(chat, ls_prefix)
  return meta.topic_title, meta.is_dm


def resolve_sender_name(user) -> str:
  if user is None:
    return "Неизвестный"
  if user.names:
    name = user.names[0]
    parts = [name.first_name, name.last_name]
    return " ".join(p for p in parts if p) or f"User {user.id}"
  return f"User {user.id}"


def format_max_text(message: MaxIncomingMessage) -> str:
  if message.is_dm:
    return message.text
  sender = message.sender_name or "Неизвестный"
  return f"{sender}:\n{message.text}"
