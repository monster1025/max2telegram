from typing import Any

import structlog
from pymax.types.domain.message import Message as MaxMessage

from app.models.domain import MaxIncomingMessage

logger = structlog.get_logger(__name__)

def get_forward_link(message: MaxMessage) -> dict[str, Any] | None:
  link = getattr(message, "link", None)
  if not isinstance(link, dict):
    return None
  if str(link.get("type", "")).upper() != "FORWARD":
    return None
  return link


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


def build_chat_title(chat, ls_prefix: str) -> tuple[str, bool]:
  is_dm = bool(getattr(chat, "is_dialog", False) or chat.type == "DIALOG")
  if is_dm:
    title = chat.title or "Контакт"
    if not title.startswith(ls_prefix.strip()):
      title = f"{ls_prefix}{title}"
    return title, True
  return chat.title or f"Чат {chat.id}", False


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
