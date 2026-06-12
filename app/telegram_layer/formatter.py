from aiogram.types import Message

from app.models.domain import TgIncomingMessage

TG_CAPTION_MAX_LEN = 1024
TG_MESSAGE_MAX_LEN = 4096


def split_text_for_media_caption(text: str) -> tuple[str | None, str | None]:
  if not text:
    return None, None
  if len(text) <= TG_CAPTION_MAX_LEN:
    return text, None
  return None, text


def format_tg_author(message: Message) -> tuple[str, str | None]:
  user = message.from_user
  if user is None:
    return "Unknown", None
  parts = [user.first_name or "", user.last_name or ""]
  name = " ".join(p for p in parts if p).strip() or (user.username or "User")
  return name, user.username


def format_topic_pin_text(
  *,
  chat_name: str,
  is_dm: bool,
  max_chat_id: int,
  participants_count: int,
  max_chat_link: str | None,
) -> str:
  chat_type = "PRIVATE" if is_dm else "CHAT"
  lines = [
    f"{chat_name} · Тип: {chat_type}",
    f"id: {max_chat_id}",
    f"Участников: {participants_count}",
  ]
  if max_chat_link:
    lines.extend(["", f"🔗 {max_chat_link}"])
  return "\n".join(lines)


def format_tg_to_max_text(author_name: str, username: str | None, text: str) -> str:
  handle = f" (@{username})" if username else ""
  return f"{author_name}{handle}:\n{text}"


def extract_tg_media(message: Message) -> list[dict]:
  items: list[dict] = []
  if message.photo:
    photo = message.photo[-1]
    items.append({"kind": "photo", "file_id": photo.file_id})
  elif message.video:
    items.append({"kind": "video", "file_id": message.video.file_id})
  elif message.document:
    items.append(
      {
        "kind": "document",
        "file_id": message.document.file_id,
        "file_name": message.document.file_name,
        "mime_type": message.document.mime_type,
      }
    )
  elif message.audio:
    items.append({"kind": "audio", "file_id": message.audio.file_id})
  elif message.voice:
    items.append({"kind": "voice", "file_id": message.voice.file_id})
  elif message.sticker or message.animation or message.video_note:
    items.append({"kind": "unsupported", "file_id": None})
  return items


def build_tg_incoming(message: Message, forum_channel_id: int) -> TgIncomingMessage | None:
  if message.chat.id != forum_channel_id:
    return None
  if message.is_topic_message is False and message.message_thread_id is None:
    return None

  author_name, username = format_tg_author(message)
  text = message.text or message.caption or ""
  media = extract_tg_media(message)

  if not text and not media:
    return None

  if media and not text and any(m.get("kind") == "unsupported" for m in media):
    text = "[Telegram files]"

  return TgIncomingMessage(
    tg_chat_id=message.chat.id,
    tg_message_id=message.message_id,
    tg_thread_id=message.message_thread_id,
    text=text,
    author_name=author_name,
    author_username=username,
    is_bot=bool(message.from_user and message.from_user.is_bot),
    reply_to_tg_message_id=message.reply_to_message.message_id
    if message.reply_to_message
    else None,
    media=media,
  )
