from dataclasses import dataclass


@dataclass(slots=True)
class ChatMapping:
  max_chat_id: int
  tg_chat_id: int
  tg_thread_id: int
  display_name: str
  is_dm: bool


@dataclass(slots=True)
class MaxIncomingMessage:
  max_chat_id: int
  max_message_id: int
  text: str
  sender_id: int | None
  sender_name: str | None
  is_dm: bool
  chat_title: str
  reply_to_max_message_id: int | None
  media: list[dict]


@dataclass(slots=True)
class TgIncomingMessage:
  tg_chat_id: int
  tg_message_id: int
  tg_thread_id: int | None
  text: str
  author_name: str
  author_username: str | None
  is_bot: bool
  reply_to_tg_message_id: int | None
  media: list[dict]
